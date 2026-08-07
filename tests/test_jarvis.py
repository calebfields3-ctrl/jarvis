"""End-to-end behaviour: waking, greeting, scanning, remembering."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from jarvis.brain import Jarvis
from jarvis.market.provider import SyntheticProvider
from jarvis.voice.io import PhraseWake, TextListener


# ------------------------------------------------------------------ boot
def test_boots_with_foundations_and_no_expertise(jarvis):
    stats = jarvis.memory.knowledge.stats()
    assert stats["total"] > 40
    assert stats["by_tier"]["foundation"] > 30
    label, _ = jarvis.memory.knowledge.expertise_level()
    assert label == "novice"


def test_starts_knowing_what_a_stock_is(jarvis):
    answer = jarvis.ask("what is a stock")
    assert "ownership" in answer.lower() or "claim" in answer.lower()


def test_does_not_start_with_deep_expertise(jarvis):
    assert jarvis.memory.knowledge.lessons(tier="expertise") == []


def test_watchlist_exceeds_five_hundred(jarvis):
    assert len(jarvis.universe) >= 500


# ------------------------------------------------------------------ waking
def test_greeting_uses_the_owners_name(jarvis):
    assert "Caleb" in jarvis.wake()


def test_greeting_reports_an_empty_portfolio_honestly(jarvis):
    assert "empty" in jarvis.wake().lower()


def test_greeting_includes_previous_day_pnl(jarvis):
    jarvis.portfolio.record_cash(10_000)
    jarvis.portfolio.record_trade("AAPL", "buy", 10, 100.0)

    prior = jarvis.portfolio.snapshot(price_lookup=lambda s: {"AAPL": 100.0})
    jarvis.portfolio.close_day(prior, as_of_date=date.today() - timedelta(days=1))

    jarvis.provider = SyntheticProvider(seed=1)
    monkey_price = {"AAPL": 120.0}
    jarvis.provider.prices = lambda syms: monkey_price  # type: ignore[method-assign]

    greeting = jarvis.wake()
    assert "up $200.00" in greeting
    assert "Portfolio:" in greeting


def test_wake_records_a_session_that_persists(jarvis, config):
    jarvis.wake()
    jarvis.sleep("done")
    jarvis.memory.close()

    revived = Jarvis(config, provider=SyntheticProvider())
    try:
        assert revived.memory.sessions.count() >= 1
        assert revived.memory.profile.name == "Caleb"
    finally:
        revived.memory.close()


def test_second_session_notes_the_gap(jarvis):
    jarvis.wake()
    jarvis.sleep()
    second = jarvis.wake()
    assert "first session" not in second


# ------------------------------------------------------------------ memory
def test_remembers_a_name_change_across_restarts(jarvis, config):
    jarvis.ask("call me Cal")
    assert jarvis.memory.profile.name == "Cal"
    jarvis.memory.close()

    revived = Jarvis(config, provider=SyntheticProvider())
    try:
        assert "Cal" in revived.ask("what is my name")
    finally:
        revived.memory.close()


def test_conversation_is_written_to_memory(jarvis):
    jarvis.wake()
    jarvis.ask("how is my portfolio doing")
    turns = jarvis.memory.sessions.recent_utterances(jarvis.session_id, limit=50)
    assert any(t["role"] == "owner" for t in turns)
    assert any(t["role"] == "jarvis" for t in turns)


def test_trading_history_persists(jarvis, config):
    jarvis.portfolio.record_cash(5_000)
    jarvis.portfolio.record_trade("MSFT", "buy", 5, 300.0, note="test")
    jarvis.memory.close()

    revived = Jarvis(config, provider=SyntheticProvider())
    try:
        symbols = {p.symbol for p in revived.portfolio.positions()}
        assert "MSFT" in symbols
        assert revived.portfolio.recent_trades(5)[0]["note"] == "test"
    finally:
        revived.memory.close()


# ------------------------------------------------------------------ scanning
def test_scan_records_signals_for_later_grading(jarvis):
    report = jarvis.scan_market(["AAPL", "MSFT", "NVDA", "TSLA", "AMD", "META"])
    assert report.symbols_watched == 6
    assert report.symbols_with_data == 6
    assert len(jarvis.memory.signals.recent(limit=100)) == report.recorded


def test_scan_survives_tradingview_being_unavailable(jarvis):
    jarvis.scanner.scan = lambda symbols, interval="1d": {}  # type: ignore[method-assign]
    jarvis.scanner.last_error = "connection refused"
    report = jarvis.scan_market(["AAPL", "MSFT"])
    assert report.tradingview_snapshots == 0
    assert any("TradingView" in note for note in report.notes)
    # The local engine still did its job.
    assert report.symbols_with_data == 2


def test_scan_briefing_is_readable(jarvis):
    briefing = jarvis.scan_briefing(jarvis.scan_market(["AAPL", "MSFT", "NVDA"]))
    assert "Watched 3 charts" in briefing


def test_untested_patterns_are_labelled_as_hypotheses(jarvis):
    briefing = jarvis.scan_briefing(jarvis.scan_market(["AAPL", "MSFT", "NVDA", "AMD"]))
    if "Highest-conviction setups" in briefing:
        assert "untested" in briefing or "graded" in briefing


def test_empty_watchlist_is_reported_not_crashed(jarvis):
    report = jarvis.scan_market([])
    assert report.symbols_watched == 0
    assert any("empty" in n for n in report.notes)


# --------------------------------------------------------------- self-learning
def test_bootstrap_builds_a_measured_track_record(jarvis):
    result = jarvis.bootstrap_expertise(
        symbols=["AAPL", "MSFT", "NVDA", "TSLA", "AMD"], period="2y"
    )
    assert result["signals_recorded"] > 0
    assert result["signals_graded"] > 0

    board = jarvis.evaluator.pattern_scoreboard(min_samples=1)
    assert board
    for row in board:
        assert 0.0 <= row["hit_rate"] <= 1.0
        assert row["samples"] >= 1


def test_expertise_rises_only_after_validation(jarvis):
    before = jarvis.memory.knowledge.expertise_level()[1]
    jarvis.bootstrap_expertise(symbols=["AAPL", "MSFT", "NVDA", "TSLA", "AMD"], period="2y")
    after = jarvis.memory.knowledge.expertise_level()[1]
    assert after > before


def test_what_i_know_reports_measured_performance(jarvis):
    jarvis.bootstrap_expertise(symbols=["AAPL", "MSFT", "NVDA"], period="2y")
    summary = jarvis.what_i_know()
    assert "Expertise:" in summary
    assert "predictions graded" in summary


def test_learning_never_invents_a_track_record(jarvis):
    """Before any grading, no pattern may claim measured performance."""
    assert jarvis.evaluator.pattern_scoreboard(min_samples=1) == []
    assert "Run `jarvis bootstrap`" in jarvis.what_i_know()


# ----------------------------------------------------------------- answering
def test_answers_portfolio_questions(jarvis):
    jarvis.portfolio.record_cash(1_000)
    assert "Portfolio" in jarvis.ask("how is my portfolio doing")


def test_topic_question_is_not_hijacked_by_self_status(jarvis):
    answer = jarvis.ask("what do you know about position sizing")
    assert "Expertise:" not in answer
    assert "position sizing" in answer.lower()


def test_self_status_question_still_works(jarvis):
    assert "Expertise:" in jarvis.ask("what do you know")


def test_symbol_briefing_includes_a_disclaimer(jarvis):
    briefing = jarvis.brief_symbol("AAPL")
    assert "not a recommendation" in briefing


def test_symbol_briefing_prices_a_holding(jarvis):
    jarvis.portfolio.record_cash(10_000)
    jarvis.portfolio.record_trade("AAPL", "buy", 10, 1.0)  # deliberately far below market
    briefing = jarvis.brief_symbol("AAPL")
    assert "You hold 10" in briefing
    assert "+0.0% open" not in briefing  # must be marked to market, not flat


def test_unknown_topic_is_admitted_not_confabulated(jarvis):
    answer = jarvis.ask("what is the optimal quantum arbitrage lattice frequency")
    assert "don't have anything solid" in answer


def test_no_prior_close_is_stated_plainly(jarvis):
    jarvis.portfolio.record_cash(1_000)
    assert "prior close" in jarvis.ask("what did I make yesterday")


# --------------------------------------------------------------------- voice
def test_wake_phrase_matching():
    wake = PhraseWake(TextListener(), "hey jarvis")
    assert wake.heard_wake("hey jarvis")
    assert wake.heard_wake("Hey Jarvis, what's up")
    assert wake.heard_wake("jarvis")            # STT drops the "hey" constantly
    assert not wake.heard_wake("hey google")
    assert not wake.heard_wake("")
    assert not wake.heard_wake(None)
