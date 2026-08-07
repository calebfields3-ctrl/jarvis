"""Session clock, intraday detectors, risk sizing and the PDT rule."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from jarvis.market.intraday import (
    detect_gap_and_go,
    detect_opening_range_break,
    detect_vwap_reclaim,
    opening_range,
    relative_volume,
    scan_intraday,
    session_bars,
    vwap_series,
)
from jarvis.market.patterns import Bar
from jarvis.market.session import (
    EASTERN,
    Phase,
    SessionClock,
    is_trading_day,
    minutes_into_session,
    minutes_to_close,
    parse_bar_time,
    phase_at,
    regular_hours_only,
)
from jarvis.portfolio.risk import (
    PDT_EQUITY_MINIMUM,
    RiskManager,
    RiskProfile,
)

# A known Monday, so weekday assumptions in the fixtures hold.
MONDAY = date(2026, 8, 3)


def ibar(day: date, hh: int, mm: int, close: float, *, open=None, high=None,
         low=None, volume=100_000) -> Bar:
    open = close if open is None else open
    high = max(open, close) if high is None else high
    low = min(open, close) if low is None else low
    return Bar(
        ts=f"{day.isoformat()} {hh:02d}:{mm:02d}:00",
        open=open, high=high, low=low, close=close, volume=volume,
    )


def session(day: date, closes, *, start=(9, 30), step=5, volume=100_000) -> list[Bar]:
    """Build a run of intraday bars from a list of closes."""
    bars = []
    moment = datetime(day.year, day.month, day.day, start[0], start[1], tzinfo=EASTERN)
    for close in closes:
        bars.append(ibar(day, moment.hour, moment.minute, close, volume=volume))
        moment += timedelta(minutes=step)
    return bars


# ------------------------------------------------------------------- session
@pytest.mark.parametrize(
    "hh,mm,expected",
    [
        (3, 0, Phase.CLOSED),
        (7, 0, Phase.PREMARKET),
        (9, 45, Phase.OPENING),
        (11, 0, Phase.MORNING),
        (13, 0, Phase.MIDDAY),
        (14, 30, Phase.AFTERNOON),
        (15, 30, Phase.POWER_HOUR),
        (17, 0, Phase.AFTERHOURS),
        (21, 0, Phase.CLOSED),
    ],
)
def test_phase_boundaries(hh, mm, expected):
    assert phase_at(f"{MONDAY.isoformat()} {hh:02d}:{mm:02d}:00") is expected


def test_weekends_are_closed():
    saturday = date(2026, 8, 8)
    assert phase_at(f"{saturday.isoformat()} 11:00:00") is Phase.CLOSED


def test_holidays_are_not_trading_days():
    assert not is_trading_day(date(2026, 1, 1))      # New Year's Day, a Thursday
    assert not is_trading_day(date(2026, 12, 25))    # Christmas, a Friday
    assert is_trading_day(MONDAY)


def test_weekend_holidays_close_the_observed_weekday():
    """4 July 2026 is a Saturday, so the market closes Friday the 3rd."""
    assert date(2026, 7, 4).weekday() == 5
    assert not is_trading_day(date(2026, 7, 3))


def test_midday_is_not_tradeable_but_is_regular_hours():
    assert Phase.MIDDAY.is_regular_hours
    assert not Phase.MIDDAY.is_tradeable
    assert Phase.OPENING.is_tradeable


def test_minutes_into_and_to_close():
    assert minutes_into_session(f"{MONDAY} 10:00:00") == 30
    assert minutes_to_close(f"{MONDAY} 15:30:00") == 30
    assert minutes_into_session(f"{MONDAY} 06:00:00") is None


def test_daily_timestamps_anchor_to_the_close():
    parsed = parse_bar_time("2026-08-03")
    assert (parsed.hour, parsed.minute) == (16, 0)


def test_bad_timestamp_raises():
    with pytest.raises(ValueError):
        parse_bar_time("not a date")


def test_regular_hours_filter_drops_extended_prints():
    bars = [
        ibar(MONDAY, 7, 0, 100.0),     # pre-market
        ibar(MONDAY, 10, 0, 101.0),
        ibar(MONDAY, 18, 0, 102.0),    # after hours
    ]
    kept = regular_hours_only(bars)
    assert len(kept) == 1
    assert kept[0].close == 101.0


def test_clock_blocks_new_trades_near_the_bell():
    assert not SessionClock.at(f"{MONDAY} 15:55:00").can_open_new_trades
    assert SessionClock.at(f"{MONDAY} 10:00:00").can_open_new_trades
    assert not SessionClock.at(f"{MONDAY} 13:00:00").can_open_new_trades  # midday


# --------------------------------------------------------------------- VWAP
def test_vwap_is_the_volume_weighted_average():
    bars = [
        ibar(MONDAY, 9, 30, 10.0, high=10.0, low=10.0, volume=100),
        ibar(MONDAY, 9, 35, 20.0, high=20.0, low=20.0, volume=300),
    ]
    vwaps = vwap_series(bars)
    assert vwaps[0] == pytest.approx(10.0)
    # (10*100 + 20*300) / 400 = 17.5
    assert vwaps[-1] == pytest.approx(17.5)


def test_vwap_resets_each_session():
    """A VWAP carried across days is not the level anyone benchmarks against."""
    tuesday = MONDAY + timedelta(days=1)
    bars = [
        ibar(MONDAY, 9, 30, 100.0, high=100.0, low=100.0, volume=1000),
        ibar(MONDAY, 15, 55, 100.0, high=100.0, low=100.0, volume=1000),
        ibar(tuesday, 9, 30, 50.0, high=50.0, low=50.0, volume=1000),
    ]
    vwaps = vwap_series(bars)
    assert vwaps[1] == pytest.approx(100.0)
    assert vwaps[2] == pytest.approx(50.0)   # not dragged toward 100


def test_opening_range_covers_only_the_first_30_minutes():
    bars = session(MONDAY, [10, 12, 9, 11, 10, 10, 25, 5])  # 5-min bars
    high, low = opening_range(bars)
    # First 30 minutes = bars at 09:30..09:55 -> six bars, closes 10,12,9,11,10,10
    assert high == pytest.approx(12.0)
    assert low == pytest.approx(9.0)


def test_relative_volume_compares_the_same_point_of_day():
    yesterday = MONDAY - timedelta(days=1) if is_trading_day(MONDAY - timedelta(days=1)) else MONDAY - timedelta(days=3)
    bars = session(yesterday, [100.0] * 12, volume=1000) + session(MONDAY, [100.0] * 12, volume=3000)
    rvol = relative_volume(bars)
    assert rvol == pytest.approx(3.0, rel=0.01)


# ---------------------------------------------------------------- detectors
def _opening_range_setup(breakout_close: float, *, high: float, low: float) -> list[Bar]:
    """Six bars build a 100-102 opening range, then three bars after it."""
    bars = session(MONDAY, [100.0, 101.0, 102.0, 101.0, 100.5, 101.0])  # 09:30-09:55
    bars.append(ibar(MONDAY, 10, 0, 101.0))
    bars.append(ibar(MONDAY, 10, 5, 101.0))
    bars.append(
        ibar(MONDAY, 10, 10, breakout_close, open=101.0, high=high, low=low,
             volume=400_000)
    )
    return bars


def test_opening_range_breakout_fires_after_the_range():
    signal = detect_opening_range_break(
        _opening_range_setup(105.0, high=105.5, low=101.0)
    )
    assert signal is not None
    assert signal.pattern_key == "opening_range_breakout"
    assert signal.direction == "long"
    assert signal.stop < signal.entry < signal.target


def test_opening_range_breakdown_fires():
    signal = detect_opening_range_break(
        _opening_range_setup(96.0, high=101.0, low=95.5)
    )
    assert signal is not None
    assert signal.pattern_key == "opening_range_breakdown"
    assert signal.direction == "short"
    assert signal.stop > signal.entry > signal.target


def test_no_break_no_signal():
    bars = session(MONDAY, [100.0, 101.0, 102.0, 101.0, 100.5, 101.0, 101.2])
    assert detect_opening_range_break(bars) is None


def test_opening_range_break_ignored_during_the_range_itself():
    """Inside the first 30 minutes there is no range to break yet."""
    bars = session(MONDAY, [100.0, 101.0, 105.0])
    assert detect_opening_range_break(bars) is None


def test_opening_range_break_expires_in_the_afternoon():
    bars = session(MONDAY, [100.0] * 6)
    bars.append(ibar(MONDAY, 14, 30, 120.0, volume=500_000))
    assert detect_opening_range_break(bars) is None


def test_vwap_reclaim_requires_a_real_cross():
    # Slide well below VWAP, then push decisively back above it in one bar.
    closes = [100, 98, 96, 94, 92, 90, 88, 86, 84, 82, 80, 95]
    bars = session(MONDAY, [float(c) for c in closes])
    signal = detect_vwap_reclaim(bars)
    assert signal is not None
    assert signal.pattern_key == "vwap_reclaim"
    assert signal.direction == "long"


def test_marginal_vwap_touch_is_not_a_reclaim():
    closes = [100.0] * 11 + [100.001]
    bars = session(MONDAY, closes)
    assert detect_vwap_reclaim(bars) is None


def test_gap_and_go_needs_a_real_gap():
    prior = MONDAY - timedelta(days=3)  # previous Friday
    yesterday = session(prior, [100.0] * 10)
    today = session(MONDAY, [110.0, 111.0, 112.0, 113.0])
    signal = detect_gap_and_go(yesterday + today)
    assert signal is not None
    assert signal.pattern_key == "gap_and_go_long"
    assert signal.features["gap_pct"] == pytest.approx(10.0, rel=0.01)


def test_no_gap_no_gap_and_go():
    prior = MONDAY - timedelta(days=3)
    bars = session(prior, [100.0] * 10) + session(MONDAY, [100.2, 100.3, 100.4, 100.5])
    assert detect_gap_and_go(bars) is None


def test_signals_carry_a_stop_and_target():
    prior = MONDAY - timedelta(days=3)
    bars = session(prior, [100.0] * 10) + session(MONDAY, [110.0, 111.0, 112.0, 113.0])
    for signal in scan_intraday(bars, require_actionable=False):
        assert signal.risk_per_share > 0
        assert signal.entry > 0
        assert signal.direction in {"long", "short"}


def test_scan_filters_out_poor_reward_risk():
    """A setup risking more than it can make is not offered."""
    prior = MONDAY - timedelta(days=3)
    bars = session(prior, [100.0] * 10) + session(MONDAY, [110.0, 111.0, 112.0, 113.0])
    strict = scan_intraday(bars, require_actionable=True)
    assert all(s.reward_risk >= 1.5 for s in strict)


def test_scan_never_raises_on_junk():
    assert scan_intraday([]) == []
    assert scan_intraday(session(MONDAY, [1.0, 1.0])) == []


# -------------------------------------------------------------------- risk
@pytest.fixture
def risk(memory) -> RiskManager:
    return RiskManager(memory.db, RiskProfile())


def test_position_size_derives_from_the_stop(risk):
    plan = risk.plan_trade("AAPL", "long", 50.0, 48.0, 56.0, equity=30_000)
    # 1% would be $300; default profile risks 0.5% = $150 / $2 = 75 shares.
    assert plan.shares == 75
    assert plan.risk_amount == pytest.approx(150.0)
    assert plan.reward_risk == pytest.approx(3.0)
    assert plan.is_viable


def test_wider_stop_means_smaller_position(risk):
    # Priced low enough that the 20% position cap does not bind either plan,
    # so the comparison isolates the effect of stop width alone.
    tight = risk.plan_trade("AAPL", "long", 20.0, 19.0, 24.0, equity=30_000)
    wide = risk.plan_trade("AAPL", "long", 20.0, 15.0, 40.0, equity=30_000)
    assert wide.shares < tight.shares
    # Neither plan was shrunk by the position cap, so the difference is the stop.
    assert not any("capped" in w for w in tight.warnings + wide.warnings)
    # Dollar risk is held constant; that is the whole point.
    assert wide.risk_amount == pytest.approx(tight.risk_amount, rel=0.05)


def test_long_stop_must_sit_below_entry(risk):
    plan = risk.plan_trade("AAPL", "long", 50.0, 52.0, 60.0, equity=30_000)
    assert not plan.is_viable
    assert "below the entry" in plan.rejected_reason


def test_short_stop_must_sit_above_entry(risk):
    plan = risk.plan_trade("AAPL", "short", 50.0, 48.0, 40.0, equity=30_000)
    assert not plan.is_viable
    assert "above the entry" in plan.rejected_reason


def test_poor_reward_risk_is_rejected(risk):
    plan = risk.plan_trade("AAPL", "long", 50.0, 48.0, 51.0, equity=30_000)
    assert not plan.is_viable
    assert "below your" in plan.rejected_reason


def test_position_cap_limits_size(risk):
    # A tight stop would otherwise buy far more than 20% of the account.
    plan = risk.plan_trade("AAPL", "long", 50.0, 49.9, 60.0, equity=30_000)
    assert plan.position_value <= 30_000 * 0.20 + 50
    assert any("capped" in w for w in plan.warnings)


def test_unaffordable_share_is_rejected(risk):
    plan = risk.plan_trade("BRK-A", "long", 700_000.0, 690_000.0, 750_000.0, equity=30_000)
    assert not plan.is_viable


def test_wide_stop_earns_a_warning(risk):
    plan = risk.plan_trade("AAPL", "long", 100.0, 90.0, 140.0, equity=100_000)
    assert any("wide" in w for w in plan.warnings)


# --------------------------------------------------------------- PDT rule
def _round_trip(tracker, symbol, when, *, entry=100.0, exit_=101.0, qty=10):
    tracker.record_trade(symbol, "buy", qty, entry, executed_at=when)
    tracker.record_trade(symbol, "sell", qty, exit_, executed_at=when + timedelta(hours=1))


def test_day_trades_are_counted(memory, risk):
    from jarvis.portfolio.tracker import PortfolioTracker

    tracker = PortfolioTracker(memory.db)
    when = datetime(MONDAY.year, MONDAY.month, MONDAY.day, 10, 0, tzinfo=EASTERN)
    for symbol in ("AAPL", "MSFT", "NVDA"):
        _round_trip(tracker, symbol, when)

    count, _ = risk.count_day_trades(as_of=MONDAY)
    assert count == 3


def test_positions_held_overnight_are_not_day_trades(memory, risk):
    from jarvis.portfolio.tracker import PortfolioTracker

    tracker = PortfolioTracker(memory.db)
    monday = datetime(MONDAY.year, MONDAY.month, MONDAY.day, 10, 0, tzinfo=EASTERN)
    tracker.record_trade("AAPL", "buy", 10, 100.0, executed_at=monday)
    tracker.record_trade("AAPL", "sell", 10, 101.0, executed_at=monday + timedelta(days=1))

    count, _ = risk.count_day_trades(as_of=MONDAY + timedelta(days=1))
    assert count == 0


def test_pdt_blocks_under_the_equity_minimum(memory, risk):
    from jarvis.portfolio.tracker import PortfolioTracker

    tracker = PortfolioTracker(memory.db)
    when = datetime(MONDAY.year, MONDAY.month, MONDAY.day, 10, 0, tzinfo=EASTERN)
    for symbol in ("AAPL", "MSFT", "NVDA"):
        _round_trip(tracker, symbol, when)

    status = risk.status(equity=20_000, as_of=MONDAY)
    assert status.is_pdt_restricted
    assert status.day_trades_remaining == 0
    assert not status.can_trade
    assert any("pattern day trader" in r for r in status.blocked_reasons)


def test_pdt_does_not_apply_above_the_minimum(memory, risk):
    from jarvis.portfolio.tracker import PortfolioTracker

    tracker = PortfolioTracker(memory.db)
    when = datetime(MONDAY.year, MONDAY.month, MONDAY.day, 10, 0, tzinfo=EASTERN)
    for symbol in ("AAPL", "MSFT", "NVDA", "TSLA", "AMD"):
        _round_trip(tracker, symbol, when)

    status = risk.status(equity=PDT_EQUITY_MINIMUM + 1, as_of=MONDAY)
    assert not status.is_pdt_restricted
    assert status.day_trades_remaining is None


def test_unfunded_account_does_not_report_a_blown_loss_limit(memory, risk):
    """0.0 <= -0.0 is True in Python; a flat empty account must not be blocked."""
    status = risk.status(equity=0.0, as_of=MONDAY)
    assert not any("loss limit" in r for r in status.blocked_reasons)


def test_daily_loss_limit_blocks(memory, risk):
    from jarvis.portfolio.tracker import PortfolioTracker

    tracker = PortfolioTracker(memory.db)
    when = datetime(MONDAY.year, MONDAY.month, MONDAY.day, 10, 0, tzinfo=EASTERN)
    # Lose $1,000 on a $30,000 account against a 2% ($600) limit.
    _round_trip(tracker, "AAPL", when, entry=100.0, exit_=90.0, qty=100)

    status = risk.status(equity=30_000, as_of=MONDAY)
    assert status.realized_today == pytest.approx(-1000.0)
    assert any("loss limit" in r for r in status.blocked_reasons)


def test_consecutive_losses_block(memory):
    from jarvis.portfolio.tracker import PortfolioTracker

    manager = RiskManager(memory.db, RiskProfile(max_consecutive_losses=3))
    tracker = PortfolioTracker(memory.db)
    when = datetime(MONDAY.year, MONDAY.month, MONDAY.day, 10, 0, tzinfo=EASTERN)
    for index, symbol in enumerate(("AAPL", "MSFT", "NVDA")):
        _round_trip(
            tracker, symbol, when + timedelta(minutes=index * 90),
            entry=100.0, exit_=99.0, qty=1,
        )
    status = manager.status(equity=100_000, as_of=MONDAY)
    assert status.consecutive_losses >= 3
    assert any("in a row" in r for r in status.blocked_reasons)


def test_closed_trades_are_matched_fifo(memory, risk):
    from jarvis.portfolio.tracker import PortfolioTracker

    tracker = PortfolioTracker(memory.db)
    when = datetime(MONDAY.year, MONDAY.month, MONDAY.day, 10, 0, tzinfo=EASTERN)
    tracker.record_trade("AAPL", "buy", 10, 100.0, executed_at=when)
    tracker.record_trade("AAPL", "buy", 10, 110.0, executed_at=when + timedelta(minutes=5))
    tracker.record_trade("AAPL", "sell", 10, 120.0, executed_at=when + timedelta(minutes=10))

    closed = risk.closed_trades()
    assert len(closed) == 1
    assert closed[0]["entry"] == pytest.approx(100.0)   # first lot out first
    assert closed[0]["pnl"] == pytest.approx(200.0)
