"""Who Jarvis is allowed to learn from.

The requirement is that Jarvis studies real, proven, successful traders and
ignores random unverified accounts. That is enforced in two layers:

1. A human-curated registry (``data/curated_sources.yaml``). Nothing is
   studied unless its handle appears there. An allowlist is the only honest
   way to do this -- follower counts are buyable and engagement is farmable.

2. Automated vetting on top, so a curated entry can still be demoted. Hard
   disqualifiers (guaranteed-return claims, signal-group selling, paid
   pump groups) reject outright regardless of how big the account is;
   positive evidence (regulatory registration, audited or independently
   documented results, verifiable long tenure) is what earns 'verified'.

An entry that is curated but lacking hard evidence lands on 'provisional':
Jarvis will read it, but lessons from it start at lower confidence and have to
earn their way up through actual outcomes.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger(__name__)

PLATFORMS = {"youtube", "instagram", "tiktok", "web", "news"}

# Claims that end the conversation. A real trader does not promise these.
HARD_DISQUALIFIERS: tuple[re.Pattern, ...] = tuple(
    re.compile(p, re.I)
    for p in (
        r"guaranteed\s+(returns?|profits?|wins?)",
        r"\b\d+\s*%\s*(win|accuracy)\s*rate\b",
        r"never\s+loses?\b",
        r"risk[-\s]?free\s+(profit|trading|returns?)",
        r"\bsignals?\s+group\b",
        r"\bpump\b.*\bgroup\b",
        r"copy\s*my\s*trades?\s*(and|to)\s*(get|earn|make)",
        r"double\s+your\s+(money|account)",
        r"quit\s+your\s+job\s+in\s+\d+",
    )
)

# Evidence that actually moves the needle, and what each is worth.
EVIDENCE_WEIGHTS: dict[str, float] = {
    "regulatory_registration": 0.30,   # e.g. registered advisor / licensed
    "audited_track_record": 0.28,      # third-party verified results
    "published_positions": 0.16,       # calls posted before the outcome, publicly
    "institutional_background": 0.14,  # verifiable desk / fund experience
    "published_book_or_research": 0.10,
    "platform_verified": 0.06,
    "long_tenure": 0.10,               # years of continuous public output
    "peer_endorsement": 0.06,
}

VERIFIED_THRESHOLD = 0.45
PROVISIONAL_THRESHOLD = 0.18


@dataclass
class SourceCandidate:
    """A creator being considered as a teacher."""

    platform: str
    handle: str
    display_name: str | None = None
    url: str | None = None
    evidence: list[str] = field(default_factory=list)
    tenure_years: float = 0.0
    followers: int = 0
    bio: str = ""
    notes: str = ""

    def evidence_set(self) -> set[str]:
        return {e.strip().lower() for e in self.evidence if e.strip()}


@dataclass
class VettingResult:
    candidate: SourceCandidate
    verification: str          # verified | provisional | rejected
    credibility: float         # 0..1 starting trust
    rationale: list[str]

    @property
    def accepted(self) -> bool:
        return self.verification in {"verified", "provisional"}


class TraderVetting:
    """Scores a candidate teacher. Hard disqualifiers always win."""

    def vet(self, candidate: SourceCandidate) -> VettingResult:
        rationale: list[str] = []
        text = " ".join(
            filter(None, [candidate.bio, candidate.notes, candidate.display_name])
        )
        for pattern in HARD_DISQUALIFIERS:
            if pattern.search(text):
                return VettingResult(
                    candidate, "rejected", 0.0,
                    [f"disqualified: promotional language matching /{pattern.pattern}/"],
                )

        if candidate.platform not in PLATFORMS:
            return VettingResult(
                candidate, "rejected", 0.0,
                [f"unsupported platform {candidate.platform!r}"],
            )

        score = 0.0
        evidence = candidate.evidence_set()
        for tag in sorted(evidence):
            weight = EVIDENCE_WEIGHTS.get(tag)
            if weight:
                score += weight
                rationale.append(f"+{weight:.2f} {tag.replace('_', ' ')}")
            else:
                rationale.append(f"+0.00 unrecognised evidence tag {tag!r}")

        if candidate.tenure_years >= 5 and "long_tenure" not in evidence:
            score += EVIDENCE_WEIGHTS["long_tenure"]
            rationale.append(f"+0.10 {candidate.tenure_years:.0f} years of public output")

        # Audience is a weak tie-breaker only, and it is capped hard on purpose:
        # reach is not evidence of skill.
        if candidate.followers >= 100_000:
            score += 0.04
            rationale.append("+0.04 substantial audience (weak signal, capped)")

        if not evidence:
            rationale.append("no verifiable track-record evidence supplied")

        if score >= VERIFIED_THRESHOLD:
            verification = "verified"
        elif score >= PROVISIONAL_THRESHOLD:
            verification = "provisional"
        else:
            verification = "rejected"
            rationale.append(
                f"score {score:.2f} below the provisional bar of {PROVISIONAL_THRESHOLD:.2f}"
            )

        # Starting trust: verified teachers begin credible but not infallible;
        # provisional ones start below a coin flip and must prove themselves.
        credibility = {
            "verified": min(0.85, 0.55 + score * 0.4),
            "provisional": 0.42,
            "rejected": 0.0,
        }[verification]

        return VettingResult(candidate, verification, round(credibility, 3), rationale)


class SourceRegistry:
    """Loads the curated file, vets every entry, and syncs it into memory."""

    def __init__(self, knowledge_store, vetting: TraderVetting | None = None) -> None:
        self.knowledge = knowledge_store
        self.vetting = vetting or TraderVetting()

    # ------------------------------------------------------------- loading
    @staticmethod
    def load_file(path: Path | str) -> list[SourceCandidate]:
        raw = _load_yaml(Path(path))
        candidates: list[SourceCandidate] = []
        for platform, entries in (raw or {}).items():
            for entry in entries or []:
                if not isinstance(entry, dict) or not entry.get("handle"):
                    continue
                candidates.append(
                    SourceCandidate(
                        platform=platform,
                        handle=str(entry["handle"]),
                        display_name=entry.get("name"),
                        url=entry.get("url"),
                        evidence=list(entry.get("evidence") or []),
                        tenure_years=float(entry.get("tenure_years") or 0),
                        followers=int(entry.get("followers") or 0),
                        bio=str(entry.get("bio") or ""),
                        notes=str(entry.get("notes") or ""),
                    )
                )
        return candidates

    def sync(self, path: Path | str) -> dict[str, int]:
        """Vet the curated file and write the survivors into memory."""
        counts = {"verified": 0, "provisional": 0, "rejected": 0}
        for candidate in self.load_file(path):
            result = self.vetting.vet(candidate)
            counts[result.verification] = counts.get(result.verification, 0) + 1
            if not result.accepted:
                log.info(
                    "rejected source %s/%s: %s",
                    candidate.platform, candidate.handle, "; ".join(result.rationale),
                )
                continue
            self.knowledge.upsert_source(
                platform=candidate.platform,
                handle=candidate.handle,
                display_name=candidate.display_name,
                url=candidate.url,
                verification=result.verification,
                credibility=result.credibility,
                evidence="; ".join(result.rationale),
            )
        return counts

    def is_trusted(self, platform: str, handle: str) -> bool:
        return any(
            s["handle"].lower() == handle.lower()
            for s in self.knowledge.trusted_sources(platform)
        )


def _load_yaml(path: Path) -> dict[str, Any]:
    """Read YAML if PyYAML is installed, else fall back to a small parser.

    The curated file is deliberately simple (mapping -> list of flat mappings
    with optional string lists), so the fallback covers it without adding a
    hard dependency.
    """
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore

        return yaml.safe_load(text) or {}
    except ImportError:
        return _mini_yaml(text)


def _mini_yaml(text: str) -> dict[str, Any]:
    """Minimal YAML subset: top-level keys, ``- key: value`` items, ``- item`` lists."""
    root: dict[str, Any] = {}
    current_list: list | None = None
    current_item: dict | None = None
    current_seq_key: str | None = None

    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        line = raw.strip()

        if indent == 0 and line.endswith(":"):
            current_list = []
            root[line[:-1].strip()] = current_list
            current_item = None
            current_seq_key = None
            continue

        if line.startswith("- ") and current_list is not None:
            body = line[2:].strip()
            if ":" in body and not body.startswith('"'):
                key, _, value = body.partition(":")
                current_item = {key.strip(): _scalar(value.strip())}
                current_list.append(current_item)
                current_seq_key = None
            elif current_seq_key and current_item is not None:
                current_item[current_seq_key].append(_scalar(body))
            continue

        if current_item is not None and ":" in line:
            key, _, value = line.partition(":")
            key, value = key.strip(), value.strip()
            if value == "":
                current_seq_key = key
                current_item[key] = []
            else:
                current_item[key] = _scalar(value)
                current_seq_key = None
            continue

        if line.startswith("- ") and current_seq_key and current_item is not None:
            current_item[current_seq_key].append(_scalar(line[2:].strip()))

    return root


def _scalar(value: str):
    value = value.strip().strip('"').strip("'")
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value
