"""The feedback loop. This is what makes Jarvis self-learning rather than self-confident.

Every pattern Jarvis spots is written down *before* the outcome is known, with
the price, the direction and the horizon. Once the horizon has passed, this
module looks up what actually happened, grades the call, and folds the result
back into the lesson that produced it.

A pattern that keeps working climbs from hypothesis to working to expertise. A
pattern that keeps failing gets retired, and the teacher who taught it loses
credibility. Nothing is promoted for being persuasive -- only for being right.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Sequence

from ..market.patterns import Bar

log = logging.getLogger(__name__)

# A move smaller than this is noise, not a hit -- roughly a round-trip in
# spread and fees on a liquid name.
NOISE_BAND_PCT = 0.25


@dataclass
class GradedSignal:
    signal_id: int
    symbol: str
    pattern_key: str
    direction: str
    entry: float
    exit: float
    return_pct: float
    hit: bool


@dataclass
class EvaluationReport:
    graded: list[GradedSignal]
    skipped_no_data: int = 0
    still_open: int = 0
    promoted: list[str] = None      # pattern keys that reached 'expertise'
    retired: list[str] = None       # pattern keys that were retired

    def __post_init__(self) -> None:
        self.promoted = self.promoted or []
        self.retired = self.retired or []

    @property
    def hit_rate(self) -> float:
        return (
            sum(1 for g in self.graded if g.hit) / len(self.graded)
            if self.graded else 0.0
        )

    def summary(self) -> str:
        if not self.graded:
            return "No predictions were ready to grade this cycle."
        parts = [
            f"Graded {len(self.graded)} predictions, {self.hit_rate * 100:.0f}% correct."
        ]
        if self.promoted:
            parts.append("Promoted to expertise: " + ", ".join(sorted(set(self.promoted))) + ".")
        if self.retired:
            parts.append("Retired as unreliable: " + ", ".join(sorted(set(self.retired))) + ".")
        return " ".join(parts)


class OutcomeEvaluator:
    def __init__(
        self,
        provider,
        signal_store,
        knowledge_store,
        *,
        promotion_samples: int = 20,
        promotion_confidence: float = 0.62,
        noise_band_pct: float = NOISE_BAND_PCT,
    ) -> None:
        self.provider = provider
        self.signals = signal_store
        self.knowledge = knowledge_store
        self.promotion_samples = promotion_samples
        self.promotion_confidence = promotion_confidence
        self.noise_band_pct = noise_band_pct

    # ------------------------------------------------------------- grading
    def evaluate_open_signals(self, *, limit: int = 500) -> EvaluationReport:
        report = EvaluationReport(graded=[])
        open_signals = self.signals.open_signals()[:limit]
        if not open_signals:
            return report

        by_symbol: dict[str, list[dict]] = {}
        for signal in open_signals:
            by_symbol.setdefault(signal["symbol"], []).append(signal)

        histories = self.provider.bulk_bars(list(by_symbol), period="1y", interval="1d")

        for symbol, signals in by_symbol.items():
            bars = histories.get(symbol) or self.provider.bars(symbol, "1y", "1d")
            if not bars:
                report.skipped_no_data += len(signals)
                continue
            index = {bar.ts: i for i, bar in enumerate(bars)}
            for signal in signals:
                graded = self._grade_one(signal, bars, index)
                if graded is None:
                    report.still_open += 1
                    continue
                report.graded.append(graded)
                self.signals.grade(
                    graded.signal_id, graded.exit, graded.return_pct, graded.hit
                )
                if signal.get("lesson_id"):
                    lesson = self.knowledge.reinforce(
                        signal["lesson_id"],
                        hit=graded.hit,
                        promotion_samples=self.promotion_samples,
                        promotion_conf=self.promotion_confidence,
                    )
                    if lesson and lesson.tier == "expertise":
                        report.promoted.append(lesson.pattern_key or lesson.topic)
                    elif lesson and lesson.tier == "retired":
                        report.retired.append(lesson.pattern_key or lesson.topic)
        return report

    def _grade_one(
        self, signal: dict, bars: Sequence[Bar], index: dict[str, int]
    ) -> GradedSignal | None:
        detected_day = (signal["detected_at"] or "")[:10]
        start = index.get(detected_day)
        if start is None:
            # Signal fired on a day this history doesn't cover (weekend stamp,
            # or the series starts later). Anchor to the nearest prior bar.
            candidates = [i for i, b in enumerate(bars) if b.ts <= detected_day]
            if not candidates:
                return None
            start = candidates[-1]

        horizon = int(signal.get("horizon_bars") or 5)
        end = start + horizon
        if end >= len(bars):
            return None  # the future hasn't happened yet

        entry = float(signal["price_at_signal"]) or bars[start].close
        exit_price = bars[end].close
        if entry <= 0:
            return None
        return_pct = (exit_price - entry) / entry * 100
        if signal["direction"] == "long":
            hit = return_pct > self.noise_band_pct
        else:
            hit = return_pct < -self.noise_band_pct

        return GradedSignal(
            signal_id=signal["id"],
            symbol=signal["symbol"],
            pattern_key=signal["pattern_key"],
            direction=signal["direction"],
            entry=round(entry, 4),
            exit=round(exit_price, 4),
            return_pct=round(return_pct, 3),
            hit=hit,
        )

    # ------------------------------------------------------------ reporting
    def pattern_scoreboard(self, *, min_samples: int = 1) -> list[dict]:
        """What Jarvis has actually learned, ranked by measured performance."""
        rows = self.signals.db.query(
            """
            SELECT s.pattern_key                          AS pattern_key,
                   COUNT(*)                               AS samples,
                   AVG(o.hit)                             AS hit_rate,
                   AVG(CASE WHEN s.direction = 'long' THEN o.return_pct
                            ELSE -o.return_pct END)       AS avg_edge_pct
            FROM outcomes o
            JOIN signals s ON s.id = o.signal_id
            GROUP BY s.pattern_key
            HAVING COUNT(*) >= ?
            ORDER BY hit_rate DESC, samples DESC
            """,
            (min_samples,),
        )
        scoreboard = []
        for row in rows:
            lesson = self.knowledge.lesson_for_pattern(row["pattern_key"])
            scoreboard.append(
                {
                    "pattern": row["pattern_key"],
                    "samples": int(row["samples"]),
                    "hit_rate": round(float(row["hit_rate"] or 0), 3),
                    "avg_edge_pct": round(float(row["avg_edge_pct"] or 0), 3),
                    "tier": lesson.tier if lesson else "unknown",
                    "confidence": lesson.confidence if lesson else None,
                }
            )
        return scoreboard

    def backfill_from_history(
        self,
        symbols: Sequence[str],
        detectors_scan,
        *,
        period: str = "2y",
        max_signals_per_symbol: int = 60,
    ) -> int:
        """Bootstrap the track record by replaying detectors over past bars.

        This is how Jarvis gets from "has read about a golden cross" to "has
        measured 400 golden crosses". Every replayed signal is graded on the
        bars that followed it, exactly as a live one would be, so the resulting
        hit rates are honest walk-forward numbers rather than curve fits.
        """
        recorded = 0
        histories = self.provider.bulk_bars(symbols, period=period, interval="1d")
        for symbol, bars in histories.items():
            if len(bars) < 220:
                continue
            per_symbol = 0
            # Step forward a bar at a time, only ever showing detectors the past.
            for cutoff in range(210, len(bars) - 1):
                if per_symbol >= max_signals_per_symbol:
                    break
                window = bars[: cutoff + 1]
                for detection in detectors_scan(window):
                    lesson = self.knowledge.lesson_for_pattern(detection.pattern_key)
                    signal_id = self.signals.record(
                        symbol=symbol,
                        pattern_key=detection.pattern_key,
                        direction=detection.direction,
                        price=window[-1].close,
                        timeframe="1d",
                        horizon_bars=detection.horizon_bars,
                        features=detection.features,
                        lesson_id=lesson.id if lesson else None,
                        confidence=detection.strength,
                        detected_at=f"{window[-1].ts} 00:00:00",
                    )
                    if signal_id:
                        recorded += 1
                        per_symbol += 1
        return recorded
