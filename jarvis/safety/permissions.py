"""The guardrails between an LLM and Caleb's computer.

Jarvis gets real control: a shell, the filesystem, the browser. That is only
safe if something sits between the model's intent and the machine, and that
something is this file.

Three independent layers, all of which must pass:

1. **Forbidden patterns** -- a hard denylist that no approval can override.
   ``rm -rf /``, writing to a raw block device, ``curl | bash``, fork bombs.
   These are refused even if Caleb says yes, because the most likely reason
   the model produced one is that something went wrong.
2. **Risk tiering** -- every action is classified SAFE / CAUTION / DANGEROUS.
   Safe actions run. Dangerous ones stop and ask. What counts as which is
   data, not scattered ``if`` statements, so it can be audited at a glance.
3. **Path jail** -- file operations are confined to roots Caleb has allowed.
   Symlinks and ``..`` are resolved before the check, so neither escapes.

Everything that runs is written to an audit log. The design assumption is
that the model will occasionally be wrong or manipulated by something it
read, and that the blast radius has to be bounded regardless.
"""

from __future__ import annotations

import json
import os
import re
import shlex
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import IntEnum
from pathlib import Path
from typing import Iterable, Sequence


class Risk(IntEnum):
    """How much damage an action could do. Ordered, so comparisons work."""

    SAFE = 0        # reads, queries -- run without asking
    CAUTION = 1     # writes, edits -- run, but audited and reversible
    DANGEROUS = 2   # deletes, installs, sends -- stop and ask Caleb
    FORBIDDEN = 3   # never, with or without approval


@dataclass
class Decision:
    """The verdict on one proposed action."""

    allowed: bool
    risk: Risk
    reason: str
    needs_approval: bool = False
    matched_rule: str | None = None

    @property
    def refused(self) -> bool:
        return not self.allowed and not self.needs_approval

    def describe(self) -> str:
        if self.allowed:
            return f"allowed ({self.risk.name.lower()}): {self.reason}"
        if self.needs_approval:
            return f"needs your approval ({self.risk.name.lower()}): {self.reason}"
        return f"refused: {self.reason}"


# ------------------------------------------------------------------ denylist
# Nothing on this list runs, ever. Not with approval, not in a sandbox, not
# "just this once". Each entry is a shape that is either catastrophic or has
# no legitimate reason to be produced by an assistant.
FORBIDDEN_PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"\brm\s+(-[a-zA-Z]*\s+)*(-[a-zA-Z]*r[a-zA-Z]*f|-[a-zA-Z]*f[a-zA-Z]*r)\S*\s+/(\s|$)"),
     "recursive force-delete of the filesystem root"),
    (re.compile(r"\brm\s+(-\S+\s+)*(~|\$HOME)(/\s*)?$"),
     "recursive delete of the entire home directory"),
    (re.compile(r"\bdd\b[^|]*\bof=/dev/(sd|nvme|hd|mmcblk|disk)"),
     "raw write to a block device -- this destroys a disk"),
    (re.compile(r"\bmkfs(\.\w+)?\b"), "formatting a filesystem"),
    (re.compile(r"\b(shutdown|reboot|halt|poweroff)\b"), "powering off the machine"),
    (re.compile(r":\(\)\s*\{.*\|.*&.*\}\s*;?\s*:"), "fork bomb"),
    (re.compile(r"\b(curl|wget)\b[^|;&]*\|\s*(sudo\s+)?(ba|z|k|d)?sh\b"),
     "piping a download straight into a shell -- runs unreviewed code"),
    (re.compile(r"\bchmod\s+(-R\s+)?777\s+/(\s|$)"), "world-writable filesystem root"),
    (re.compile(r"\bchown\s+-R\s+\S+\s+/(\s|$)"), "recursive chown of the filesystem root"),
    (re.compile(r">\s*/dev/(sd|nvme|hd|mmcblk)"), "redirecting output onto a raw disk"),
    (re.compile(r"\bhistory\s+-c\b|>\s*~?/?\.bash_history"),
     "erasing shell history -- covers tracks, never useful here"),
    (re.compile(r"\b(nc|ncat|netcat)\b.*-e\s*/bin/(ba)?sh"), "reverse shell"),
    (re.compile(r"/etc/(shadow|sudoers)\b"), "touching system credential files"),
    (re.compile(r"\bgit\s+push\b.*--force\b.*\b(main|master)\b"),
     "force-pushing over a main branch"),
)

