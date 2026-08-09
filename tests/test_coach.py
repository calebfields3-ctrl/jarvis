"""The training curriculum and the journal review."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from jarvis.learning.coach import CURRICULUM, MODULES_BY_KEY, TradingCoach
from jarvis.market.session import EASTERN
from jarvis.portfolio.risk import RiskManager, RiskProfile
from jarvis.portfolio.tracker import PortfolioTracker

MONDAY = datetime(2026, 8, 3, 10, 0, tzinfo=EASTERN)


@pytest.fixture
def coach(memory) -> TradingCoach:
    return TradingCoach(memory.training, RiskManager(memory.db, RiskProfile()))


@pytest.fixture
def tracker(memory) -> PortfolioTracker:
    return PortfolioTracker(memory.db)


def round_trip(tracker, symbol, when, entry, exit_, qty=100):
    tracker.record_trade(symbol, "buy", qty, entry, executed_at=when)
    tracker.record_trade(symbol, "sell", qty, exit_, executed_at=when + timedelta(hours=1))


# ------------------------------------------------------------- curriculum
def test_curriculum_is_well_formed():
    keys = {m.key for m in CURRICULUM}
    for module in CURRICULUM:
        assert module.quiz, f"{module.key} has no quiz"
        assert 1 <= module.pass_mark <= len(module.quiz)
        for question in module.quiz:
            assert 0 <= question.answer_index < len(question.options)
            assert question.explanation
        for requirement in module.requires:
            assert requirement in keys, f"{module.key} requires unknown {requirement}"


def test_prerequisites_come_before_dependents():
    seen: set[str] = set()
    for module in CURRICULUM:
        assert all(r in seen for r in module.requires)
        seen.add(module.key)


def test_risk_modules_demand_a_perfect_score():
    """You do not get to be 'mostly right' about the PDT rule or sizing."""
    for key in ("risk_sizing", "pdt_rule"):
        module = MODULES_BY_KEY[key]
        assert module.pass_mark == len(module.quiz)


def test_starts_at_the_first_module(coach):
    assert coach.next_module().key == "what_is_day_trading"
    assert coach.progress()["passed"] == 0


def test_passing_unlocks_the_next_module(coach):
    module = MODULES_BY_KEY["what_is_day_trading"]
    correct = [q.answer_index for q in module.quiz]
    result = coach.grade_quiz(module, correct)

    assert result["passed"]
    assert result["score"] == len(module.quiz)
    assert coach.next_module().key == "session_structure"


def test_failing_does_not_unlock(coach):
    module = MODULES_BY_KEY["what_is_day_trading"]
    wrong = [(q.answer_index + 1) % len(q.options) for q in module.quiz]
    result = coach.grade_quiz(module, wrong)

    assert not result["passed"]
    assert result["score"] == 0
    assert coach.next_module().key == "what_is_day_trading"


def test_wrong_answers_come_back_with_explanations(coach):
    module = MODULES_BY_KEY["what_is_day_trading"]
    wrong = [(q.answer_index + 1) % len(q.options) for q in module.quiz]
    result = coach.grade_quiz(module, wrong)
    for item in result["detail"]:
        assert not item["correct"]
        assert item["explanation"]
        assert item["right_answer"]


def test_progress_persists_across_restarts(memory, home):
    from jarvis.memory.store import Memory

    module = MODULES_BY_KEY["what_is_day_trading"]
    first = TradingCoach(memory.training, RiskManager(memory.db, RiskProfile()))
    first.grade_quiz(module, [q.answer_index for q in module.quiz])
    path = memory.db.path
    memory.close()

    revived = Memory(path)
    try:
        coach = TradingCoach(revived.training, RiskManager(revived.db, RiskProfile()))
        assert "what_is_day_trading" in revived.training.passed_modules()
        assert coach.next_module().key == "session_structure"
    finally:
        revived.close()


def test_a_later_failure_does_not_revoke_a_pass(coach):
    module = MODULES_BY_KEY["what_is_day_trading"]
    coach.grade_quiz(module, [q.answer_index for q in module.quiz])
    coach.grade_quiz(module, [-1] * len(module.quiz))
    assert "what_is_day_trading" in coach.training.passed_modules()


def test_completing_everything_certifies(coach):
    for module in CURRICULUM:
        coach.grade_quiz(module, [q.answer_index for q in module.quiz])
    progress = coach.progress()
    assert progress["certified"]
    assert progress["next"] is None
    assert coach.next_module() is None


# ---------------------------------------------------------------- review
def test_empty_journal_says_so(coach):
    review = coach.review_journal()
    assert review.trades_reviewed == 0
    assert "No closed round trips" in review.describe()


def test_expectancy_is_computed(coach, tracker):
    round_trip(tracker, "AAPL", MONDAY, 100.0, 102.0)          # +200
    round_trip(tracker, "MSFT", MONDAY + timedelta(days=1), 100.0, 99.0)   # -100
    review = coach.review_journal()

    assert review.trades_reviewed == 2
    assert review.win_rate == pytest.approx(0.5)
    assert review.avg_win == pytest.approx(200.0)
    assert review.avg_loss == pytest.approx(100.0)
    assert review.expectancy == pytest.approx(50.0)


def test_cutting_winners_and_holding_losers_is_flagged(coach, tracker):
    for index in range(4):
        round_trip(tracker, f"WIN{index}", MONDAY + timedelta(days=index), 100.0, 100.2)
    for index in range(3):
        round_trip(tracker, f"LOSS{index}", MONDAY + timedelta(days=index + 4), 100.0, 95.0)

    review = coach.review_journal()
    codes = {f.code for f in review.findings}
    assert "cut_winners_hold_losers" in codes
    finding = next(f for f in review.findings if f.code == "cut_winners_hold_losers")
    assert finding.severity == "critical"
    assert review.expectancy < 0


def test_healthy_asymmetry_is_praised(coach, tracker):
    for index in range(3):
        round_trip(tracker, f"WIN{index}", MONDAY + timedelta(days=index), 100.0, 106.0)
    for index in range(4):
        round_trip(tracker, f"LOSS{index}", MONDAY + timedelta(days=index + 3), 100.0, 98.0)

    review = coach.review_journal()
    assert any(f.code == "good_asymmetry" and f.severity == "good" for f in review.findings)


def test_revenge_sizing_is_caught_within_a_session(coach, tracker):
    """Sizing up right after a loss, in the same session, is the tell."""
    for day in range(3):
        base = MONDAY + timedelta(days=day)
        round_trip(tracker, f"A{day}", base, 100.0, 95.0, qty=100)                 # loss
        round_trip(tracker, f"B{day}", base + timedelta(minutes=90), 100.0, 101.0, qty=400)

    review = coach.review_journal()
    assert any(f.code == "revenge_sizing" for f in review.findings)


def test_sizing_up_the_next_morning_is_not_revenge_trading(coach, tracker):
    """A considered size change between sessions is a strategy call, not a reflex."""
    for day in range(4):
        base = MONDAY + timedelta(days=day)
        qty = 100 * (day + 1)
        round_trip(tracker, f"S{day}", base, 100.0, 95.0, qty=qty)

    review = coach.review_journal()
    assert not any(f.code == "revenge_sizing" for f in review.findings)


def test_overnight_holds_are_flagged(coach, tracker):
    for index in range(6):
        when = MONDAY + timedelta(days=index)
        tracker.record_trade(f"SYM{index}", "buy", 10, 100.0, executed_at=when)
        tracker.record_trade(
            f"SYM{index}", "sell", 10, 101.0, executed_at=when + timedelta(days=1)
        )
    review = coach.review_journal()
    assert any(f.code == "overnight_drift" for f in review.findings)


def test_midday_entries_are_flagged(coach, tracker):
    for index in range(10):
        when = (MONDAY + timedelta(days=index)).replace(hour=12, minute=30)
        round_trip(tracker, f"MID{index}", when, 100.0, 101.0)
    review = coach.review_journal()
    assert any(f.code == "midday_entries" for f in review.findings)


def test_morning_entries_are_not_flagged_as_midday(coach, tracker):
    for index in range(10):
        when = (MONDAY + timedelta(days=index)).replace(hour=10, minute=15)
        round_trip(tracker, f"AM{index}", when, 100.0, 101.0)
    review = coach.review_journal()
    assert not any(f.code == "midday_entries" for f in review.findings)


def test_overtrading_is_flagged(coach, tracker):
    for index in range(9):
        when = MONDAY + timedelta(minutes=index * 20)
        round_trip(tracker, f"OT{index}", when, 100.0, 100.5)
    review = coach.review_journal()
    assert any(f.code == "overtrading" for f in review.findings)


def test_small_samples_are_called_out(coach, tracker):
    round_trip(tracker, "AAPL", MONDAY, 100.0, 101.0)
    review = coach.review_journal()
    assert any(f.code == "small_sample" for f in review.findings)


def test_findings_are_ordered_by_severity(coach, tracker):
    for index in range(4):
        round_trip(tracker, f"WIN{index}", MONDAY + timedelta(days=index), 100.0, 100.2)
    for index in range(3):
        round_trip(tracker, f"LOSS{index}", MONDAY + timedelta(days=index + 4), 100.0, 95.0)

    review = coach.review_journal()
    order = {"critical": 0, "warning": 1, "note": 2, "good": 3}
    severities = [order[f.severity] for f in review.findings]
    assert severities == sorted(severities)
