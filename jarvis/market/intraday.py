"""Intraday pattern detection for day trading.

Daily-bar patterns answer "should I own this for a few weeks". Day trading asks
a different question, on a different clock, with different failure modes, so it
gets its own detectors rather than a reused daily one with a smaller interval.

Three things separate these from ``patterns.py``:

* **VWAP is the reference price.** Institutions benchmark fills against it, so
  intraday support and resistance cluster there far more reliably than at any
  moving average.
* **The opening range anchors the day.** The high and low of the first 30
  minutes define the levels the rest of the session trades around.
* **Time of day gates everything.** A breakout at 09:45 and the identical
  breakout at 12:30 are not the same trade, and a setup with no time left to
  resolve before the bell is not a trade at all.

Every detection carries a stop and a target, because an intraday signal without
an exit plan is not actionable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Sequence

from .patterns import Bar, atr, stdev
from .session import (
    Phase,
    group_by_session,
    minutes_into_session,
    minutes_to_close,
    phase_at,
    regular_hours_only,
    session_date,
)

# The first 30 minutes set the day's reference range.
OPENING_RANGE_MINUTES = 30


@dataclass
class IntradaySignal:
    """An intraday setup, complete enough to act on or reject."""

    pattern_key: str
    direction: str              # 'long' | 'short'
    strength: float             # 0..1 before track record is applied
    entry: float
    stop: float
    target: float
    phase: Phase
    description: str
    features: dict = field(default_factory=dict)

    @property
    def risk_per_share(self) -> float:
        return abs(self.entry - self.stop)

    @property
    def reward_per_share(self) -> float:
        return abs(self.target - self.entry)

    @property
    def reward_risk(self) -> float:
        """R multiple. Below ~1.5 a day trade rarely survives its own costs."""
        risk = self.risk_per_share
        return 0.0 if risk <= 0 else self.reward_per_share / risk

    @property
    def is_actionable(self) -> bool:
        return self.risk_per_share > 0 and self.reward_risk >= 1.5


# ------------------------------------------------------------------- VWAP
def vwap_series(bars: Sequence[Bar]) -> list[float]:
    """Session-anchored VWAP, reset at each new trading day.

    Resetting matters: a VWAP carried across days drifts to a number no
    institution is benchmarking against, and the level stops working.
    """
    out: list[float] = []
    cumulative_pv = cumulative_volume = 0.0
    current_day: date | None = None

    for bar in bars:
        day = session_date(bar.ts)
        if day != current_day:
            current_day = day
            cumulative_pv = cumulative_volume = 0.0
        typical = (bar.high + bar.low + bar.close) / 3
        cumulative_pv += typical * bar.volume
        cumulative_volume += bar.volume
        out.append(cumulative_pv / cumulative_volume if cumulative_volume else bar.close)
    return out


def opening_range(bars: Sequence[Bar], minutes: int = OPENING_RANGE_MINUTES):
    """High/low of the first N minutes of the most recent session."""
    if not bars:
        return None, None
    today = session_date(bars[-1].ts)
    window = []
    for bar in bars:
        if session_date(bar.ts) != today:
            continue
        # Explicit None check: the 09:30 bar is minute 0, and `elapsed or
        # default` would treat that falsy zero as missing and drop the single
        # most important bar of the range.
        elapsed = minutes_into_session(bar.ts)
        if elapsed is not None and 0 <= elapsed < minutes:
            window.append(bar)
    if not window:
        return None, None
    return max(b.high for b in window), min(b.low for b in window)


def session_bars(bars: Sequence[Bar]) -> list[Bar]:
    """Regular-hours bars belonging to the most recent session."""
    if not bars:
        return []
    today = session_date(bars[-1].ts)
    return [b for b in regular_hours_only(bars) if session_date(b.ts) == today]


def relative_volume(bars: Sequence[Bar], lookback_sessions: int = 5) -> float | None:
    """Today's pace vs the same point in previous sessions.

    Compared at the *same minute* of the day, not against a whole-day average --
    otherwise every stock looks quiet at 09:45 and busy at 15:55.
    """
    if not bars:
        return None
    sessions = group_by_session(regular_hours_only(bars))
    days = sorted(sessions)
    if len(days) < 2:
        return None
    today = days[-1]
    elapsed = minutes_into_session(bars[-1].ts)
    if elapsed is None or elapsed <= 0:
        return None

    today_volume = sum(b.volume for b in sessions[today])
    prior_paces: list[float] = []
    for day in days[-(lookback_sessions + 1):-1]:
        matched = []
        for bar in sessions[day]:
            # Same falsy-zero trap as the opening range: minute 0 is real.
            prior_elapsed = minutes_into_session(bar.ts)
            if prior_elapsed is not None and prior_elapsed <= elapsed:
                matched.append(bar)
        if matched:
            prior_paces.append(sum(b.volume for b in matched))
    if not prior_paces:
        return None
    baseline = sum(prior_paces) / len(prior_paces)
    return today_volume / baseline if baseline else None


# -------------------------------------------------------------- detectors
def detect_opening_range_break(bars: Sequence[Bar]) -> IntradaySignal | None:
    """The foundational day trade: break of the first 30 minutes' range."""
    today = session_bars(bars)
    if len(today) < 8:
        return None
    last = today[-1]
    phase = phase_at(last.ts)
    elapsed = minutes_into_session(last.ts)
    # `<` not `<=`: the bar at exactly minute 30 is the first bar *after* the
    # range, and is precisely the one that most often breaks it.
    if elapsed is None or elapsed < OPENING_RANGE_MINUTES:
        return None
    # The edge is in the first couple of hours; by the afternoon the range has
    # usually been probed from both sides and no longer means anything.
    if elapsed > 180:
        return None

    high, low = opening_range(bars)
    if high is None or low is None or high <= low:
        return None
    span = high - low
    rvol = relative_volume(bars) or 1.0

    if last.close > high:
        stop = high - span * 0.25
        return IntradaySignal(
            pattern_key="opening_range_breakout",
            direction="long",
            strength=min(0.82, 0.45 + min(rvol, 3) * 0.12),
            entry=last.close,
            stop=stop,
            target=last.close + span,
            phase=phase,
            description=(
                f"Broke the {OPENING_RANGE_MINUTES}-minute opening range high "
                f"({high:.2f}) on {rvol:.1f}x relative volume"
            ),
            features={"or_high": round(high, 4), "or_low": round(low, 4),
                      "rvol": round(rvol, 2), "minutes_in": round(elapsed)},
        )
    if last.close < low:
        stop = low + span * 0.25
        return IntradaySignal(
            pattern_key="opening_range_breakdown",
            direction="short",
            strength=min(0.82, 0.45 + min(rvol, 3) * 0.12),
            entry=last.close,
            stop=stop,
            target=last.close - span,
            phase=phase,
            description=(
                f"Lost the {OPENING_RANGE_MINUTES}-minute opening range low "
                f"({low:.2f}) on {rvol:.1f}x relative volume"
            ),
            features={"or_high": round(high, 4), "or_low": round(low, 4),
                      "rvol": round(rvol, 2), "minutes_in": round(elapsed)},
        )
    return None


