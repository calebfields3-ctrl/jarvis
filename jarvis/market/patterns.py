"""Chart pattern detection.

Pure Python over plain OHLCV bars -- no pandas in the hot path, because this
runs across 500+ symbols on every scan cycle. Each detector returns a
``Detection`` whose ``pattern_key`` links back to a lesson in memory, so a
pattern's track record is what decides how loudly Jarvis calls it out.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence


@dataclass(frozen=True)
class Bar:
    ts: str
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class Detection:
    pattern_key: str
    direction: str            # 'long' | 'short'
    strength: float           # 0..1, detector's own conviction before history
    horizon_bars: int
    features: dict = field(default_factory=dict)
    description: str = ""


# ------------------------------------------------------------------ indicators
def sma(values: Sequence[float], period: int) -> float | None:
    if len(values) < period or period <= 0:
        return None
    return sum(values[-period:]) / period


def sma_series(values: Sequence[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    window = sum(values[:period])
    out[period - 1] = window / period
    for i in range(period, len(values)):
        window += values[i] - values[i - period]
        out[i] = window / period
    return out


def ema_series(values: Sequence[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    k = 2 / (period + 1)
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def rsi(values: Sequence[float], period: int = 14) -> float | None:
    """Wilder's RSI."""
    if len(values) < period + 1:
        return None
    gains = losses = 0.0
    for i in range(1, period + 1):
        delta = values[i] - values[i - 1]
        gains += max(delta, 0.0)
        losses += max(-delta, 0.0)
    avg_gain, avg_loss = gains / period, losses / period
    for i in range(period + 1, len(values)):
        delta = values[i] - values[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(delta, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-delta, 0.0)) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def rsi_series(values: Sequence[float], period: int = 14) -> list[float | None]:
    """Wilder's RSI at every bar, in one pass.

    Computed incrementally rather than by re-running ``rsi`` per bar: the
    history replay calls this thousands of times, and the quadratic version
    dominated the whole scan.
    """
    out: list[float | None] = [None] * len(values)
    if len(values) < period + 1:
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        delta = values[i] - values[i - 1]
        gains += max(delta, 0.0)
        losses += max(-delta, 0.0)
    avg_gain, avg_loss = gains / period, losses / period
    out[period] = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
    for i in range(period + 1, len(values)):
        delta = values[i] - values[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(delta, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-delta, 0.0)) / period
        out[i] = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
    return out


def macd(values: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9):
    fast_e, slow_e = ema_series(values, fast), ema_series(values, slow)
    line = [
        (f - s) if (f is not None and s is not None) else None
        for f, s in zip(fast_e, slow_e)
    ]
    valid = [v for v in line if v is not None]
    sig_valid = ema_series(valid, signal)
    sig: list[float | None] = [None] * (len(line) - len(valid)) + sig_valid
    hist = [
        (l - s) if (l is not None and s is not None) else None for l, s in zip(line, sig)
    ]
    return line, sig, hist