# Read-only commands. These run without interrupting Caleb.
SAFE_COMMANDS: frozenset[str] = frozenset({
    "ls", "pwd", "cat", "head", "tail", "wc", "grep", "rg", "file",
    "stat", "du", "df", "tree", "which", "whereis", "type", "echo", "date",
    "cal", "uname", "hostname", "whoami", "id", "printenv", "uptime",
    "ps", "top", "free", "sort", "uniq", "cut", "diff", "cmp",
    "basename", "dirname", "realpath", "readlink", "sha256sum", "md5sum",
    "jq", "man", "help",
    "git",   # subcommand-screened below
    "find",  # -exec screened below
})

# Writes and edits. Allowed, but logged, and the path jail still applies.
CAUTION_COMMANDS: frozenset[str] = frozenset({
    "mkdir", "touch", "cp", "mv", "ln", "tee", "chmod", "sed", "truncate",
    "tar", "zip", "unzip", "gzip", "gunzip", "make", "npm", "yarn", "cargo",
})

# Anything that runs code supplied as an argument or a file. These cannot be
# classified by looking at them -- `python3 -c "os.system('rm -rf x')"` is a
# read-only-looking command that does anything at all. Treating an interpreter
# as safe hands the model a way around every other rule in this file, so they
# stop and ask like any other dangerous thing.
INTERPRETERS: frozenset[str] = frozenset({
    "python", "python3", "node", "ruby", "perl", "php", "lua", "Rscript",
    "bash", "sh", "zsh", "ksh", "dash", "fish", "osascript", "awk", "gawk",
    "eval", "exec", "source",
})

# Commands whose job is to run another command. The wrapper itself is
# harmless; what it wraps is the whole question. `env rm -rf x` is a run of
# `rm`, not a run of `env`.
WRAPPER_COMMANDS: frozenset[str] = frozenset({
    "env", "nohup", "time", "timeout", "nice", "ionice", "stdbuf", "setsid",
    "command", "xargs", "watch", "script",
})

# `find` is read-only right up until one of these, which run a command per
# match -- the most efficient way to delete a lot of files by accident.
FIND_EXECUTION_FLAGS: frozenset[str] = frozenset({
    "-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprintf",
})

# Destructive, installing, or outward-facing. Always stops to ask.
DANGEROUS_COMMANDS: frozenset[str] = frozenset({
    "rm", "rmdir", "shred", "dd", "apt", "apt-get", "pip", "pip3", "sudo",
    "su", "kill", "killall", "pkill", "systemctl", "service", "crontab",
    "mount", "umount", "fdisk", "parted", "useradd", "userdel", "passwd",
    "curl", "wget", "ssh", "scp", "rsync", "docker",
})

# git subcommands that move history or reach the network.
DANGEROUS_GIT_SUBCOMMANDS: frozenset[str] = frozenset({
    "push", "reset", "clean", "rebase", "filter-branch", "gc", "prune",
    "remote", "submodule", "config",
})

# git subcommands that only read. Anything not listed here or in
# CAUTION_GIT_SUBCOMMANDS is treated as dangerous -- `git` is a hundred
# programs wearing one name, and a denylist of the scary ones would miss
# the next one.
SAFE_GIT_SUBCOMMANDS: frozenset[str] = frozenset({
    "status", "log", "diff", "show", "blame", "grep", "ls-files", "ls-tree",
    "ls-remote", "rev-parse", "rev-list", "describe", "shortlog", "reflog",
    "cat-file", "count-objects", "whatchanged", "version", "help", "check-ignore",
})

