"""End-to-end behaviour: waking, greeting, scanning, remembering."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from jarvis.brain import Jarvis
from jarvis.market.provider import SyntheticProvider
from jarvis.portfolio.tracker import market_now
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
    jarvis.portfolio.close_day(prior, as_of_date=market_now().date() - timedelta(days=1))

    jarvis.provider = SyntheticProvider(seed=1)
    monkey_price = {"AAPL": 120.0}
    jarvis.provider.prices = lambda syms: monkey_price  # type: ignore[method-assign]

    greeting = jarvis.wake()
    # Assert on the facts, not the phrasing -- the wording belongs to the
    # persona and is covered by tests/test_persona.py.
    assert "up $200.00" in greeting
    assert "$10,200.00" in greeting


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
    report = jarvis.scan_market(["AAPL", "MSFT", "NVDA"])
    briefing = jarvis.scan_briefing(report)
    assert jarvis.voice.scan_summary(3, 3, len(report.detections)) in briefing


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
    assert "$1,000.00" in jarvis.ask("how is my portfolio doing")


def test_topic_question_is_not_hijacked_by_self_status(jarvis):
    answer = jarvis.ask("what do you know about position sizing")
    assert "Expertise:" not in answer
    assert "position sizing" in answer.lower()


def test_self_status_question_still_works(jarvis):
    assert "Expertise:" in jarvis.ask("what do you know")


def test_symbol_briefing_includes_a_disclaimer(jarvis):
    briefing = jarvis.brief_symbol("AAPL")
    assert jarvis.voice.disclaimer() in briefing


def test_symbol_briefing_prices_a_holding(jarvis):
    jarvis.portfolio.record_cash(10_000)
    jarvis.portfolio.record_trade("AAPL", "buy", 10, 1.0)  # deliberately far below market
    briefing = jarvis.brief_symbol("AAPL")
    assert "You hold 10" in briefing
    assert "+0.0% open" not in briefing  # must be marked to market, not flat


def test_unknown_topic_is_admitted_not_confabulated(jarvis):
    answer = jarvis.ask("what is the optimal quantum arbitrage lattice frequency")
    assert answer == jarvis.voice.unknown_topic()


def test_no_prior_close_is_stated_plainly(jarvis):
    jarvis.portfolio.record_cash(1_000)
    assert "prior close" in jarvis.ask("what did I make yesterday").lower()


# --------------------------------------------------------------------- voice
def test_wake_phrase_matching():
    wake = PhraseWake(TextListener(), "hey jarvis")
    assert wake.heard_wake("hey jarvis")
    assert wake.heard_wake("Hey Jarvis, what's up")
    assert wake.heard_wake("jarvis")            # STT drops the "hey" constantly
    assert not wake.heard_wake("hey google")
    assert not wake.heard_wake("")
    assert not wake.heard_wake(None)


# ------------------------------------------------------------- day trading
def test_daytrade_scan_reports_the_session_clock(jarvis):
    report = jarvis.daytrade_scan(["AAPL", "MSFT", "NVDA"])
    assert report.symbols_scanned == 3
    assert report.status is not None
    assert any("ET" in note or "closed" in note.lower() for note in report.notes)


def test_daytrade_setups_carry_a_sized_plan(jarvis):
    jarvis.portfolio.record_cash(50_000)
    briefing = jarvis.daytrade_briefing(
        jarvis.daytrade_scan(["AAPL", "MSFT", "NVDA", "TSLA", "AMD", "META", "XOM", "JPM"])
    )
    assert "Scanned" in briefing
    if "shares @" in briefing:
        assert "stop" in briefing and "target" in briefing


def test_daytrade_refuses_to_pitch_setups_while_blocked(jarvis):
    from datetime import datetime, timedelta

    from jarvis.market.session import EASTERN

    jarvis.portfolio.record_cash(20_000)
    when = datetime.now(tz=EASTERN).replace(hour=10, minute=0)
    for symbol in ("AAPL", "MSFT", "NVDA"):
        jarvis.portfolio.record_trade(symbol, "buy", 10, 100.0, executed_at=when)
        jarvis.portfolio.record_trade(
            symbol, "sell", 10, 101.0, executed_at=when + timedelta(hours=1)
        )
    briefing = jarvis.daytrade_briefing(jarvis.daytrade_scan(["AAPL"]))
    assert jarvis.voice.blocked_preamble() in briefing


def test_plan_trade_uses_live_equity(jarvis):
    jarvis.portfolio.record_cash(30_000)
    plan = jarvis.plan_trade("AAPL", "long", 50.0, 48.0, 56.0)
    assert plan.is_viable
    assert plan.shares == 75


def test_intraday_patterns_start_untested(jarvis):
    lesson = jarvis.memory.knowledge.lesson_for_pattern("opening_range_breakout")
    assert lesson is not None
    assert lesson.tier == "hypothesis"
    assert lesson.support + lesson.refute == 0


# ---------------------------------------------------------------- coaching
def test_teach_presents_the_first_module(jarvis):
    lesson = jarvis.teach()
    assert "What a day trade actually is" in lesson
    assert "Quiz" in lesson


def test_quiz_flow_updates_progress(jarvis):
    from jarvis.learning.coach import MODULES_BY_KEY

    module = MODULES_BY_KEY["what_is_day_trading"]
    letters = [chr(97 + q.answer_index) for q in module.quiz]
    result = jarvis.submit_quiz("what_is_day_trading", letters)
    assert "PASSED" in result
    assert jarvis.coach.progress()["passed"] == 1


def test_unknown_quiz_module_is_reported(jarvis):
    assert "No module called" in jarvis.submit_quiz("not_a_module", ["a"])


def test_asking_to_be_taught_routes_to_the_coach(jarvis):
    assert "Quiz" in jarvis.ask("teach me to day trade")


def test_asking_about_pdt_routes_to_risk(jarvis):
    answer = jarvis.ask("can i trade today")
    assert "PDT" in answer or "day trades" in answer


def test_review_routes_to_the_journal(jarvis):
    assert "No closed round trips" in jarvis.ask("review my trades")


# ----------------------------------------------------------------- watches
def test_watch_fires_once_when_the_level_is_crossed(jarvis):
    jarvis.provider.prices = lambda syms: {"XOM": 104.0}  # type: ignore[method-assign]
    jarvis.add_watch("XOM", 105.0, "below")

    first = jarvis.check_watches()
    assert len(first) == 1
    assert "XOM" in first[0]
    # A watch that repeats every cycle becomes noise, so it fires exactly once.
    assert jarvis.check_watches() == []


def test_watch_stays_quiet_until_the_level_is_reached(jarvis):
    jarvis.provider.prices = lambda syms: {"XOM": 110.0}  # type: ignore[method-assign]
    jarvis.add_watch("XOM", 105.0, "below")
    assert jarvis.check_watches() == []
    assert len(jarvis.memory.watches.active()) == 1


def test_watch_above_direction(jarvis):
    jarvis.provider.prices = lambda syms: {"NVDA": 130.0}  # type: ignore[method-assign]
    jarvis.add_watch("NVDA", 125.0, "above")
    assert len(jarvis.check_watches()) == 1


def test_watches_survive_a_restart(jarvis, config):
    jarvis.add_watch("XOM", 105.0, "below")
    jarvis.memory.close()

    revived = Jarvis(config, provider=SyntheticProvider())
    try:
        assert len(revived.memory.watches.active()) == 1
        assert revived.memory.watches.active()[0]["symbol"] == "XOM"
    finally:
        revived.memory.close()


def test_watch_can_be_cancelled(jarvis):
    jarvis.add_watch("XOM", 105.0, "below")
    assert jarvis.memory.watches.cancel("XOM") >= 1
    assert jarvis.memory.watches.active() == []


def test_natural_language_sets_a_watch(jarvis):
    answer = jarvis.ask("keep an eye on XOM below 105")
    assert "XOM" in answer
    assert len(jarvis.memory.watches.active()) == 1


def test_natural_language_watch_handles_synonyms(jarvis):
    jarvis.ask("tell me if NVDA hits 200")
    active = jarvis.memory.watches.active()
    assert active and active[0]["direction"] == "above"


def test_unknown_ticker_is_not_turned_into_a_watch(jarvis):
    jarvis.ask("keep an eye on ZZZZZ below 105")
    assert jarvis.memory.watches.active() == []


def test_listing_watches_when_empty(jarvis):
    assert "Nothing on watch" in jarvis.list_watches()


def test_price_lookup_failure_does_not_break_watches(jarvis):
    def boom(syms):
        raise RuntimeError("network down")

    jarvis.provider.prices = boom  # type: ignore[method-assign]
    jarvis.add_watch("XOM", 105.0, "below")
    assert jarvis.check_watches() == []          # no crash
    assert len(jarvis.memory.watches.active()) == 1  # and not lost


# ------------------------------------------------------------ premarket brief
def test_premarket_brief_leads_with_risk(jarvis):
    jarvis.portfolio.record_cash(20_000)
    brief = jarvis.premarket_brief()
    assert "$20,000.00" in brief
    # Below $25k the PDT allowance is the first thing that constrains the day.
    assert "Day trades available today" in brief


def test_premarket_brief_lists_active_watches(jarvis):
    jarvis.provider.prices = lambda syms: {"XOM": 200.0}  # type: ignore[method-assign]
    jarvis.portfolio.record_cash(50_000)
    jarvis.add_watch("XOM", 105.0, "below")
    assert "Still on watch" in jarvis.premarket_brief()


def test_premarket_brief_omits_pdt_line_above_the_minimum(jarvis):
    jarvis.portfolio.record_cash(80_000)
    assert "Day trades available today" not in jarvis.premarket_brief()
