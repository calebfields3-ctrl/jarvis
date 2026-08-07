"""The voice layer: what he actually says, and what he must never say."""

from __future__ import annotations

import inspect

import pytest

from jarvis.persona import (
    PERSONAS,
    GreetingContext,
    JarvisPersona,
    Persona,
    build_persona,
)


def ctx(**overrides) -> GreetingContext:
    base = dict(
        name="Caleb", hour=9, first_session=False, gap="14 hours",
        expertise_label="competent", expertise_score=0.49,
        lessons=214, graded=1295,
    )
    base.update(overrides)
    return GreetingContext(**base)


@pytest.fixture
def jarvis_voice() -> JarvisPersona:
    return JarvisPersona(seed=1)


@pytest.fixture
def plain_voice() -> Persona:
    return Persona(seed=1)


# ------------------------------------------------------------------ selection
def test_default_is_the_butler():
    assert build_persona().key == "jarvis"
    assert isinstance(build_persona("jarvis"), JarvisPersona)


def test_plain_is_selectable():
    assert build_persona("plain").key == "plain"


def test_unknown_persona_falls_back_to_the_butler():
    assert build_persona("nonsense").key == "jarvis"
    assert build_persona("").key == "jarvis"


def test_every_persona_implements_the_full_surface():
    """A persona missing a method would crash at runtime, not at import."""
    required = [
        name for name, _ in inspect.getmembers(Persona, inspect.isfunction)
        if not name.startswith("_")
    ]
    for key, cls in PERSONAS.items():
        for name in required:
            assert callable(getattr(cls, name, None)), f"{key} is missing {name}()"


# -------------------------------------------------------------------- address
def test_butler_uses_the_honorific(jarvis_voice):
    assert jarvis_voice.address == "sir"
    assert "sir" in jarvis_voice.dismissed().lower()


def test_honorific_can_be_changed():
    voice = JarvisPersona(seed=1, address="boss")
    assert "boss" in voice.blocked_preamble()
    assert "sir" not in voice.blocked_preamble()


def test_honorific_can_be_dropped_entirely():
    voice = JarvisPersona(seed=1, address="")
    text = voice.blocked_preamble()
    assert "sir" not in text
    # Dropping it must not leave dangling punctuation.
    assert ", ." not in text
    assert "  " not in text


def test_plain_persona_has_no_honorific(plain_voice):
    assert plain_voice.address == ""
    assert "sir" not in plain_voice.dismissed().lower()


# ------------------------------------------------------------------- greeting
@pytest.mark.parametrize("key", list(PERSONAS))
def test_every_persona_greets_by_name(key):
    """The original brief: he greets you by name. Non-negotiable in any voice."""
    voice = build_persona(key, seed=1)
    assert "Caleb" in voice.greeting_open(ctx())


@pytest.mark.parametrize("hour,expected", [(9, "morning"), (14, "afternoon"), (20, "evening")])
def test_greeting_tracks_the_clock(jarvis_voice, hour, expected):
    assert expected in jarvis_voice.greeting_open(ctx(hour=hour)).lower()


def test_small_hours_get_a_dry_remark(jarvis_voice):
    assert "Caleb" in jarvis_voice.greeting_open(ctx(hour=3))


def test_first_session_is_acknowledged(jarvis_voice):
    opener = jarvis_voice.greeting_gap(ctx(first_session=True, gap=None))
    assert "remember" in opener.lower()


def test_returning_session_mentions_the_gap(jarvis_voice):
    assert "14 hours" in jarvis_voice.greeting_gap(ctx())


def test_no_gap_means_no_line(jarvis_voice):
    assert jarvis_voice.greeting_gap(ctx(gap=None)) is None


# ---------------------------------------------------------------- honesty
def test_he_admits_when_nothing_has_been_graded(jarvis_voice):
    """The persona must never dress up an untested record."""
    text = jarvis_voice.greeting_expertise(ctx(graded=0, expertise_label="novice"))
    assert "graded nothing" in text
    assert "bootstrap" in text


def test_novice_is_reported_as_untrustworthy(jarvis_voice):
    text = jarvis_voice.greeting_expertise(ctx(expertise_label="novice", graded=40))
    assert "not yet trust" in text