def detect_vwap_reclaim(bars: Sequence[Bar]) -> IntradaySignal | None:
    """Price crossing back through VWAP -- the level institutions defend."""
    today = session_bars(bars)
    if len(today) < 10:
        return None
    vwaps = vwap_series(today)
    last, prev = today[-1], today[-2]
    vwap_now, vwap_prev = vwaps[-1], vwaps[-2]
    phase = phase_at(last.ts)
    if not phase.is_tradeable:
        return None

    session_high = max(b.high for b in today)
    session_low = min(b.low for b in today)
    day_range = session_high - session_low
    if day_range <= 0:
        return None

    reclaimed = prev.close < vwap_prev and last.close > vwap_now
    rejected = prev.close > vwap_prev and last.close < vwap_now
    if not (reclaimed or rejected):
        return None
    # A cross that barely clears VWAP is noise, not a reclaim.
    if abs(last.close - vwap_now) < day_range * 0.03:
        return None

    rvol = relative_volume(bars) or 1.0
    if reclaimed:
        return IntradaySignal(
            pattern_key="vwap_reclaim",
            direction="long",
            strength=min(0.75, 0.42 + min(rvol, 3) * 0.1),
            entry=last.close,
            stop=min(vwap_now, prev.low) - day_range * 0.05,
            target=last.close + (last.close - vwap_now) * 3,
            phase=phase,
            description=f"Reclaimed VWAP at {vwap_now:.2f} on {rvol:.1f}x volume",
            features={"vwap": round(vwap_now, 4), "rvol": round(rvol, 2)},
        )
    return IntradaySignal(
        pattern_key="vwap_rejection",
        direction="short",
        strength=min(0.75, 0.42 + min(rvol, 3) * 0.1),
        entry=last.close,
        stop=max(vwap_now, prev.high) + day_range * 0.05,
        target=last.close - (vwap_now - last.close) * 3,
        phase=phase,
        description=f"Rejected at VWAP {vwap_now:.2f} on {rvol:.1f}x volume",
        features={"vwap": round(vwap_now, 4), "rvol": round(rvol, 2)},
    )


