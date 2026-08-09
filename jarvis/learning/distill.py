"""Turning studied content into lessons.

A transcript is not knowledge. The distiller pulls out sentences that make a
*claim about what markets do*, normalises them, attaches them to a pattern
detector where one exists, and discards the other 95% (sponsor reads, hype,
anecdotes, calls to action).

Extraction is rule-based on purpose: it is inspectable, deterministic and needs
no model at runtime. ``Distiller`` is a protocol, so an LLM-backed distiller can
be dropped in later without touching the pipeline.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Protocol, Sequence

# Vocabulary that says "this sentence is about market behaviour".
TOPIC_TERMS: dict[str, tuple[str, ...]] = {
    "patterns": (
        "breakout", "breakdown", "support", "resistance", "trend line", "trendline",
        "double top", "double bottom", "head and shoulders", "flag", "pennant",
        "wedge", "triangle", "consolidation", "base", "engulfing", "hammer",
        "doji", "gap", "golden cross", "death cross", "squeeze", "pullback",
        "retracement", "higher high", "lower low",
        # Traders describe these setups by what price does, not by the label.
        "volume", "new high", "new low", "all-time high", "52-week high",
        "day high", "day low", "closes above", "closes below", "breaks above",
        "breaks below", "candle", "wick", "range",
    ),
    "indicators": (
        "rsi", "macd", "moving average", "vwap", "bollinger", "atr", "adx",
        "stochastic", "ema", "sma", "oscillator", "divergence",
    ),
    "risk": (
        "position size", "position sizing", "stop loss", "stop-loss", "risk",
        "drawdown", "r multiple", "risk reward", "risk/reward", "exposure",
        "leverage", "hedge", "cut losses", "let winners run",
    ),
    "fundamentals": (
        "earnings", "revenue", "guidance", "margin", "cash flow", "balance sheet",
        "valuation", "p/e", "pe ratio", "eps", "dividend", "buyback",
    ),
    "macro": (
        "fed", "federal reserve", "interest rate", "inflation", "cpi", "jobs report",
        "unemployment", "yield curve", "recession", "treasury", "central bank",
    ),
    "psychology": (
        "discipline", "patience", "fomo", "revenge trade", "overtrading",
        "journal", "emotion", "conviction", "process",
    ),
    "execution": (
        "limit order", "market order", "slippage", "liquidity", "spread",
        "fill", "scaling in", "scaling out", "partial",
    ),
}

# Sentences making a generalisation are worth keeping; a one-off anecdote is not.
CLAIM_MARKERS: tuple[re.Pattern, ...] = tuple(
    re.compile(p, re.I)
    for p in (
        r"\b(tends? to|usually|typically|generally|often|more often than not)\b",
        r"\b(the key is|the rule is|the point is|what matters is)\b",
        r"\b(always|never)\s+\w+",
        r"\bif\b.{5,60}\bthen\b",
        r"\bwhen\b.{5,60}\b(you|it|price|the market)\b.{0,40}\b(should|will|tends?|often)\b",
        r"\b(you (want|need) to|make sure you|the mistake .{0,30}is)\b",
        r"\b(signals?|indicates?|confirms?|suggests?)\b",
    )
)

# Noise that must never become a lesson, however claim-shaped it looks.
NOISE_MARKERS: tuple[re.Pattern, ...] = tuple(
    re.compile(p, re.I)
    for p in (
        r"\b(subscribe|like and|smash that|link in (the )?bio|promo code|sponsor)",
        r"\b(join (my|our) (discord|telegram|group)|dm me|link below)",
        r"\b(guaranteed|risk[- ]free|get rich|easy money)",
        r"\b(course|masterclass|bootcamp)\b.{0,30}\b(sale|discount|enroll)",
        r"^\s*(um|uh|so yeah|alright guys|what'?s up)\b",
    )
)

# Sentence -> detector, so a distilled claim can inherit a real track record.
PATTERN_HINTS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"golden cross", re.I), "golden_cross"),
    (re.compile(r"death cross", re.I), "death_cross"),
    (re.compile(r"oversold.{0,40}(bounce|revers|buy)|rsi.{0,20}below 30", re.I), "rsi_oversold_reversal"),
    (re.compile(r"overbought.{0,40}(fade|revers|sell)|rsi.{0,20}above 70", re.I), "rsi_overbought_reversal"),
    (re.compile(r"(break|clos)(s|es|ing|ed)? (out )?(above|over).{0,30}(high|resistance)", re.I), "breakout_20d_high"),
    (re.compile(r"(break|clos)(s|es|ing|ed)? (down )?(below|under).{0,30}(low|support)", re.I), "breakdown_20d_low"),
    (re.compile(r"bullish engulfing", re.I), "bullish_engulfing"),
    (re.compile(r"bearish engulfing", re.I), "bearish_engulfing"),
    (re.compile(r"\bhammer\b", re.I), "hammer"),
    (re.compile(r"shooting star", re.I), "shooting_star"),
    (re.compile(r"macd.{0,30}cross(es|ed)? (above|up)", re.I), "macd_bull_cross"),
    (re.compile(r"macd.{0,30}cross(es|ed)? (below|down)", re.I), "macd_bear_cross"),
    (re.compile(r"squeeze.{0,40}(break|expand|resolve)", re.I), "squeeze_breakout_up"),
    (re.compile(r"gap (up|higher).{0,40}(hold|continu|fill)", re.I), "gap_up_hold"),
    (re.compile(r"gap (down|lower).{0,40}(continu|keep)", re.I), "gap_down_continue"),
    (re.compile(r"volume dr(y|ie)(s|d)? up|volume contraction", re.I), "volume_dryup_base"),
)

KIND_BY_TOPIC = {
    "patterns": "pattern",
    "indicators": "pattern",
    "risk": "risk",
    "fundamentals": "fundamental",
    "macro": "fundamental",
    "psychology": "rule",
    "execution": "rule",
}

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
_WHITESPACE = re.compile(r"\s+")
# A newline that isn't preceded by sentence-ending punctuation is a wrap, not a
# sentence break. Transcripts and captions are wrapped constantly, and splitting
# on them chopped real claims in half.
_SOFT_WRAP = re.compile(r"(?<![.!?:;])\n+(?![-*•])")


@dataclass
class DistilledLesson:
    topic: str
    claim: str
    kind: str
    pattern_key: str | None
    score: float


class Distiller(Protocol):
    def distill(self, text: str, *, title: str | None = None) -> list[DistilledLesson]: ...


class RuleBasedDistiller:
    """Deterministic extraction. Tuned to be stingy rather than generous."""

    def __init__(self, *, min_score: float = 0.5, max_lessons: int = 12) -> None:
        self.min_score = min_score
        self.max_lessons = max_lessons

    def distill(self, text: str, *, title: str | None = None) -> list[DistilledLesson]:
        if not text:
            return []
        found: dict[str, DistilledLesson] = {}
        unwrapped = _SOFT_WRAP.sub(" ", text)
        for raw in _SENTENCE_SPLIT.split(unwrapped):
            sentence = _WHITESPACE.sub(" ", raw).strip()
            lesson = self._score_sentence(sentence)
            if lesson and lesson.score >= self.min_score:
                # Keep the strongest phrasing of any duplicate claim.
                key = _normalise_key(lesson.claim)
                if key not in found or lesson.score > found[key].score:
                    found[key] = lesson
        ranked = sorted(found.values(), key=lambda l: l.score, reverse=True)
        return ranked[: self.max_lessons]

    def _score_sentence(self, sentence: str) -> DistilledLesson | None:
        if not (30 <= len(sentence) <= 320):
            return None
        for noise in NOISE_MARKERS:
            if noise.search(sentence):
                return None

        lowered = sentence.lower()
        topic, term_hits = self._topic_for(lowered)
        if topic is None:
            return None

        claim_hits = sum(1 for marker in CLAIM_MARKERS if marker.search(sentence))
        if claim_hits == 0:
            return None

        pattern_key = next(
            (key for rx, key in PATTERN_HINTS if rx.search(sentence)), None
        )

        score = min(
            1.0,
            0.32
            + min(term_hits, 3) * 0.12
            + min(claim_hits, 3) * 0.10
            + (0.12 if pattern_key else 0.0),
        )
        # A sentence stuffed with first-person storytelling is usually an
        # anecdote, not a transferable rule.
        if re.search(r"\bi (bought|sold|made|lost)\b", lowered):
            score -= 0.2

        return DistilledLesson(
            topic=topic,
            claim=_clean_claim(sentence),
            kind=KIND_BY_TOPIC.get(topic, "concept"),
            pattern_key=pattern_key,
            score=round(score, 3),
        )

    @staticmethod
    def _topic_for(lowered: str) -> tuple[str | None, int]:
        best_topic, best_hits = None, 0
        for topic, terms in TOPIC_TERMS.items():
            hits = sum(1 for term in terms if term in lowered)
            if hits > best_hits:
                best_topic, best_hits = topic, hits
        return best_topic, best_hits


def _clean_claim(sentence: str) -> str:
    sentence = sentence.strip().strip("-–—•* ")
    sentence = re.sub(r"^(and|but|so|okay|ok|now|right|look|listen)[,\s]+", "", sentence, flags=re.I)
    sentence = sentence[0].upper() + sentence[1:] if sentence else sentence
    if sentence and sentence[-1] not in ".!?":
        sentence += "."
    return sentence


def _normalise_key(claim: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", claim.lower())[:120]


def ingest_lessons(
    knowledge_store,
    lessons: Sequence[DistilledLesson],
    *,
    source_id: int | None = None,
    content_id: int | None = None,
    source_credibility: float = 0.5,
) -> int:
    """Write distilled lessons into memory at a confidence the source has earned.

    Starting confidence blends how clean the extraction was with how credible
    the teacher is, and is capped below certainty -- nothing is believed on the
    strength of having been said.
    """
    written = 0
    for lesson in lessons:
        confidence = round(min(0.72, 0.25 + lesson.score * 0.3 + source_credibility * 0.3), 3)
        knowledge_store.add_lesson(
            topic=lesson.topic,
            claim=lesson.claim,
            kind=lesson.kind,
            pattern_key=lesson.pattern_key,
            tier="hypothesis",
            confidence=confidence,
            source_id=source_id,
            content_id=content_id,
        )
        written += 1
    return written
