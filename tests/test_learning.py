"""Distillation, the outcome loop, and news assessment."""

from __future__ import annotations

import pytest

from jarvis.learning.curriculum import seed_foundations, seed_pattern_hypotheses
from jarvis.learning.distill import RuleBasedDistiller, ingest_lessons
from jarvis.learning.evaluate import OutcomeEvaluator
from jarvis.learning.news import NewsMonitor, parse_feed
from jarvis.market.patterns import Bar, scan_bars
from jarvis.safety.url_safety import URLSafetyChecker


# ------------------------------------------------------------------ distiller
@pytest.fixture
def distiller() -> RuleBasedDistiller:
    return RuleBasedDistiller()


def test_extracts_a_generalisable_claim(distiller):
    lessons = distiller.distill(
        "When the MACD crosses above its signal line it often confirms that "
        "momentum is shifting to the upside."
    )
    assert len(lessons) == 1
    assert lessons[0].pattern_key == "macd_bull_cross"
    assert lessons[0].topic == "indicators"


def test_wrapped_sentences_are_not_chopped(distiller):
    """Transcripts are line-wrapped; a claim must survive the wrap."""
    lessons = distiller.distill(
        "The key is position sizing: you want to risk a small fixed fraction\n"
        "of your account per trade, because that is what keeps a losing streak\n"
        "survivable."
    )
    assert lessons
    assert "losing streak" in lessons[0].claim


@pytest.mark.parametrize(
    "noise",
    [
        "Smash that like button and subscribe to the channel right now.",
        "Join my discord signals group, link in bio, guaranteed returns.",
        "Use promo code TRADE for my masterclass sale, enroll today.",
    ],
)
def test_promotional_noise_is_discarded(distiller, noise):
    assert distiller.distill(noise) == []


def test_personal_anecdotes_score_below_rules(distiller):
    lessons = distiller.distill(
        "I bought TSLA last week when it broke above resistance and I made money. "
        "A breakout above resistance on volume tends to keep running for days."
    )
    claims = [l.claim for l in lessons]
    assert any("tends to keep running" in c for c in claims)


def test_duplicate_claims_are_collapsed(distiller):
    text = (
        "RSI below 30 usually signals oversold conditions. "
        "RSI below 30 usually signals oversold conditions."
    )
    assert len(distiller.distill(text)) == 1


def test_ingested_lessons_start_uncertain(memory):
    lessons = RuleBasedDistiller().distill(
        "A breakout above the 20-day high on volume tends to keep running."
    )
    assert lessons
    ingest_lessons(memory.knowledge, lessons, source_credibility=0.9)
    stored = memory.knowledge.lessons(tier="hypothesis")
    assert stored
    # Even a maximally credible teacher cannot mint certainty.
    assert all(l.confidence <= 0.72 for l in stored)


# ----------------------------------------------------------------- curriculum
def test_foundations_are_seeded_once(memory):
    seed_foundations(memory.knowledge)
    first = memory.knowledge.stats()["total"]
    seed_foundations(memory.knowledge)
    assert memory.knowledge.stats()["total"] == first


def test_pattern_hypotheses_start_at_a_coin_flip(memory):
    seed_pattern_hypotheses(memory.knowledge)
    for lesson in memory.knowledge.lessons(tier="hypothesis", limit=100):
        assert lesson.tier == "hypothesis"
        assert lesson.confidence == pytest.approx(0.5)


def test_jarvis_starts_a_novice(memory):
    seed_foundations(memory.knowledge)
    seed_pattern_hypotheses(memory.knowledge)
    label, score = memory.knowledge.expertise_level()
    assert label == "novice"
    assert score < 0.1


# ------------------------------------------------------------- outcome loop
class _FixedProvider:
    """A market that goes exactly where the test says it goes."""

    name = "fixed"

    def __init__(self, closes: list[float]) -> None:
        self.closes = closes

    def bars(self, symbol, period="1y", interval="1d"):
        return [
            Bar(ts=f"2025-01-{i + 1:02d}", open=c, high=c, low=c, close=c, volume=1e6)
            for i, c in enumerate(self.closes)
        ]

    def bulk_bars(self, symbols, period="6mo", interval="1d"):
        return {s: self.bars(s) for s in symbols}

    def prices(self, symbols):
        return {s: self.closes[-1] for s in symbols}

    def fundamentals(self, symbol):
        return {}


def test_correct_call_reinforces_its_lesson(memory):
    seed_pattern_hypotheses(memory.knowledge)
    lesson = memory.knowledge.lesson_for_pattern("breakout_20d_high")
    before = lesson.confidence

    memory.signals.record(
        symbol="TEST", pattern_key="breakout_20d_high", direction="long",
        price=100.0, horizon_bars=3, lesson_id=lesson.id,
        detected_at="2025-01-01 00:00:00",
    )
    provider = _FixedProvider([100.0, 101.0, 102.0, 110.0, 111.0])
    report = OutcomeEvaluator(provider, memory.signals, memory.knowledge).evaluate_open_signals()

    assert len(report.graded) == 1
    assert report.graded[0].hit is True
    assert memory.knowledge.get_lesson(lesson.id).confidence > before


