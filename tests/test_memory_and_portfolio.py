"""Persistence and portfolio arithmetic -- the numbers in the greeting."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from jarvis.memory.store import Memory
from jarvis.portfolio.tracker import PortfolioTracker, market_now


# ------------------------------------------------------------------- memory
def test_memory_survives_restart(home):
    path = home / "persist.db"
    first = Memory(path)
    first.profile.name = "Caleb"
    first.profile.set("timezone", "America/New_York")
    session = first.sessions.start()
    first.sessions.record(session, "owner", "hey jarvis")
    first.sessions.end(session, "first chat")
    first.close()

    second = Memory(path)
    assert second.profile.name == "Caleb"
    assert second.profile.get("timezone") == "America/New_York"
    assert second.sessions.count() == 1
    assert second.sessions.last_session()["summary"] == "first chat"
    second.close()


def test_profile_defaults_when_unset(memory):
    assert memory.profile.name == "there"
    memory.profile.name = "Caleb"
    assert memory.profile.name == "Caleb"


def test_session_transcript_is_ordered(memory):
    session = memory.sessions.start()
    memory.sessions.record(session, "owner", "first")
    memory.sessions.record(session, "jarvis", "second")
    memory.sessions.record(session, "owner", "third")
    turns = memory.sessions.recent_utterances(session)
    assert [t["text"] for t in turns] == ["first", "second", "third"]


# ---------------------------------------------------------------- portfolio
@pytest.fixture
def tracker(memory) -> PortfolioTracker:
    return PortfolioTracker(memory.db)


def test_average_cost_and_cash(tracker):
    tracker.record_cash(10_000)
    tracker.record_trade("AAPL", "buy", 10, 100.0)
    tracker.record_trade("AAPL", "buy", 10, 120.0)

    position = next(p for p in tracker.positions() if p.symbol == "AAPL")
    assert position.quantity == 20
    assert position.avg_cost == pytest.approx(110.0)
    assert tracker.cash() == pytest.approx(10_000 - 2_200)


def test_fees_fold_into_cost_basis(tracker):
    tracker.record_cash(5_000)
    tracker.record_trade("MSFT", "buy", 10, 100.0, fees=10.0)
    position = next(p for p in tracker.positions() if p.symbol == "MSFT")
    assert position.avg_cost == pytest.approx(101.0)
    assert tracker.cash() == pytest.approx(5_000 - 1_010)


def test_realized_pnl_booked_on_sell(tracker):
    tracker.record_cash(10_000)
    tracker.record_trade("NVDA", "buy", 10, 100.0)
    tracker.record_trade("NVDA", "sell", 4, 150.0)

    position = next(p for p in tracker.positions() if p.symbol == "NVDA")
    assert position.quantity == 6
    assert position.realized_pnl == pytest.approx(200.0)
    assert position.avg_cost == pytest.approx(100.0)


def test_selling_everything_closes_the_position(tracker):
    tracker.record_cash(10_000)
    tracker.record_trade("TSLA", "buy", 5, 200.0)
    tracker.record_trade("TSLA", "sell", 5, 250.0)
    held = [p for p in tracker.positions() if p.quantity > 0]
    assert held == []
    assert tracker.cash() == pytest.approx(10_000 - 1_000 + 1_250)


def test_snapshot_marks_to_market(tracker):
    tracker.record_cash(10_000)
    tracker.record_trade("AAPL", "buy", 10, 100.0)
    snap = tracker.snapshot(price_lookup=lambda syms: {"AAPL": 150.0})

    assert snap.positions_value == pytest.approx(1_500.0)
    assert snap.total_value == pytest.approx(9_000 + 1_500)
    assert snap.open_unrealized == pytest.approx(500.0)
    assert snap.stale_prices == []


def test_unpriced_positions_are_flagged_not_guessed(tracker):
    tracker.record_cash(10_000)
    tracker.record_trade("ZZZZ", "buy", 10, 100.0)
    snap = tracker.snapshot(price_lookup=lambda syms: {})
    assert snap.stale_prices == ["ZZZZ"]
    # Falls back to cost basis rather than inventing a price.
    assert snap.positions_value == pytest.approx(1_000.0)


def test_previous_day_pnl(tracker):
    tracker.record_cash(10_000)
    tracker.record_trade("AAPL", "buy", 10, 100.0)

    # Market time, not the system clock -- the ledger is stamped in Eastern, so
    # a UTC "yesterday" can still be today in the market's calendar.
    yesterday = market_now().date() - timedelta(days=1)
    prior = tracker.snapshot(price_lookup=lambda s: {"AAPL": 100.0})
    tracker.close_day(prior, as_of_date=yesterday)

    today = tracker.snapshot(price_lookup=lambda s: {"AAPL": 110.0})
    assert today.day_pnl == pytest.approx(100.0)
    assert today.day_pnl_pct == pytest.approx(1.0, abs=0.01)
    assert today.prior_date == yesterday.isoformat()


def test_close_day_is_idempotent(tracker):
    tracker.record_cash(1_000)
    snap = tracker.snapshot(price_lookup=lambda s: {})
    tracker.close_day(snap)
    tracker.close_day(snap)
    assert len(tracker.history(10)) == 1


def test_bad_trade_input_rejected(tracker):
    with pytest.raises(ValueError):
        tracker.record_trade("AAPL", "hodl", 1, 100.0)
    with pytest.raises(ValueError):
        tracker.record_trade("AAPL", "buy", -1, 100.0)