# Modify the working tree or local history, but recoverably.
CAUTION_GIT_SUBCOMMANDS: frozenset[str] = frozenset({
    "add", "commit", "checkout", "switch", "restore", "branch", "tag",
    "stash", "merge", "cherry-pick", "revert", "mv", "apply", "am", "fetch",
    "pull", "worktree", "bisect", "notes", "init", "clone",
})

# git options that take a separate value, so the token after them is that
# value and not the subcommand. Missing these is how `git -c foo=bar push`
# gets read as a run of `foo=bar`.
GIT_VALUE_OPTIONS: frozenset[str] = frozenset({
    "-c", "-C", "--git-dir", "--work-tree", "--namespace", "--exec-path",
    "--config-env", "--super-prefix",
})

# Global options that can make git run an arbitrary program --
# `git -c core.pager='...' log` is a shell in a trench coat.
GIT_EXECUTION_OPTIONS: frozenset[str] = frozenset({
    "-c", "--config-env", "--exec-path",
})


@dataclass
class PermissionPolicy:
    """What Jarvis is allowed to touch. Conservative by default."""

    # File operations are confined to these roots, resolved.
    allowed_roots: list[Path] = field(default_factory=list)
    # Never readable or writable, even inside an allowed root.
    protected_paths: list[Path] = field(default_factory=list)
    # Run CAUTION actions without asking. DANGEROUS always asks regardless.
    auto_approve_caution: bool = True
    # When False, DANGEROUS actions are refused outright rather than queued
    # for approval -- for unattended runs where nobody can answer.
    allow_approval_prompts: bool = True
    max_output_bytes: int = 200_000
    command_timeout_seconds: int = 120

    @classmethod
    def default(cls, home: Path | None = None) -> "PermissionPolicy":
        home = Path(home or Path.home())
        return cls(
            allowed_roots=[home],
            protected_paths=[
                home / ".ssh",
                home / ".gnupg",
                home / ".aws",
                home / ".config" / "gcloud",
                Path("/etc"),
                Path("/boot"),
                Path("/sys"),
                Path("/proc"),
                Path("/dev"),
            ],
        )