def test_wrong_call_erodes_its_lesson(memory):
    seed_pattern_hypotheses(memory.knowledge)
    lesson = memory.knowledge.lesson_for_pattern("breakout_20d_high")
    before = lesson.confidence

    memory.signals.record(
        symbol="TEST", pattern_key="breakout_20d_high", direction="long",
        price=100.0, horizon_bars=3, lesson_id=lesson.id,
        detected_at="2025-01-01 00:00:00",
    )
    provider = _FixedProvider([100.0, 99.0, 98.0, 90.0, 89.0])
    report = OutcomeEvaluator(provider, memory.signals, memory.knowledge).evaluate_open_signals()

    assert report.graded[0].hit is False
    assert memory.knowledge.get_lesson(lesson.id).confidence < before


def test_short_signal_grades_on_a_fall(memory):
    seed_pattern_hypotheses(memory.knowledge)
    lesson = memory.knowledge.lesson_for_pattern("breakdown_20d_low")
    memory.signals.record(
        symbol="TEST", pattern_key="breakdown_20d_low", direction="short",
        price=100.0, horizon_bars=2, lesson_id=lesson.id,
        detected_at="2025-01-01 00:00:00",
    )
    provider = _FixedProvider([100.0, 97.0, 92.0, 91.0])
    report = OutcomeEvaluator(provider, memory.signals, memory.knowledge).evaluate_open_signals()
    assert report.graded[0].hit is True


def test_noise_sized_moves_do_not_count_as_hits(memory):
    seed_pattern_hypotheses(memory.knowledge)
    lesson = memory.knowledge.lesson_for_pattern("breakout_20d_high")
    memory.signals.record(
        symbol="TEST", pattern_key="breakout_20d_high", direction="long",
        price=100.0, horizon_bars=2, lesson_id=lesson.id,
        detected_at="2025-01-01 00:00:00",
    )
    provider = _FixedProvider([100.0, 100.05, 100.1, 100.1])
    report = OutcomeEvaluator(provider, memory.signals, memory.knowledge).evaluate_open_signals()
    assert report.graded[0].hit is False


def test_signals_whose_horizon_has_not_passed_stay_open(memory):
    seed_pattern_hypotheses(memory.knowledge)
    lesson = memory.knowledge.lesson_for_pattern("breakout_20d_high")
    memory.signals.record(
        symbol="TEST", pattern_key="breakout_20d_high", direction="long",
        price=100.0, horizon_bars=10, lesson_id=lesson.id,
        detected_at="2025-01-01 00:00:00",
    )
    provider = _FixedProvider([100.0, 101.0, 102.0])
    report = OutcomeEvaluator(provider, memory.signals, memory.knowledge).evaluate_open_signals()
    assert report.graded == []
    assert report.still_open == 1
    assert len(memory.signals.open_signals()) == 1


def test_a_repeatedly_wrong_pattern_is_retired(memory):
    seed_pattern_hypotheses(memory.knowledge)
    lesson = memory.knowledge.lesson_for_pattern("hammer")
    for _ in range(25):
        memory.knowledge.reinforce(
            lesson.id, hit=False, promotion_samples=20, promotion_conf=0.62
        )
    assert memory.knowledge.get_lesson(lesson.id).tier == "retired"


def test_a_repeatedly_right_pattern_becomes_expertise(memory):
    seed_pattern_hypotheses(memory.knowledge)
    lesson = memory.knowledge.lesson_for_pattern("hammer")
    for _ in range(25):
        memory.knowledge.reinforce(
            lesson.id, hit=True, promotion_samples=20, promotion_conf=0.62
        )
    updated = memory.knowledge.get_lesson(lesson.id)
    assert updated.tier == "expertise"
    assert updated.confidence > 0.8


def test_foundations_are_never_retired(memory):
    lesson_id = memory.knowledge.add_lesson(
        "mechanics", "The bid is below the ask.", tier="foundation", confidence=0.9
    )
    for _ in range(40):
        memory.knowledge.reinforce(
            lesson_id, hit=False, promotion_samples=20, promotion_conf=0.62
        )
    assert memory.knowledge.get_lesson(lesson_id).tier == "foundation"


