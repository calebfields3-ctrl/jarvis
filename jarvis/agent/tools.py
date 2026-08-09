"""What Jarvis can actually do, and the gate every one of them passes through.

Each tool is a plain function with a docstring; the SDK turns the signature
and docstring into the schema Claude sees, so the docstring *is* the
specification. They are built inside :meth:`ToolBox.build` rather than at
module level because each one closes over the permission engine, the
approval callback, and the trading brain.

The rule the whole file is arranged around: **no tool touches the machine
before ``PermissionEngine`` has judged the specific thing it is about to
do.** Not the category -- the actual command, the actual path. A tool that
asks permission for one thing and then does another is worse than no gate at
all, because it looks like there is one.

Approval happens *inside* the tool. When Caleb says no, that is a normal
tool result explaining he declined, not an exception -- Claude reads it and
adapts, which is what you want. Refusals work the same way, so a model that
has been talked into something destructive gets a sentence back rather than
a crash it might try to route around.
"""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from jarvis.safety.permissions import Decision, PermissionEngine, Risk

log = logging.getLogger(__name__)

try:  # The SDK is optional -- Jarvis still runs without a mind.
    from anthropic import beta_tool
except ImportError:  # pragma: no cover - exercised by the no-SDK path
    beta_tool = None


# Callback signature: (human-readable action, the engine's verdict) -> approved?
Approver = Callable[[str, Decision], bool]


def always_deny(action: str, decision: Decision) -> bool:
    """The default when nobody wired up an approval prompt.

    Defaulting to *yes* here would mean an unattended Jarvis silently
    self-approves every dangerous action, which is the opposite of the
    guarantee the permission engine is supposed to make.
    """
    return False


@dataclass
class ToolCall:
    """One thing Jarvis did, for the HUD and the transcript."""

    name: str
    summary: str
    risk: Risk = Risk.SAFE
    ok: bool = True
    detail: str = ""
    at: float = field(default_factory=time.time)