class PermissionEngine:
    """Judges every action before it touches the machine."""

    def __init__(self, policy: PermissionPolicy, audit_log: Path | None = None) -> None:
        self.policy = policy
        self.audit_log = Path(audit_log) if audit_log else None
        self._roots = [self._resolve(p) for p in policy.allowed_roots]
        self._protected = [self._resolve(p) for p in policy.protected_paths]

    # ---------------------------------------------------------- primitives
    @staticmethod
    def _resolve(path: Path | str) -> Path:
        """Canonical absolute path with symlinks and ``..`` collapsed.

        ``strict=False`` so a path that does not exist yet (a file about to be
        created) still resolves -- the jail must apply to writes, not only to
        reads of existing files.
        """
        return Path(os.path.expanduser(str(path))).resolve(strict=False)

    def path_allowed(self, path: Path | str) -> Decision:
        """Is this path inside the jail and outside the protected set?"""
        resolved = self._resolve(path)

        for protected in self._protected:
            if resolved == protected or protected in resolved.parents:
                return Decision(
                    False, Risk.FORBIDDEN,
                    f"{resolved} is inside the protected path {protected}",
                    matched_rule="protected_path",
                )

        if not self._roots:
            return Decision(False, Risk.FORBIDDEN, "no allowed roots configured")

        for root in self._roots:
            if resolved == root or root in resolved.parents:
                return Decision(True, Risk.SAFE, f"{resolved} is inside {root}")

        allowed = ", ".join(str(r) for r in self._roots)
        return Decision(
            False, Risk.FORBIDDEN,
            f"{resolved} is outside the allowed area ({allowed})",
            matched_rule="path_jail",
        )

    # ------------------------------------------------------------ commands
    def judge_command(self, command: str) -> Decision:
        """Classify a shell command. Denylist first, then risk tier."""
        stripped = command.strip()
        if not stripped:
            return Decision(False, Risk.FORBIDDEN, "empty command")

        for pattern, why in FORBIDDEN_PATTERNS:
            if pattern.search(stripped):
                return self._log(Decision(
                    False, Risk.FORBIDDEN, why, matched_rule=pattern.pattern
                ), command)

        # A compound command is only as safe as its riskiest part, so every
        # segment is judged and the worst verdict wins.
        segments = self._split_segments(stripped)
        worst = Decision(True, Risk.SAFE, "no risky commands found")
        for segment in segments:
            verdict = self._judge_single(segment)
            if verdict.risk > worst.risk:
                worst = verdict
            if verdict.risk is Risk.FORBIDDEN:
                break

        if worst.risk is Risk.DANGEROUS:
            if not self.policy.allow_approval_prompts:
                worst = Decision(
                    False, Risk.DANGEROUS,
                    worst.reason + " (approval prompts are disabled)",
                    matched_rule=worst.matched_rule,
                )
            else:
                worst = Decision(
                    False, Risk.DANGEROUS, worst.reason,
                    needs_approval=True, matched_rule=worst.matched_rule,
                )
        elif worst.risk is Risk.CAUTION and not self.policy.auto_approve_caution:
            worst = Decision(
                False, Risk.CAUTION, worst.reason,
                needs_approval=True, matched_rule=worst.matched_rule,
            )

        return self._log(worst, command)

    @staticmethod
    def _split_segments(command: str) -> list[str]:
        """Split into the pieces the shell would actually run.

        ``ls && rm -rf build`` must not be waved through because it starts
        with ``ls``, so operators split. But a naive split on those characters
        is wrong in both directions, and both directions bit:

        * ``python3 -c 'import sys; sys.exit(3)'`` is one command. Splitting
          the quoted argument on its semicolon tears the quote in half and
          gets a legitimate command refused.
        * ``echo $(rm -rf ~)`` looks like a run of ``echo``. It is not --
          the substitution runs first, and reading only the leading token
          waves the deletion straight through.

        So this walks the string tracking quote state, splits on operators
        only when they are unquoted, and treats the inside of ``$(...)``,
        backticks, and ``<(...)`` as a segment in its own right. Single
        quotes suppress substitution; double quotes do not, which is why the
        substitution check runs inside them too.
        """
        segments: list[str] = []
        current: list[str] = []
        quote: str | None = None
        # Quote state to restore when a substitution closes.
        stack: list[str | None] = []
        # True when the open quote came from resuming a quoted string this
        # function cut, rather than from a quote Caleb left unterminated.
        resumed = False
        index = 0
        length = len(command)

        def flush(*, balance: bool) -> None:
            # A segment cut at a substitution boundary can end mid-quote, and
            # balancing it keeps the parser from calling valid input broken.
            # The final flush does not balance: a quote the user actually left
            # open is malformed, and closing it here would hide that.
            text = "".join(current)
            if balance and quote is not None:
                text += quote
            text = text.strip()
            if text:
                segments.append(text)
            current.clear()

        while index < length:
            char = command[index]
            pair = command[index:index + 2]

            # Single quotes are literal all the way to the closing quote --
            # no operators, no substitution, nothing.
            if quote == "'":
                if char == "'":
                    quote = None
                current.append(char)
                index += 1
                continue

            if char == "\\" and index + 1 < length:
                current.extend(command[index:index + 2])
                index += 2
                continue

            # Substitutions run their contents, so the contents get judged.
            if pair == "$(" or (quote is None and pair == "<("):
                flush(balance=True)
                stack.append(quote)
                quote, resumed = None, False
                index += 2
                continue
            if char == "`":
                flush(balance=True)
                if stack:
                    quote, resumed = stack.pop(), True
                else:
                    stack.append(quote)
                    quote, resumed = None, False
                index += 1
                continue
            if char == ")" and quote is None and stack:
                flush(balance=True)
                quote = stack.pop()
                resumed = quote is not None
                if quote is not None:
                    # What follows continues the quoted string the
                    # substitution sat inside, so reopen it -- otherwise the
                    # tail arrives as a lone quote and reads as broken input.
                    current.append(quote)
                index += 1
                continue

            if quote == '"':
                if char == '"':
                    quote = None
                current.append(char)
                index += 1
                continue

            if char in "'\"":
                quote, resumed = char, False
                current.append(char)
                index += 1
                continue

            if char in ";|&\n":
                flush(balance=True)
                resumed = False
                index += 1
                continue

            current.append(char)
            index += 1

        flush(balance=resumed)
        return segments

    def _judge_single(self, segment: str, depth: int = 0) -> Decision:
        try:
            tokens = shlex.split(segment)
        except ValueError as exc:
            return Decision(
                False, Risk.FORBIDDEN, f"could not parse the command: {exc}",
                matched_rule="unparseable",
            )
        # An empty string is not a command name. These come from balancing a
        # segment cut at a substitution boundary -- `echo "x $(date)"` leaves
        # a trailing `""` that would otherwise read as an unknown binary.
        tokens = [t for t in tokens if t]
        if not tokens:
            return Decision(True, Risk.SAFE, "empty segment")

        # Skip leading VAR=value assignments to find the real binary.
        index = 0
        while index < len(tokens) and re.fullmatch(r"\w+=.*", tokens[index]):
            index += 1
        if index >= len(tokens):
            return Decision(True, Risk.SAFE, "variable assignment only")

        binary = Path(tokens[index]).name
        args = tokens[index + 1:]

        if binary == "git":
            return self._judge_git(args)

        if binary in WRAPPER_COMMANDS:
            return self._judge_wrapper(binary, args, depth)

        if binary in INTERPRETERS:
            return Decision(
                False, Risk.DANGEROUS,
                f"{binary!r} runs whatever code it's handed, so I can't tell "
                "from the outside what it does",
                matched_rule=f"interpreter:{binary}",
            )

        if binary == "find":
            flag = next((a for a in args if a in FIND_EXECUTION_FLAGS), None)
            if flag is not None:
                return Decision(
                    False, Risk.DANGEROUS,
                    f"find {flag} runs a command on every match",
                    matched_rule=f"find:{flag}",
                )
            return Decision(True, Risk.SAFE, "'find' is read-only without -exec")

        if binary in DANGEROUS_COMMANDS:
            return Decision(
                False, Risk.DANGEROUS,
                f"{binary!r} can delete, install, or reach the network",
                matched_rule=f"dangerous:{binary}",
            )
        if binary in CAUTION_COMMANDS:
            return Decision(
                True, Risk.CAUTION, f"{binary!r} modifies files",
                matched_rule=f"caution:{binary}",
            )
        if binary in SAFE_COMMANDS:
            return Decision(True, Risk.SAFE, f"{binary!r} is read-only")

        # Unknown binaries are treated as dangerous rather than safe. An
        # allowlist that fails open is not an allowlist.
        return Decision(
            False, Risk.DANGEROUS,
            f"{binary!r} isn't a command I recognise, so I'd rather ask first",
            matched_rule="unknown_binary",
        )

    def _judge_wrapper(self, binary: str, args: Sequence[str], depth: int) -> Decision:
        """Judge what a wrapper wraps, not the wrapper.

        ``env rm -rf x`` is a run of ``rm``. Reading the leading token gets
        ``env``, which is read-only, and waves the delete straight through.
        """
        if depth >= 3:
            # Wrappers wrapping wrappers is not something a legitimate command
            # does three deep, and unbounded recursion is its own problem.
            return Decision(
                False, Risk.DANGEROUS,
                "that's wrapped in too many layers for me to see what it does",
                matched_rule="wrapper_depth",
            )

        index = 0
        while index < len(args):
            token = args[index]
            # The wrapper's own flags, its VAR=value assignments, and the bare
            # numbers that things like `timeout 5` take.
            if token.startswith("-") or re.fullmatch(r"\w+=.*|[\d.]+[smhd]?", token):
                index += 1
                continue
            break

        inner = list(args[index:])
        if not inner:
            return Decision(
                True, Risk.CAUTION, f"{binary!r} with nothing to run",
                matched_rule=f"wrapper:{binary}",
            )

        verdict = self._judge_single(shlex.join(inner), depth + 1)
        return Decision(
            verdict.allowed, verdict.risk,
            f"{binary} runs {inner[0]!r}: {verdict.reason}",
            needs_approval=verdict.needs_approval,
            matched_rule=verdict.matched_rule,
        )

    @staticmethod
    def _judge_git(args: Sequence[str]) -> Decision:
        """Screen a git invocation by its real subcommand.

        Two things make this harder than reading ``args[0]``. Global options
        come before the subcommand and some of them take a separate value, so
        the first non-flag token is often that value rather than the
        subcommand. And a few of those options -- ``-c``, ``--exec-path`` --
        can point git at an arbitrary program, which turns any subcommand,
        however innocent, into a way to run anything.
        """
        index = 0
        while index < len(args) and args[index].startswith("-"):
            option = args[index]
            name = option.split("=", 1)[0]
            if name in GIT_EXECUTION_OPTIONS:
                return Decision(
                    False, Risk.DANGEROUS,
                    f"git {name} can point git at another program to run",
                    matched_rule=f"git-option:{name}",
                )
            # A separate value only follows when it wasn't given as --opt=value.
            if name in GIT_VALUE_OPTIONS and "=" not in option:
                index += 1
            index += 1

        sub = args[index] if index < len(args) else ""

        if not sub:
            return Decision(True, Risk.SAFE, "git on its own just prints help")
        if sub in DANGEROUS_GIT_SUBCOMMANDS:
            return Decision(
                False, Risk.DANGEROUS, f"git {sub} changes history or the remote",
                matched_rule=f"git:{sub}",
            )
        if sub in CAUTION_GIT_SUBCOMMANDS:
            return Decision(
                True, Risk.CAUTION, f"git {sub} changes the working tree",
                matched_rule=f"git:{sub}",
            )
        if sub in SAFE_GIT_SUBCOMMANDS:
            return Decision(True, Risk.SAFE, f"git {sub} is read-only")

        return Decision(
            False, Risk.DANGEROUS,
            f"git {sub} isn't a subcommand I know, so I'd rather ask first",
            matched_rule="unknown_git_subcommand",
        )

    # --------------------------------------------------------------- files
    def judge_read(self, path: Path | str) -> Decision:
        verdict = self.path_allowed(path)
        return self._log(verdict, f"read {path}")

    def judge_write(self, path: Path | str, *, deleting: bool = False) -> Decision:
        verdict = self.path_allowed(path)
        if not verdict.allowed:
            return self._log(verdict, f"write {path}")

        if deleting:
            decision = Decision(
                False, Risk.DANGEROUS, f"deleting {path}",
                needs_approval=self.policy.allow_approval_prompts,
                matched_rule="delete",
            )
        else:
            decision = Decision(
                True, Risk.CAUTION, f"writing {path}", matched_rule="write",
            )
            if not self.policy.auto_approve_caution:
                decision = Decision(
                    False, Risk.CAUTION, f"writing {path}",
                    needs_approval=True, matched_rule="write",
                )
        return self._log(decision, f"{'delete' if deleting else 'write'} {path}")

    # --------------------------------------------------------------- audit
    def _log(self, decision: Decision, action: str) -> Decision:
        if self.audit_log is None:
            return decision
        entry = {
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "action": action[:600],
            "risk": decision.risk.name,
            "allowed": decision.allowed,
            "needs_approval": decision.needs_approval,
            "reason": decision.reason,
            "rule": decision.matched_rule,
        }
        try:
            self.audit_log.parent.mkdir(parents=True, exist_ok=True)
            with self.audit_log.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry) + "\n")
        except OSError:
            # An unwritable audit log must never block or crash the decision.
            pass
        return decision

    def recent_audit(self, limit: int = 20) -> list[dict]:
        if self.audit_log is None or not self.audit_log.exists():
            return []
        lines = self.audit_log.read_text(encoding="utf-8").splitlines()[-limit:]
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out