def test_expertise_numbers_are_never_inflated(jarvis_voice):
    text = jarvis_voice.greeting_expertise(ctx())
    assert "1,295" in text
    assert "214" in text
    assert "0.49" in text


@pytest.mark.parametrize("key", list(PERSONAS))
def test_disclaimer_survives_every_persona(key):
    """However he phrases it, he must not present analysis as advice."""
    text = build_persona(key, seed=1).disclaimer().lower()
    assert "advice" in text or "recommendation" in text


@pytest.mark.parametrize("key", list(PERSONAS))
def test_blocked_preamble_actually_refuses(key):
    text = build_persona(key, seed=1).blocked_preamble().lower()
    assert any(word in text for word in ("rather not", "not going to", "won't", "will not"))


# ------------------------------------------------------------------ numbers
def test_money_formatting(plain_voice):
    assert plain_voice.money(1504.5) == "$1,504.50"
    assert plain_voice.money(1504.5, signed=True) == "+$1,504.50"
    assert plain_voice.money(-689.4, signed=True) == "-$689.40"


@pytest.mark.parametrize("key", list(PERSONAS))
def test_numbers_are_identical_across_personas(key):
    """Only the framing changes. The figures must be byte-identical."""
    voice = build_persona(key, seed=1)
    assert "$100,303.12" in voice.portfolio_headline(100303.12, 3)
    assert "$1,504.55" in voice.day_pnl(1504.55, 1.52, "2026-08-06")
    assert "1.52" in voice.day_pnl(1504.55, 1.52, "2026-08-06")


def test_loss_is_reported_as_a_loss(jarvis_voice):
    text = jarvis_voice.day_pnl(-689.40, -0.68, "2026-08-06")
    assert "down" in text
    assert "$689.40" in text
    assert "-$" not in text  # direction is in the words, not a stray sign


def test_singular_position_reads_correctly(jarvis_voice):
    assert "1 position." in jarvis_voice.portfolio_headline(1000.0, 1)
    assert "3 positions." in jarvis_voice.portfolio_headline(1000.0, 3)


def test_all_cash_is_described_as_such(jarvis_voice):
    assert "cash" in jarvis_voice.portfolio_headline(5000.0, 0)


# ------------------------------------------------------------------ tone
def test_a_bad_position_gets_flagged_more_firmly(jarvis_voice):
    mild = jarvis_voice.worst_position("XOM", -1.0, -50.0)
    bad = jarvis_voice.worst_position("XOM", -12.0, -1200.0)
    assert "draw your attention" in bad
    assert "draw your attention" not in mild


def test_empty_scan_is_stated_without_padding(jarvis_voice):
    text = jarvis_voice.scan_summary(677, 677, 0)
    assert "nothing" in text.lower()


def test_watch_language_is_specific(jarvis_voice):
    added = jarvis_voice.watch_added("XOM", 105.0, "below")
    assert "XOM" in added and "105.00" in added and "below" in added
    fired = jarvis_voice.watch_triggered("XOM", 105.0, "below", 104.2)
    assert "104.20" in fired and "105.00" in fired


def test_wit_is_deterministic_under_a_seed():
    """Two personas with the same seed must produce identical prose."""
    a, b = JarvisPersona(seed=7), JarvisPersona(seed=7)
    assert [a.dismissed() for _ in range(5)] == [b.dismissed() for _ in range(5)]


def test_he_does_not_quip_on_every_line(jarvis_voice):
    """Understatement is the register; constant jokes stop being read."""
    factual = [
        jarvis_voice.portfolio_headline(100.0, 2),
        jarvis_voice.day_pnl(100.0, 1.0, "2026-08-06"),
        jarvis_voice.best_position("NVDA", 5.0, 50.0),
        jarvis_voice.watch_added("XOM", 105.0, "below"),
    ]
    for line in factual:
        assert len(line) < 160, f"too florid: {line}"


def test_a_winning_laggard_is_not_called_unhappy(jarvis_voice):
    """The weakest holding can still be up. He must not misread his own numbers."""
    green = jarvis_voice.worst_position("AAPL", 84.9, 15483.21)
    assert "Least happy" not in green
    assert "green" in green

    red = jarvis_voice.worst_position("XOM", -3.1, -689.40)
    assert "Least happy" in red

    ugly = jarvis_voice.worst_position("XOM", -12.0, -1200.0)
    assert "draw your attention" in ugly