class ToolBox:
    """Builds the tool set and owns the gate in front of it."""

    def __init__(
        self,
        engine: PermissionEngine,
        *,
        jarvis: Any = None,
        approver: Approver | None = None,
        on_call: Callable[[ToolCall], None] | None = None,
    ) -> None:
        self.engine = engine
        self._jarvis = jarvis
        self.approve = approver or always_deny
        self.on_call = on_call
        self.calls: list[ToolCall] = []
        # The background study session, if one is running. Held here rather
        # than on the agent so it survives the conversation being reset.
        self._study = None

    # ----------------------------------------------------------- the gate
    def _gate(self, decision: Decision, action: str) -> str | None:
        """Run one decision to ground. Returns an explanation, or None to proceed.

        Three outcomes, and the difference between the last two matters:
        refused means no answer will change it; needs-approval means asking
        Caleb is the answer.
        """
        if decision.allowed:
            return None
        if decision.refused:
            return f"I can't do that: {decision.reason}."
        if self.approve(action, decision):
            return None
        return f"You declined that one. ({decision.reason})"

    def _record(self, call: ToolCall) -> None:
        self.calls.append(call)
        if self.on_call is not None:
            try:
                self.on_call(call)
            except Exception:  # a broken display must not break the agent
                log.debug("on_call hook raised", exc_info=True)

    @property
    def jarvis(self):
        """The trading brain, built on first use.

        Constructing it vets sources and opens the database, which is a
        second or two -- not worth paying if this session is only ever going
        to be asked to open a file.
        """
        if self._jarvis is None:
            from jarvis.brain import Jarvis

            self._jarvis = Jarvis()
        return self._jarvis

    # --------------------------------------------------------------- build
    def build(self) -> list:
        """Every tool, wired to this box. Order is the order Claude sees."""
        if beta_tool is None:
            raise RuntimeError(
                "The anthropic package isn't installed, so there are no tools "
                "to give him. Run: pip install 'anthropic>=0.69'"
            )

        box = self

        # ------------------------------------------------------ the machine
        @beta_tool
        def run_command(command: str, why: str = "") -> str:
            """Run a shell command on this computer and return its output.

            Prefer the specific tools below when one fits -- read_file over
            `cat`, write_file over `echo >`. Reach for this when nothing else
            does the job.

            Destructive commands stop and ask Caleb first. A few shapes
            (wiping the filesystem, piping a download into a shell) are
            refused outright and no amount of rephrasing gets them through,
            so don't try -- explain to Caleb what you wanted to do instead.

            Args:
                command: The command line to run, exactly as typed in a shell.
                why: One short phrase on what this is for. Caleb sees it if
                    the command needs his approval.
            """
            verdict = box.engine.judge_command(command)
            blocked = box._gate(verdict, f"run: {command}" + (f" ({why})" if why else ""))
            if blocked is not None:
                box._record(ToolCall("run_command", command, verdict.risk, False, blocked))
                return blocked

            try:
                proc = subprocess.run(
                    command,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=box.engine.policy.command_timeout_seconds,
                    cwd=str(Path.home()),
                )
            except subprocess.TimeoutExpired:
                msg = (
                    f"That took longer than "
                    f"{box.engine.policy.command_timeout_seconds}s and I stopped it."
                )
                box._record(ToolCall("run_command", command, verdict.risk, False, msg))
                return msg
            except OSError as exc:
                box._record(ToolCall("run_command", command, verdict.risk, False, str(exc)))
                return f"Couldn't run that: {exc}"

            output = (proc.stdout or "") + (proc.stderr or "")
            output = box._truncate(output)
            box._record(ToolCall(
                "run_command", command, verdict.risk, proc.returncode == 0,
                output[:200],
            ))
            if not output.strip():
                return f"(exit {proc.returncode}, no output)"
            return f"(exit {proc.returncode})\n{output}"

        # --------------------------------------------------------- the disk
        @beta_tool
        def read_file(path: str) -> str:
            """Read a text file and return its contents.

            Args:
                path: Path to the file. `~` works.
            """
            verdict = box.engine.judge_read(path)
            blocked = box._gate(verdict, f"read {path}")
            if blocked is not None:
                box._record(ToolCall("read_file", path, verdict.risk, False, blocked))
                return blocked

            target = Path(path).expanduser()
            try:
                text = target.read_text(encoding="utf-8", errors="replace")
            except FileNotFoundError:
                box._record(ToolCall("read_file", path, verdict.risk, False, "missing"))
                return f"There's no file at {target}."
            except IsADirectoryError:
                return f"{target} is a folder. Use list_directory for that."
            except OSError as exc:
                return f"Couldn't read {target}: {exc}"

            box._record(ToolCall("read_file", str(target), verdict.risk, True))
            return box._truncate(text) or "(the file is empty)"

        @beta_tool
        def write_file(path: str, content: str) -> str:
            """Write text to a file, creating or replacing it.

            This replaces the whole file. If you mean to change part of one,
            read it first and write back the full new contents.

            Args:
                path: Where to write. Parent folders are created as needed.
                content: The complete new contents of the file.
            """
            verdict = box.engine.judge_write(path)
            blocked = box._gate(verdict, f"write {path} ({len(content)} chars)")
            if blocked is not None:
                box._record(ToolCall("write_file", path, verdict.risk, False, blocked))
                return blocked

            target = Path(path).expanduser()
            existed = target.exists()
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
            except OSError as exc:
                box._record(ToolCall("write_file", path, verdict.risk, False, str(exc)))
                return f"Couldn't write {target}: {exc}"

            box._record(ToolCall("write_file", str(target), Risk.CAUTION, True))
            verb = "Replaced" if existed else "Wrote"
            return f"{verb} {target} ({len(content)} characters)."

        @beta_tool
        def list_directory(path: str = "~") -> str:
            """List the files and folders inside a folder, with sizes.

            Long listings are cut at 200 entries, so narrow the path rather
            than listing somewhere huge and hoping.

            Args:
                path: The folder to list. Defaults to Caleb's home folder.
            """
            verdict = box.engine.judge_read(path)
            blocked = box._gate(verdict, f"list {path}")
            if blocked is not None:
                return blocked

            target = Path(path).expanduser()
            if not target.is_dir():
                return f"{target} isn't a folder."
            entries = []
            for item in sorted(target.iterdir())[:200]:
                if item.is_dir():
                    entries.append(f"{item.name}/")
                else:
                    try:
                        entries.append(f"{item.name}  ({item.stat().st_size:,} bytes)")
                    except OSError:
                        entries.append(item.name)
            box._record(ToolCall("list_directory", str(target), Risk.SAFE, True))
            return "\n".join(entries) or "(empty)"

        # ------------------------------------------------------- the world
        @beta_tool
        def open_in_browser(url: str) -> str:
            """Open a web page in Chrome on Caleb's screen.

            Use this to *show* him something -- a chart, an article, a filing.
            It opens the page in front of him; it does not give you the page
            back. To read a page yourself, use search_web or fetch_page.

            Args:
                url: The full https:// address to open.
            """
            verdict = box.jarvis.safety.check(url)
            if not verdict.is_safe:
                box._record(ToolCall("open_in_browser", url, Risk.DANGEROUS, False, str(verdict)))
                return f"I won't open that one -- {verdict}."

            opener = box._browser_opener()
            if opener is None:
                return (
                    f"I can't reach a browser from in here. The address is {url} "
                    "if you want to open it yourself."
                )
            try:
                subprocess.Popen(
                    [opener, url],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except OSError as exc:
                return f"Couldn't open the browser: {exc}"
            box._record(ToolCall("open_in_browser", url, Risk.CAUTION, True))
            return f"Opened {url} in Chrome."

        @beta_tool
        def search_web(query: str, pages: int = 3) -> str:
            """Search the web and read the results that pass the safety check.

            Only pages that clear the URL safety gate are fetched. A page
            whose safety can't be established is skipped, not guessed at, so
            some results come back listed as blocked -- say so rather than
            filling the gap from memory.

            Args:
                query: What to search for.
                pages: How many results to actually open. Keep it small.
            """
            try:
                report = box.jarvis.web.research(query, max_pages=max(1, min(pages, 8)))
            except Exception as exc:
                return f"The search didn't go through: {exc}"

            box._record(ToolCall("search_web", query, Risk.SAFE, not report.error))
            if report.error:
                return f"The search didn't go through: {report.error}"

            parts = [
                f"[{page.title or 'untitled'}] {page.url}\n{page.text}"
                for page in report.opened
            ]
            if report.blocked:
                parts.append(
                    "Skipped as unverified:\n"
                    + "\n".join(f"  {url} -- {why}" for url, why in report.blocked)
                )
            if not parts:
                return "Nothing came back that I'd trust enough to read."
            return box._truncate("\n\n".join(parts))

        # ------------------------------------------------------- the market
        @beta_tool
        def portfolio() -> str:
            """Caleb's account: cash, open positions, and today's and yesterday's P/L.

            This is the record of what he has told Jarvis he traded, so it is
            only as current as `record_trade` has kept it.
            """
            box._record(ToolCall("portfolio", "summary", Risk.SAFE, True))
            return box.jarvis.portfolio_summary()

        @beta_tool
        def scan_for_setups(symbols: str = "", intraday: bool = False) -> str:
            """Scan charts for patterns and return what's worth a look.

            Args:
                symbols: Space- or comma-separated tickers. Leave empty to
                    scan the whole watch universe -- that's 500+ names and
                    takes a while, so name symbols when you can.
                intraday: True for day-trading setups on today's bars, False
                    for daily-chart swing patterns.
            """
            names = [s.strip().upper() for s in symbols.replace(",", " ").split() if s.strip()]
            box._record(ToolCall(
                "scan_for_setups", ", ".join(names) or "full universe", Risk.SAFE, True,
            ))
            if intraday:
                report = box.jarvis.daytrade_scan(names or None)
                return box.jarvis.daytrade_briefing(report)
            report = box.jarvis.scan_market(names or None)
            return box.jarvis.scan_briefing(report)

        @beta_tool
        def symbol_report(symbol: str) -> str:
            """Everything Jarvis knows about one ticker: price, trend, patterns, news.

            Args:
                symbol: The ticker, e.g. NVDA.
            """
            box._record(ToolCall("symbol_report", symbol.upper(), Risk.SAFE, True))
            return box.jarvis.brief_symbol(symbol.upper())

        @beta_tool
        def size_a_trade(
            symbol: str, direction: str, entry: float, stop: float, target: float
        ) -> str:
            """Work out position size and risk for a trade Caleb is considering.

            This sizes and checks the trade against his account and his rules.
            It does not place anything -- Jarvis has no broker connection, so
            every order is Caleb's to enter himself.

            Args:
                symbol: The ticker.
                direction: "long" or "short".
                entry: Intended entry price.
                stop: Where the idea is wrong and he gets out.
                target: Where he takes profit.
            """
            try:
                plan = box.jarvis.plan_trade(symbol.upper(), direction, entry, stop, target)
            except Exception as exc:
                return f"Couldn't size that: {exc}"
            box._record(ToolCall("size_a_trade", f"{symbol.upper()} {direction}", Risk.SAFE, True))
            return plan.describe()

        @beta_tool
        def trading_rules_check() -> str:
            """Whether Caleb can take another day trade right now.

            Covers the pattern-day-trader count against the rolling five-day
            window, his daily loss limit, consecutive losses, and equity.
            Check this before encouraging a trade, not after.
            """
            status = box.jarvis.trading_status()
            box._record(ToolCall("trading_rules_check", "status", Risk.SAFE, True))
            return status.describe()

        @beta_tool
        def record_trade(action: str, symbol: str, shares: float, price: float) -> str:
            """Write a fill into Caleb's book after he's traded it in his broker.

            Only call this when he says he actually did it. Jarvis's numbers
            about Caleb are only honest if this matches reality.

            Args:
                action: "buy" or "sell".
                symbol: The ticker.
                shares: How many shares.
                price: Fill price per share.
            """
            act = action.strip().lower()
            if act not in {"buy", "sell"}:
                return "That has to be 'buy' or 'sell'."
            try:
                box.jarvis.portfolio.record_trade(symbol.upper(), act, shares, price)
            except Exception as exc:
                return f"Couldn't record that: {exc}"
            box._record(ToolCall(
                "record_trade", f"{act} {shares} {symbol.upper()} @ {price}", Risk.CAUTION, True,
            ))
            return f"Recorded: {act} {shares:g} {symbol.upper()} at {price:.2f}."

        # ------------------------------------------------------ the learning
        @beta_tool
        def what_i_know(top: int = 12) -> str:
            """Jarvis's own track record: which patterns have earned trust and which haven't.

            Read this before claiming a setup works. The hit rates here are
            measured on graded outcomes, not asserted.

            Args:
                top: How many lessons to list.
            """
            box._record(ToolCall("what_i_know", f"top {top}", Risk.SAFE, True))
            return box.jarvis.what_i_know(top=top)

        @beta_tool
        def study(topic: str = "") -> str:
            """Go learn: pull from curated traders, the web, and the news, and distil it.

            Slow -- tens of seconds. Use it when Caleb asks Jarvis to go learn
            something, not to answer a question he's waiting on.

            Args:
                topic: What to study. Empty means Jarvis picks from his gaps.
            """
            box._record(ToolCall("study", topic or "own gaps", Risk.SAFE, True))
            try:
                if topic:
                    report = box.jarvis.web.research(topic, max_pages=5)
                    box.jarvis.distill_pending()
                    return (
                        f"Studied {topic}: {len(report.opened)} sources read and "
                        f"distilled, {len(report.blocked)} skipped as unverified."
                    )
                result = box.jarvis.learn_cycle()
                return "Learning cycle done: " + ", ".join(
                    f"{k}={v}" for k, v in sorted(result.items())
                )
            except Exception as exc:
                return f"The learning run failed: {exc}"

        @beta_tool
        def study_while_away(topic: str = "", hours: float = 2.0) -> str:
            """Keep studying in the background after Caleb walks away.

            Use this when he says he's leaving and wants you to keep working
            -- "I'm out for a few hours, go learn about the market". It starts
            and returns immediately; the studying carries on behind you, so
            say goodbye properly rather than waiting.

            It reads and grades continuously: articles, video transcripts from
            the curated traders, the news wire, and its own past calls against
            what the market actually did. It stops at the deadline on its own.
            Tell him roughly when you'll stop.

            Args:
                topic: What to concentrate on, e.g. "options flow" or
                    "opening range breakouts". Leave empty to work on
                    whatever the biggest gaps are.
                hours: How long to keep going. Capped at 12.
            """
            from jarvis.learning.deepstudy import StudySession

            if box._study is not None and box._study.running:
                return (
                    "I'm already studying "
                    f"{box._study.topic or 'my own gaps'}. Stop that one first "
                    "if you want me on something else."
                )

            box._study = StudySession(box.jarvis, topic=topic, hours=hours)
            box._study.start()
            box._record(ToolCall(
                "study_while_away", f"{topic or 'own gaps'} for {hours}h", Risk.SAFE, True,
            ))
            focus = f"on {topic}" if topic else "on whatever I'm weakest at"
            return (
                f"Studying {focus}, for up to {box._study.hours:g} hours. "
                "I'll tell you what I found when you're back."
            )

        @beta_tool
        def study_progress() -> str:
            """What the background studying has turned up, running or finished.

            Call this when Caleb comes back and asks what you learned, or when
            he wants to know how it's going.
            """
            from jarvis.learning.deepstudy import describe_progress, load_progress

            if box._study is not None:
                return box._study.describe()
            progress = load_progress(box.jarvis.memory)
            if progress is None:
                return "I haven't studied on my own yet."
            return describe_progress(progress)

        @beta_tool
        def stop_studying() -> str:
            """Stop the background studying now, before its deadline."""
            if box._study is None or not box._study.running:
                return "I'm not studying at the moment."
            box._study.stop("you asked me to stop")
            box._record(ToolCall("stop_studying", "stopped", Risk.SAFE, True))
            return box._study.describe()

        @beta_tool
        def remember(fact: str, key: str = "") -> str:
            """Store something about Caleb that should survive this session.

            Preferences, goals, how he likes to be spoken to, what he's
            working on. Not market data -- that has its own home.

            Args:
                fact: What to remember, in a sentence.
                key: Optional short label, e.g. "risk_tolerance". Reusing a
                    key overwrites what was there.
            """
            label = key.strip() or f"note_{int(time.time())}"
            box.jarvis.memory.profile.set(f"agent:{label}", fact)
            box._record(ToolCall("remember", f"{label}: {fact[:60]}", Risk.CAUTION, True))
            return f"Noted, under '{label}'."

        @beta_tool
        def recall() -> str:
            """Everything Jarvis has been told to remember about Caleb.

            Worth reading at the start of a session -- it's what carries
            across from previous conversations.
            """
            stored = box.jarvis.memory.profile.all()
            notes = {k[len("agent:"):]: v for k, v in stored.items() if k.startswith("agent:")}
            if not notes:
                return "Nothing stored yet."
            return "\n".join(f"{k}: {v}" for k, v in sorted(notes.items()))

        @beta_tool
        def audit_trail(limit: int = 20) -> str:
            """The log of what Jarvis has done on this machine and what was refused.

            Args:
                limit: How many recent entries.
            """
            entries = box.engine.recent_audit(limit=limit)
            if not entries:
                return "Nothing logged yet."
            return "\n".join(
                f"{e['at']}  {e['risk']:<9} {'ok ' if e['allowed'] else 'no '} {e['action']}"
                for e in entries
            )

        return [
            run_command, read_file, write_file, list_directory,
            open_in_browser, search_web,
            portfolio, scan_for_setups, symbol_report, size_a_trade,
            trading_rules_check, record_trade,
            what_i_know, study,
            study_while_away, study_progress, stop_studying,
            remember, recall, audit_trail,
        ]

    # ------------------------------------------------------------- helpers
    def _truncate(self, text: str) -> str:
        cap = self.engine.policy.max_output_bytes
        if len(text) <= cap:
            return text
        half = cap // 2
        cut = len(text) - cap
        return f"{text[:half]}\n\n... [{cut:,} characters cut] ...\n\n{text[-half:]}"

    @staticmethod
    def _browser_opener() -> str | None:
        """Find something that can put a URL on Caleb's screen.

        On a Chromebook, ``xdg-open`` inside the Linux container hands the URL
        to ChromeOS, which opens it in his real browser. That indirection is
        the only way out of the container, so it is worth preferring.
        """
        from shutil import which

        for candidate in ("xdg-open", "google-chrome", "chromium", "open"):
            if which(candidate):
                return candidate
        return None