def test_outcomes_move_the_teacher_not_just_the_lesson(memory):
    source_id = memory.knowledge.upsert_source(
        "youtube", "@teacher", verification="verified", credibility=0.6
    )
    lesson_id = memory.knowledge.add_lesson(
        "patterns", "Some claim.", pattern_key="test_pattern",
        tier="hypothesis", confidence=0.5, source_id=source_id,
    )
    for _ in range(10):
        memory.knowledge.reinforce(lesson_id, hit=False, promotion_samples=20, promotion_conf=0.62)
    assert memory.knowledge.get_source(source_id)["credibility"] < 0.6


def test_retired_lessons_still_resolve_for_their_pattern(memory):
    """A retired pattern must keep its lesson so it can recover -- and so its
    low confidence keeps damping new signals."""
    seed_pattern_hypotheses(memory.knowledge)
    lesson = memory.knowledge.lesson_for_pattern("hammer")
    for _ in range(25):
        memory.knowledge.reinforce(lesson.id, hit=False, promotion_samples=20, promotion_conf=0.62)
    resolved = memory.knowledge.lesson_for_pattern("hammer")
    assert resolved is not None
    assert resolved.tier == "retired"
    assert resolved.confidence < 0.4


def test_duplicate_signals_on_the_same_bar_are_ignored(memory):
    first = memory.signals.record(
        symbol="TEST", pattern_key="hammer", direction="long", price=100.0,
        detected_at="2025-01-01 00:00:00",
    )
    second = memory.signals.record(
        symbol="TEST", pattern_key="hammer", direction="long", price=100.0,
        detected_at="2025-01-01 00:00:00",
    )
    assert first is not None
    assert second is None


# ----------------------------------------------------------------------- news
@pytest.fixture
def monitor() -> NewsMonitor:
    return NewsMonitor([], URLSafetyChecker(), universe=["AAPL", "NVDA"])


def test_macro_headlines_score_high(monitor):
    item = monitor.assess("Reuters", {"title": "Fed announces surprise rate cut", "summary": ""})
    assert item.impact >= 0.8
    assert item.is_material


def test_mundane_headlines_score_low(monitor):
    item = monitor.assess("Reuters", {"title": "Local bakery wins county fair prize", "summary": ""})
    assert item.impact == 0.0
    assert not item.is_material


def test_urgency_markers_add_impact(monitor):
    calm = monitor.assess("AP", {"title": "Company announces layoffs", "summary": ""})
    urgent = monitor.assess("AP", {"title": "BREAKING: Company announces layoffs", "summary": ""})
    assert urgent.impact > calm.impact


def test_cashtags_are_always_extracted(monitor):
    item = monitor.assess("CNBC", {"title": "$AAPL and $TSLA rally on earnings", "summary": ""})
    assert "AAPL" in item.tickers
    assert "TSLA" in item.tickers


def test_bare_tickers_only_count_when_on_the_watchlist(monitor):
    item = monitor.assess("CNBC", {"title": "NVDA and ZZZZ move on earnings", "summary": ""})
    assert "NVDA" in item.tickers
    assert "ZZZZ" not in item.tickers


def test_common_acronyms_are_not_mistaken_for_tickers(monitor):
    item = monitor.assess("BBC", {"title": "The CEO told the SEC and the FBI", "summary": ""})
    assert item.tickers == []


def test_sectors_are_tagged(monitor):
    item = monitor.assess("Reuters", {"title": "OPEC cuts oil output", "summary": "crude prices"})
    assert "energy" in item.sectors


def test_news_store_dedupes(memory):
    first = memory.news.add("Reuters", "Fed cuts rates", impact=0.9)
    second = memory.news.add("Reuters", "Fed cuts rates", impact=0.9)
    assert first is not None
    assert second is None


def test_rss_parses_without_feedparser():
    raw = b"""<?xml version="1.0"?>
    <rss version="2.0"><channel>
      <item><title>Fed holds rates</title><link>https://reuters.com/a</link>
            <description>Policy unchanged</description>
            <pubDate>Mon, 01 Jan 2025 00:00:00 GMT</pubDate></item>
    </channel></rss>"""
    entries = parse_feed(raw)
    assert entries[0]["title"] == "Fed holds rates"
    assert entries[0]["link"] == "https://reuters.com/a"


def test_atom_parses_without_feedparser():
    raw = b"""<?xml version="1.0"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry><title>ECB decision</title>
             <link href="https://reuters.com/b"/>
             <summary>Rates held</summary></entry>
    </feed>"""
    entries = parse_feed(raw)
    assert entries[0]["title"] == "ECB decision"
    assert entries[0]["link"] == "https://reuters.com/b"


def test_malformed_feed_returns_nothing_rather_than_raising():
    assert parse_feed(b"this is not xml at all") == []


def test_unsafe_feed_urls_are_never_fetched():
    monitor = NewsMonitor(
        [{"name": "Sketchy", "url": "http://not-https-feed.example/rss"}], URLSafetyChecker()
    )
    assert monitor.poll() == []
    assert any("safety gate" in e for e in monitor.last_errors)