def detect_gap_and_go(bars: Sequence[Bar]) -> IntradaySignal | None:
    """Gapped from yesterday's close and pushing further in the gap direction."""
    sessions = group_by_session(regular_hours_only(bars))
    days = sorted(sessions)
    if len(days) < 2:
        return None
    today, yesterday = sessions[days[-1]], sessions[days[-2]]
    if len(today) < 4:
        return None

    prior_close = yesterday[-1].close
    if prior_close <= 0:
        return None
    open_price = today[0].open
    gap_pct = (open_price - prior_close) / prior_close * 100
    if abs(gap_pct) < 2.0:
        return None

    last = today[-1]
    phase = phase_at(last.ts)
    elapsed = minutes_into_session(last.ts) or 0
    # This is an opening-drive trade; after 90 minutes the gap is old news.
    if not phase.is_tradeable or elapsed > 90:
        return None

    session_high = max(b.high for b in today)
    session_low = min(b.low for b in today)
    rvol = relative_volume(bars) or 1.0

    if gap_pct > 0 and last.close >= session_high * 0.999 and last.close > open_price:
        stop = max(open_price, session_low)
        return IntradaySignal(
            pattern_key="gap_and_go_long",
            direction="long",
            strength=min(0.8, 0.45 + min(rvol, 3) * 0.1 + min(abs(gap_pct), 10) / 50),
            entry=last.close,
            stop=stop,
            target=last.close + (last.close - stop) * 2,
            phase=phase,
            description=f"Gapped up {gap_pct:.1f}% and making session highs on {rvol:.1f}x volume",
            features={"gap_pct": round(gap_pct, 2), "rvol": round(rvol, 2)},
        )
    if gap_pct < 0 and last.close <= session_low * 1.001 and last.close < open_price:
        stop = min(open_price, session_high)
        return IntradaySignal(
            pattern_key="gap_and_go_short",
            direction="short",
            strength=min(0.8, 0.45 + min(rvol, 3) * 0.1 + min(abs(gap_pct), 10) / 50),
            entry=last.close,
            stop=stop,
            target=last.close - (stop - last.close) * 2,
            phase=phase,
            description=f"Gapped down {abs(gap_pct):.1f}% and making session lows on {rvol:.1f}x volume",
            features={"gap_pct": round(gap_pct, 2), "rvol": round(rvol, 2)},
        )
    return None


def detect_failed_breakdown(bars: Sequence[Bar]) -> IntradaySignal | None:
    """New session low that immediately reclaims -- traps the late shorts."""
    today = session_bars(bars)
    if len(today) < 12:
        return None
    last = today[-1]
    phase = phase_at(last.ts)
    if not phase.is_tradeable:
        return None

    earlier = today[:-3]
    recent = today[-3:]
    if not earlier:
        return None
    prior_low = min(b.low for b in earlier)
    made_new_low = min(b.low for b in recent) < prior_low
    reclaimed = last.close > prior_low
    if not (made_new_low and reclaimed):
        return None

    day_range = max(b.high for b in today) - min(b.low for b in today)
    if day_range <= 0:
        return None
    stop = min(b.low for b in recent) - day_range * 0.03
    return IntradaySignal(
        pattern_key="failed_breakdown_reversal",
        direction="long",
        strength=0.6,
        entry=last.close,
        stop=stop,
        target=last.close + (last.close - stop) * 2,
        phase=phase,
        description=f"Undercut {prior_low:.2f} and reclaimed it -- trapped shorts",
        features={"prior_low": round(prior_low, 4)},
    )