def stdev(values: Sequence[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (n - 1))


def bollinger(values: Sequence[float], period: int = 20, mult: float = 2.0):
    if len(values) < period:
        return None, None, None
    window = values[-period:]
    mid = sum(window) / period
    sd = stdev(window)
    return mid - mult * sd, mid, mid + mult * sd


def atr(bars: Sequence[Bar], period: int = 14) -> float | None:
    if len(bars) < period + 1:
        return None
    trs = []
    for i in range(len(bars) - period, len(bars)):
        prev_close = bars[i - 1].close
        trs.append(
            max(
                bars[i].high - bars[i].low,
                abs(bars[i].high - prev_close),
                abs(bars[i].low - prev_close),
            )
        )
    return sum(trs) / len(trs)


# ------------------------------------------------------------------ detectors
# Each detector takes the full bar history and returns a Detection for the LAST
# bar only, or None. Keeping them one-bar keeps signals de-duplicated naturally.

def _closes(bars: Sequence[Bar]) -> list[float]:
    return [b.close for b in bars]


def detect_golden_cross(bars: Sequence[Bar]) -> Detection | None:
    closes = _closes(bars)
    fast, slow = sma_series(closes, 50), sma_series(closes, 200)
    if len(closes) < 201 or fast[-1] is None or slow[-1] is None:
        return None
    if fast[-2] is None or slow[-2] is None:
        return None
    crossed_up = fast[-2] <= slow[-2] and fast[-1] > slow[-1]
    crossed_down = fast[-2] >= slow[-2] and fast[-1] < slow[-1]
    if not (crossed_up or crossed_down):
        return None
    gap = abs(fast[-1] - slow[-1]) / slow[-1]
    return Detection(
        pattern_key="golden_cross" if crossed_up else "death_cross",
        direction="long" if crossed_up else "short",
        strength=min(0.75, 0.5 + gap * 20),
        horizon_bars=20,
        features={"sma50": round(fast[-1], 4), "sma200": round(slow[-1], 4)},
        description=(
            "50-day crossed above the 200-day" if crossed_up
            else "50-day crossed below the 200-day"
        ),
    )


def detect_rsi_reversal(bars: Sequence[Bar]) -> Detection | None:
    closes = _closes(bars)
    series = rsi_series(closes, 14)
    if len(closes) < 20 or series[-1] is None or series[-2] is None:
        return None
    prev, now = series[-2], series[-1]
    if prev < 30 <= now:
        return Detection(
            "rsi_oversold_reversal", "long",
            strength=min(0.7, 0.45 + (30 - prev) / 60),
            horizon_bars=5,
            features={"rsi_prev": round(prev, 2), "rsi": round(now, 2)},
            description=f"RSI turned up out of oversold ({prev:.0f} -> {now:.0f})",
        )
    if prev > 70 >= now:
        return Detection(
            "rsi_overbought_reversal", "short",
            strength=min(0.7, 0.45 + (prev - 70) / 60),
            horizon_bars=5,
            features={"rsi_prev": round(prev, 2), "rsi": round(now, 2)},
            description=f"RSI rolled over from overbought ({prev:.0f} -> {now:.0f})",
        )
    return None


def detect_breakout(bars: Sequence[Bar], lookback: int = 20) -> Detection | None:
    if len(bars) < lookback + 2:
        return None
    window = bars[-(lookback + 1):-1]
    prior_high = max(b.high for b in window)
    prior_low = min(b.low for b in window)
    last = bars[-1]
    avg_vol = sum(b.volume for b in window) / len(window) or 1.0
    vol_ratio = last.volume / avg_vol
    if last.close > prior_high:
        return Detection(
            "breakout_20d_high", "long",
            strength=min(0.85, 0.45 + min(vol_ratio, 3) * 0.12),
            horizon_bars=10,
            features={
                "prior_high": round(prior_high, 4),
                "close": round(last.close, 4),
                "volume_ratio": round(vol_ratio, 2),
            },
            description=f"Closed above the {lookback}-day high on {vol_ratio:.1f}x volume",
        )
    if last.close < prior_low:
        return Detection(
            "breakdown_20d_low", "short",
            strength=min(0.85, 0.45 + min(vol_ratio, 3) * 0.12),
            horizon_bars=10,
            features={
                "prior_low": round(prior_low, 4),
                "close": round(last.close, 4),
                "volume_ratio": round(vol_ratio, 2),
            },
            description=f"Closed below the {lookback}-day low on {vol_ratio:.1f}x volume",
        )
    return None


def detect_engulfing(bars: Sequence[Bar]) -> Detection | None:
    if len(bars) < 3:
        return None
    prev, last = bars[-2], bars[-1]
    prev_body = abs(prev.close - prev.open)
    last_body = abs(last.close - last.open)
    if prev_body == 0 or last_body < prev_body:
        return None
    bull = (
        prev.close < prev.open
        and last.close > last.open
        and last.close >= prev.open
        and last.open <= prev.close
    )
    bear = (
        prev.close > prev.open
        and last.close < last.open
        and last.close <= prev.open
        and last.open >= prev.close
    )
    if not (bull or bear):
        return None
    ratio = last_body / prev_body
    return Detection(
        "bullish_engulfing" if bull else "bearish_engulfing",
        "long" if bull else "short",
        strength=min(0.65, 0.4 + ratio * 0.08),
        horizon_bars=3,
        features={"body_ratio": round(ratio, 2)},
        description=("Bullish" if bull else "Bearish") + f" engulfing candle ({ratio:.1f}x body)",
    )


def detect_hammer(bars: Sequence[Bar]) -> Detection | None:
    if len(bars) < 6:
        return None
    last = bars[-1]
    rng = last.high - last.low
    if rng <= 0:
        return None
    body = abs(last.close - last.open)
    upper = last.high - max(last.close, last.open)
    lower = min(last.close, last.open) - last.low
    downtrend = bars[-1].close < bars[-5].close
    uptrend = bars[-1].close > bars[-5].close
    if lower > body * 2 and upper < body and downtrend:
        return Detection(
            "hammer", "long",
            strength=min(0.6, 0.38 + lower / rng * 0.3),
            horizon_bars=3,
            features={"lower_wick_pct": round(lower / rng, 3)},
            description="Hammer after a pullback -- sellers rejected the lows",
        )
    if upper > body * 2 and lower < body and uptrend:
        return Detection(
            "shooting_star", "short",
            strength=min(0.6, 0.38 + upper / rng * 0.3),
            horizon_bars=3,
            features={"upper_wick_pct": round(upper / rng, 3)},
            description="Shooting star after a run -- buyers rejected the highs",
        )
    return None


def detect_macd_cross(bars: Sequence[Bar]) -> Detection | None:
    closes = _closes(bars)
    if len(closes) < 40:
        return None
    line, sig, _ = macd(closes)
    if line[-1] is None or sig[-1] is None or line[-2] is None or sig[-2] is None:
        return None
    up = line[-2] <= sig[-2] and line[-1] > sig[-1]
    down = line[-2] >= sig[-2] and line[-1] < sig[-1]
    if not (up or down):
        return None
    return Detection(
        "macd_bull_cross" if up else "macd_bear_cross",
        "long" if up else "short",
        strength=0.52,
        horizon_bars=8,
        features={"macd": round(line[-1], 4), "signal": round(sig[-1], 4)},
        description="MACD crossed " + ("above" if up else "below") + " its signal line",
    )


def detect_squeeze_breakout(bars: Sequence[Bar]) -> Detection | None:
    closes = _closes(bars)
    if len(closes) < 60:
        return None
    lower, mid, upper = bollinger(closes, 20)
    if mid is None or mid == 0:
        return None
    width_now = (upper - lower) / mid
    widths = []
    for i in range(len(closes) - 40, len(closes) - 1):
        # Slice only the 20-bar window rather than the whole history to date --
        # this runs 40 times per detection, on every bar of the replay.
        lo, md, up = bollinger(closes[i - 19 : i + 1], 20)
        if md:
            widths.append((up - lo) / md)
    if not widths:
        return None
    median_width = sorted(widths)[len(widths) // 2]
    was_tight = min(widths[-10:]) < median_width * 0.6
    last = closes[-1]
    if not was_tight:
        return None
    if last > upper:
        direction, key = "long", "squeeze_breakout_up"
    elif last < lower:
        direction, key = "short", "squeeze_breakout_down"
    else:
        return None
    return Detection(
        key, direction,
        strength=min(0.8, 0.5 + (median_width / max(width_now, 1e-6)) * 0.05),
        horizon_bars=10,
        features={"band_width": round(width_now, 4), "median_width": round(median_width, 4)},
        description="Volatility squeeze resolved " + ("upward" if direction == "long" else "downward"),
    )


def detect_gap(bars: Sequence[Bar]) -> Detection | None:
    if len(bars) < 22:
        return None
    prev, last = bars[-2], bars[-1]
    if prev.close <= 0:
        return None
    gap_pct = (last.open - prev.close) / prev.close * 100
    a = atr(bars[:-1], 14)
    threshold = 2.0 if a is None else max(1.5, a / prev.close * 100 * 1.5)
    if gap_pct > threshold and last.close > last.open:
        return Detection(
            "gap_up_hold", "long", strength=min(0.7, 0.45 + gap_pct / 40), horizon_bars=5,
            features={"gap_pct": round(gap_pct, 2)},
            description=f"Gapped up {gap_pct:.1f}% and held the gap",
        )
    if gap_pct < -threshold and last.close < last.open:
        return Detection(
            "gap_down_continue", "short", strength=min(0.7, 0.45 + abs(gap_pct) / 40),
            horizon_bars=5, features={"gap_pct": round(gap_pct, 2)},
            description=f"Gapped down {abs(gap_pct):.1f}% and kept selling off",
        )
    return None


def detect_volume_dryup_reversal(bars: Sequence[Bar]) -> Detection | None:
    """Classic accumulation tell: price grinding sideways as volume dries up."""
    if len(bars) < 30:
        return None
    recent, base = bars[-5:], bars[-30:-5]
    base_vol = sum(b.volume for b in base) / len(base) or 1
    recent_vol = sum(b.volume for b in recent) / len(recent)
    if recent_vol > base_vol * 0.55:
        return None
    highs = [b.high for b in recent]
    lows = [b.low for b in recent]
    tight = (max(highs) - min(lows)) / max(min(lows), 1e-9) < 0.05
    above_trend = bars[-1].close > (sma(_closes(bars), 50) or float("inf"))
    if tight and above_trend:
        return Detection(
            "volume_dryup_base", "long",
            strength=0.55, horizon_bars=10,
            features={"vol_ratio": round(recent_vol / base_vol, 2)},
            description="Tight base on drying volume above the 50-day",
        )
    return None


DETECTORS: list[Callable[[Sequence[Bar]], Detection | None]] = [
    detect_golden_cross,
    detect_rsi_reversal,
    detect_breakout,
    detect_engulfing,
    detect_hammer,
    detect_macd_cross,
    detect_squeeze_breakout,
    detect_gap,
    detect_volume_dryup_reversal,
]

PATTERN_KEYS = [
    "golden_cross", "death_cross", "rsi_oversold_reversal", "rsi_overbought_reversal",
    "breakout_20d_high", "breakdown_20d_low", "bullish_engulfing", "bearish_engulfing",
    "hammer", "shooting_star", "macd_bull_cross", "macd_bear_cross",
    "squeeze_breakout_up", "squeeze_breakout_down", "gap_up_hold", "gap_down_continue",
    "volume_dryup_base",
]


def scan_bars(bars: Sequence[Bar]) -> list[Detection]:
    """Run every detector over one symbol's history."""
    out: list[Detection] = []
    for detector in DETECTORS:
        try:
            hit = detector(bars)
        except Exception:
            # A single bad series must never take down a 500-symbol scan.
            continue
        if hit:
            out.append(hit)
    return out
