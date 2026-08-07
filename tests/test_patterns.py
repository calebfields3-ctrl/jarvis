"""Indicators and pattern detectors, against hand-built bar sequences."""

from __future__ import annotations

import pytest

from jarvis.market.patterns import (
    Bar,
    bollinger,
    detect_breakout,
    detect_engulfing,
    detect_golden_cross,
    detect_hammer,
    detect_rsi_reversal,
    ema_series,
    rsi,
    rsi_series,
    scan_bars,
    sma,
    sma_series,
)


def bar(close, *, open=None, high=None, low=None, volume=1_000_000, ts="2025-01-01"):
    open = close if open is None else open
    high = max(open, close) if high is None else high
    low = min(open, close) if low is None else low
    return Bar(ts=ts, open=open, high=high, low=low, close=close, volume=volume)


def series(closes, volume=1_000_000):
    return [bar(c, volume=volume, ts=f"2025-01-{i % 28 + 1:02d}") for i, c in enumerate(closes)]


# ------------------------------------------------------------------ indicators
def test_sma_basic():
    assert sma([1, 2, 3, 4, 5], 5) == 3
    assert sma([1, 2], 5) is None


def test_sma_series_matches_pointwise_sma():
    values = [float(v) for v in range(1, 40)]
    computed = sma_series(values, 10)
    for i in range(9, len(values)):
        assert computed[i] == pytest.approx(sma(values[: i + 1], 10))


def test_rsi_all_gains_is_100():
    assert rsi([float(i) for i in range(1, 30)], 14) == 100.0


def test_rsi_all_losses_is_zero():
    assert rsi([float(i) for i in range(30, 1, -1)], 14) == pytest.approx(0.0)


def test_rsi_flat_series_is_neutral_ish():
    value = rsi([100.0] * 30, 14)
    assert value == 100.0  # no losses at all -> RSI is defined as 100


def test_rsi_series_equals_repeated_rsi():
    """The incremental series must match the direct calculation exactly."""
    values = [100 + (i * 7 % 13) - 6 for i in range(60)]
    fast = rsi_series(values, 14)
    for i in range(14, len(values)):
        assert fast[i] == pytest.approx(rsi(values[: i + 1], 14))


def test_ema_seeds_from_sma():
    values = [float(i) for i in range(1, 30)]
    ema = ema_series(values, 10)
    assert ema[8] is None
    assert ema[9] == pytest.approx(sum(values[:10]) / 10)


def test_bollinger_widths():
    lower, mid, upper = bollinger([10.0] * 20, 20)
    assert mid == pytest.approx(10.0)
    assert lower == upper == pytest.approx(10.0)  # zero variance


# ------------------------------------------------------------------- detectors
def test_golden_cross_fires_on_the_crossing_bar():
    # 200 bars falling, then a sharp rally that drags the 50 above the 200.
    closes = [200.0 - i * 0.4 for i in range(200)] + [
        120.0 + i * 3.0 for i in range(60)
    ]
    bars = series(closes)
    detection = None
    for end in range(201, len(bars) + 1):
        hit = detect_golden_cross(bars[:end])
        if hit:
            detection = hit
            break
    assert detection is not None
    assert detection.pattern_key == "golden_cross"
    assert detection.direction == "long"


def test_no_cross_no_detection():
    bars = series([100.0 + i * 0.1 for i in range(260)])
    assert detect_golden_cross(bars) is None


def test_breakout_needs_a_close_above_the_prior_high():
    closes = [100.0] * 21 + [105.0]
    bars = series(closes)
    detection = detect_breakout(bars)
    assert detection is not None
    assert detection.pattern_key == "breakout_20d_high"
    assert detection.direction == "long"
    assert detection.features["prior_high"] == pytest.approx(100.0)


def test_breakdown_detected():
    bars = series([100.0] * 21 + [92.0])
    detection = detect_breakout(bars)
    assert detection.pattern_key == "breakdown_20d_low"
    assert detection.direction == "short"


def test_breakout_strength_rises_with_volume():
    quiet = series([100.0] * 21 + [105.0], volume=1_000_000)
    loud = [*series([100.0] * 21, volume=1_000_000), bar(105.0, volume=4_000_000)]
    assert detect_breakout(loud).strength > detect_breakout(quiet).strength


def test_inside_the_range_is_not_a_breakout():
    bars = series([100.0, 105.0] * 11 + [102.0])
    assert detect_breakout(bars) is None


def test_bullish_engulfing():
    bars = series([100.0] * 3)
    bars.append(bar(95.0, open=100.0, high=100.5, low=94.5))   # down candle
    bars.append(bar(102.0, open=94.0, high=102.5, low=93.5))   # engulfs it
    detection = detect_engulfing(bars)
    assert detection.pattern_key == "bullish_engulfing"
    assert detection.direction == "long"


def test_bearish_engulfing():
    bars = series([100.0] * 3)
    bars.append(bar(105.0, open=100.0, high=105.5, low=99.5))
    bars.append(bar(98.0, open=106.0, high=106.5, low=97.5))
    detection = detect_engulfing(bars)
    assert detection.pattern_key == "bearish_engulfing"
    assert detection.direction == "short"


def test_smaller_body_does_not_engulf():
    bars = series([100.0] * 3)
    bars.append(bar(95.0, open=100.0))
    bars.append(bar(96.0, open=95.5))
    assert detect_engulfing(bars) is None


def test_hammer_requires_a_prior_decline():
    # Long lower wick, small body, negligible upper wick.
    hammer_bar = bar(100.5, open=100.0, high=100.7, low=94.0)

    falling = series([110.0, 108.0, 106.0, 104.0, 102.0])
    falling.append(hammer_bar)
    detection = detect_hammer(falling)
    assert detection is not None
    assert detection.pattern_key == "hammer"

    # The identical candle after a rally is not a hammer -- context is the point.
    rising = series([90.0, 92.0, 94.0, 96.0, 98.0])
    rising.append(hammer_bar)
    after_rally = detect_hammer(rising)
    assert after_rally is None or after_rally.pattern_key != "hammer"


def test_rsi_reversal_fires_crossing_out_of_oversold():
    # Sustained decline drives RSI under 30, then a real bounce lifts it back.
    closes = [100.0 - i * 1.5 for i in range(30)] + [56.0 + i * 2.5 for i in range(1, 12)]
    bars = series(closes)
    detection = next(
        (
            hit
            for end in range(32, len(bars) + 1)
            if (hit := detect_rsi_reversal(bars[:end]))
        ),
        None,
    )
    assert detection is not None
    assert detection.pattern_key == "rsi_oversold_reversal"
    assert detection.direction == "long"


# ---------------------------------------------------------------------- scan
def test_scan_bars_returns_detections_and_never_raises():
    bars = series([100.0] * 21 + [110.0])
    detections = scan_bars(bars)
    assert any(d.pattern_key == "breakout_20d_high" for d in detections)


def test_scan_bars_tolerates_degenerate_input():
    assert scan_bars([]) == []
    assert scan_bars(series([0.01] * 5)) == []


def test_detections_carry_a_readable_description():
    for detection in scan_bars(series([100.0] * 21 + [110.0])):
        assert detection.description
        assert 0.0 <= detection.strength <= 1.0
        assert detection.direction in {"long", "short"}
        assert detection.horizon_bars > 0