def detect_momentum_surge(bars: Sequence[Bar]) -> IntradaySignal | None:
    """A sharp, high-volume thrust relative to the session's own volatility."""
    today = session_bars(bars)
    if len(today) < 15:
        return None
    last = today[-1]
    phase = phase_at(last.ts)
    if not phase.is_tradeable:
        return None

    closes = [b.close for b in today]
    recent_move = (closes[-1] - closes[-4]) / closes[-4] * 100 if closes[-4] else 0
    typical = stdev([
        (closes[i] - closes[i - 1]) / closes[i - 1] * 100
        for i in range(1, len(closes)) if closes[i - 1]
    ])
    if typical <= 0:
        return None
    z = recent_move / (typical * 3 ** 0.5)
    if abs(z) < 2.5:
        return None

    volumes = [b.volume for b in today]
    baseline = sum(volumes[:-3]) / max(len(volumes) - 3, 1) or 1
    surge_volume = sum(volumes[-3:]) / 3
    if surge_volume < baseline * 1.5:
        return None

    day_range = max(b.high for b in today) - min(b.low for b in today)
    direction = "long" if z > 0 else "short"
    stop = (
        last.close - day_range * 0.25 if direction == "long"
        else last.close + day_range * 0.25
    )
    target = (
        last.close + day_range * 0.5 if direction == "long"
        else last.close - day_range * 0.5
    )
    return IntradaySignal(
        pattern_key=f"momentum_surge_{direction}",
        direction=direction,
        strength=min(0.78, 0.45 + abs(z) / 15),
        entry=last.close,
        stop=stop,
        target=target,
        phase=phase,
        description=(
            f"{abs(recent_move):.1f}% thrust in 3 bars "
            f"({abs(z):.1f} sigma) on {surge_volume / baseline:.1f}x volume"
        ),
        features={"move_pct": round(recent_move, 2), "sigma": round(z, 2)},
    )


def detect_power_hour_trend(bars: Sequence[Bar]) -> IntradaySignal | None:
    """After 15:00, a stock holding above VWAP at session highs tends to close strong."""
    today = session_bars(bars)
    if len(today) < 20:
        return None
    last = today[-1]
    if phase_at(last.ts) is not Phase.POWER_HOUR:
        return None
    remaining = minutes_to_close(last.ts) or 0
    if remaining < 15:
        return None  # no time left for the trade to work

    vwaps = vwap_series(today)
    session_high = max(b.high for b in today)
    session_low = min(b.low for b in today)
    day_range = session_high - session_low
    if day_range <= 0:
        return None

    above_vwap = last.close > vwaps[-1]
    near_high = last.close >= session_high - day_range * 0.15
    near_low = last.close <= session_low + day_range * 0.15

    if above_vwap and near_high:
        stop = max(vwaps[-1], last.close - day_range * 0.2)
        return IntradaySignal(
            pattern_key="power_hour_trend_long",
            direction="long",
            strength=0.58,
            entry=last.close,
            stop=stop,
            target=last.close + (last.close - stop) * 1.8,
            phase=Phase.POWER_HOUR,
            description="Holding above VWAP at session highs into the close",
            features={"vwap": round(vwaps[-1], 4), "minutes_left": round(remaining)},
        )
    if not above_vwap and near_low:
        stop = min(vwaps[-1], last.close + day_range * 0.2)
        return IntradaySignal(
            pattern_key="power_hour_trend_short",
            direction="short",
            strength=0.58,
            entry=last.close,
            stop=stop,
            target=last.close - (stop - last.close) * 1.8,
            phase=Phase.POWER_HOUR,
            description="Pinned below VWAP at session lows into the close",
            features={"vwap": round(vwaps[-1], 4), "minutes_left": round(remaining)},
        )
    return None


INTRADAY_DETECTORS: list[Callable[[Sequence[Bar]], IntradaySignal | None]] = [
    detect_opening_range_break,
    detect_vwap_reclaim,
    detect_gap_and_go,
    detect_failed_breakdown,
    detect_momentum_surge,
    detect_power_hour_trend,
]

INTRADAY_PATTERN_KEYS = [
    "opening_range_breakout", "opening_range_breakdown",
    "vwap_reclaim", "vwap_rejection",
    "gap_and_go_long", "gap_and_go_short",
    "failed_breakdown_reversal",
    "momentum_surge_long", "momentum_surge_short",
    "power_hour_trend_long", "power_hour_trend_short",
]


def scan_intraday(bars: Sequence[Bar], *, require_actionable: bool = True) -> list[IntradaySignal]:
    """Run every intraday detector over one symbol's session.

    ``require_actionable`` drops setups whose stop and target imply worse than
    1.5R. A day trade that risks a dollar to make eighty cents loses money at a
    perfectly respectable hit rate.
    """
    found: list[IntradaySignal] = []
    for detector in INTRADAY_DETECTORS:
        try:
            signal = detector(bars)
        except Exception:
            continue
        if signal and (signal.is_actionable or not require_actionable):
            found.append(signal)
    return found
