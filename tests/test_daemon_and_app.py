"""The wake-word daemon and the assembly around it.

Most of what matters here is degradation. A missing microphone, a missing
API key, and a missing display all have to end with Jarvis still working,
because a Chromebook will be missing at least one of them.
"""

from __future__ import annotations

import threading
import time

import pytest

from jarvis.voice.daemon import MAX_CONSECUTIVE_FAILURES, WakeDaemon


class ScriptedDetector:
    """Fires in a set order, then blocks until told to stop."""

    def __init__(self, *results):
        self.results = list(results)
        self.done = threading.Event()
        self.closed = False

    def wait_for_wake(self):
        if self.results:
            result = self.results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        self.done.wait(timeout=2)
        return False

    def close(self):
        self.closed = True
        self.done.set()


def wait_until(predicate, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


# --------------------------------------------------------------- the daemon


def test_hearing_the_wake_word_calls_back():
    fired = threading.Event()
    daemon = WakeDaemon(ScriptedDetector(True), on_wake=fired.set)
    daemon.start()
    try:
        assert fired.wait(timeout=2)
    finally:
        daemon.stop()


def test_it_keeps_listening_after_each_wake():
    """One wake is not the job. He has to still be there afterwards."""
    count = []
    daemon = WakeDaemon(ScriptedDetector(True, True, True), on_wake=lambda: count.append(1))
    daemon.start()
    try:
        assert wait_until(lambda: len(count) == 3)
    finally:
        daemon.stop()


def test_a_crash_in_the_conversation_does_not_cost_him_his_ears():
    count = []

    def handler():
        count.append(1)
        raise RuntimeError("the turn blew up")

    daemon = WakeDaemon(ScriptedDetector(True, True), on_wake=handler)
    daemon.start()
    try:
        assert wait_until(lambda: len(count) == 2)
    finally:
        daemon.stop()


def test_a_dead_microphone_gives_up_instead_of_spinning_a_core():
    """A detector that raises instantly would otherwise loop forever at 100%."""
    errors = []
    detector = ScriptedDetector(*[OSError("no such device")] * 50)
    daemon = WakeDaemon(detector, on_wake=lambda: None, on_error=errors.append)
    daemon.start()
    try:
        assert wait_until(lambda: not daemon.running)
        assert daemon.failures == MAX_CONSECUTIVE_FAILURES
        assert errors and "microphone" in errors[0]
    finally:
        daemon.stop()


def test_an_intermittent_failure_does_not_end_the_daemon():
    fired = threading.Event()
    detector = ScriptedDetector(OSError("busy"), OSError("busy"), True)
    daemon = WakeDaemon(detector, on_wake=fired.set)
    daemon.start()
    try:
        assert fired.wait(timeout=2)
        assert daemon.failures == 0, "the failure count should reset on success"
    finally:
        daemon.stop()


def test_the_end_of_the_input_stream_stops_the_loop():
    daemon = WakeDaemon(ScriptedDetector(False), on_wake=lambda: None)
    daemon.start()
    try:
        assert wait_until(lambda: not daemon.running)
    finally:
        daemon.stop()


def test_stopping_closes_the_detector():
    detector = ScriptedDetector()
    daemon = WakeDaemon(detector, on_wake=lambda: None)
    daemon.start()
    daemon.stop()
    assert detector.closed
    assert not daemon.running


def test_starting_twice_does_not_make_two_threads():
    detector = ScriptedDetector()
    daemon = WakeDaemon(detector, on_wake=lambda: None)
    daemon.start()
    first = daemon._thread
    daemon.start()
    try:
        assert daemon._thread is first
    finally:
        daemon.stop()


def test_the_listening_thread_never_holds_the_process_open():
    """A blocking mic read can't be interrupted, so it must not be joinable."""
    detector = ScriptedDetector()
    daemon = WakeDaemon(detector, on_wake=lambda: None)
    daemon.start()
    try:
        assert daemon._thread.daemon
    finally:
        daemon.stop()


def test_a_broken_error_hook_does_not_mask_the_shutdown():
    def explode(message):
        raise RuntimeError("the HUD is gone")

    detector = ScriptedDetector(*[OSError("gone")] * 50)
    daemon = WakeDaemon(detector, on_wake=lambda: None, on_error=explode)
    daemon.start()
    try:
        assert wait_until(lambda: not daemon.running)
    finally:
        daemon.stop()


# ------------------------------------------------------------------ the app


@pytest.fixture
def app(tmp_path, monkeypatch):
    from jarvis.app import JarvisApp
    from jarvis.config import Config, reset_config

    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    reset_config()
    yield JarvisApp(Config(), jarvis=FakeJarvis(), use_hud=False, use_voice=False)
    reset_config()


class FakeVoicePersona:
    def summoned(self, name):
        return f"Hello, sir. What can I do for you, {name}?"

    def dismissed(self):
        return "Standing by."


class FakeProfile:
    name = "Caleb"


class FakeMemory:
    profile = FakeProfile()


class FakeJarvis:
    def __init__(self, first_today=False):
        self.asked = []
        self.slept = False
        self.briefed = 0
        self._first_today = first_today
        self.voice = FakeVoicePersona()
        self.memory = FakeMemory()

    def wake(self, channel="text"):
        return "Morning, Caleb."

    def first_time_today(self):
        return self._first_today

    def summoned(self):
        self.briefed += 1
        self._first_today = False
        return "Your portfolio stands at $10,000. Overnight: the Fed spoke."

    def ask(self, text):
        self.asked.append(text)
        return f"answer to {text}"

    def sleep(self, summary=None):
        self.slept = True


class FakeHUD:
    """Records what the window was told to do, in order."""

    def __init__(self):
        self.actions = []
        self.lines = []

    def summon(self):
        self.actions.append("summon")

    def dismiss(self):
        self.actions.append("dismiss")

    def say(self, text, who="jarvis"):
        self.actions.append("say")
        self.lines.append((who, text))

    def set_mode(self, mode):
        self.actions.append(f"mode:{mode.value}")

    def status(self, text):
        pass

    def tool_call(self, call):
        pass


class FakeVoice:
    def __init__(self, *heard):
        self.heard = list(heard)
        self.spoken = []
        self.is_voice = True

    def listen(self, timeout=None):
        return self.heard.pop(0) if self.heard else None

    def say(self, text):
        self.spoken.append(text)


@pytest.fixture
def summonable(app):
    """An app wired the way it runs with a microphone and a window."""
    app.hud = FakeHUD()
    app.voice = FakeVoice()
    app.mind = None
    return app


# ------------------------------------------------------- summoned by voice


def test_the_window_comes_up_before_anything_is_looked_up(summonable):
    """A pause between the wake word and the window reads as it not working."""
    summonable._woken()
    assert summonable.hud.actions[0] == "summon"


def test_he_greets_you_out_loud_when_summoned(summonable):
    summonable._woken()
    assert "What can I do for you" in summonable.voice.spoken[0]


def test_the_first_summon_of_the_day_brings_the_briefing(app):
    app.hud = FakeHUD()
    app.voice = FakeVoice()
    app._jarvis = FakeJarvis(first_today=True)
    app._woken()
    said = " ".join(app.voice.spoken)
    assert "portfolio stands at" in said
    assert "Fed" in said


def test_the_greeting_comes_before_the_briefing(app):
    """The briefing is the slow part. It goes after hello, not instead of it."""
    app.hud = FakeHUD()
    app.voice = FakeVoice()
    app._jarvis = FakeJarvis(first_today=True)
    app._woken()
    assert "What can I do for you" in app.voice.spoken[0]
    assert "portfolio" in app.voice.spoken[1]


def test_later_summons_the_same_day_are_one_line(summonable):
    """Repeating the whole briefing every time is how useful becomes noise."""
    summonable._woken()
    assert summonable._jarvis.briefed == 0
    assert len(summonable.voice.spoken) == 1


def test_what_he_hears_after_the_greeting_is_answered(app):
    app.hud = FakeHUD()
    app.voice = FakeVoice("how's my portfolio")
    app._woken()
    assert app._jarvis.asked == ["how's my portfolio"]


def test_hearing_nothing_does_not_start_a_turn(summonable):
    summonable._woken()
    assert summonable._jarvis.asked == []


# -------------------------------------------------------------- dismissal


@pytest.mark.parametrize("word", ["goodbye", "thanks", "never mind", "that's all"])
def test_saying_goodbye_puts_the_window_away_at_once(summonable, word):
    summonable._handle(word)
    assert "dismiss" in summonable.hud.actions
    assert summonable._jarvis.asked == [], "a dismissal is not a question"


def test_a_dismissal_gets_an_acknowledgement(summonable):
    summonable._handle("goodbye")
    assert "Standing by." in summonable.voice.spoken


def test_the_window_goes_away_on_its_own_after_a_while(summonable, monkeypatch):
    monkeypatch.setattr("jarvis.app.IDLE_DISMISS_SECONDS", 0.05)
    summonable._handle("hello")
    assert "dismiss" not in summonable.hud.actions, "it should linger, not vanish"
    assert wait_until(lambda: "dismiss" in summonable.hud.actions)


def test_a_follow_up_cancels_the_pending_dismissal(summonable):
    """Asking something else must not have the window vanish mid-answer."""
    summonable._handle("hello")
    first = summonable._dismiss_timer

    summonable._handle("and another thing")

    assert first.finished.is_set(), "the old timer was left running"
    assert summonable._dismiss_timer is not first


def test_being_summoned_again_cancels_a_pending_dismissal(summonable):
    summonable._handle("hello")
    first = summonable._dismiss_timer

    summonable._woken()

    assert first.finished.is_set()


def test_without_a_wake_word_the_window_never_hides(app, monkeypatch):
    """Hiding it with no way to summon it back would strand him."""
    monkeypatch.setattr("jarvis.app.IDLE_DISMISS_SECONDS", 0.05)
    app.hud = FakeHUD()
    app.voice = None
    app._handle("hello")
    time.sleep(0.15)
    assert "dismiss" not in app.hud.actions


def test_shutdown_cancels_a_pending_dismissal(summonable, monkeypatch):
    monkeypatch.setattr("jarvis.app.IDLE_DISMISS_SECONDS", 0.05)
    summonable._handle("hello")
    summonable._shutdown()
    time.sleep(0.15)
    assert "dismiss" not in summonable.hud.actions


def test_without_a_mind_he_falls_back_to_the_router(app, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    app._build()
    assert app.mind is None
    assert app._answer("how's my portfolio") == "answer to how's my portfolio"


def test_the_fallback_is_announced_once_not_silently(app, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    app._build()
    assert any("ANTHROPIC_API_KEY" in note for note in app._notes)


def test_the_audit_log_lands_in_his_home_not_the_repo(app):
    assert app.engine.audit_log == app.config.home / "audit.jsonl"


def test_a_failed_turn_is_answered_rather_than_crashing(app, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    app._build()
    monkeypatch.setattr(app, "_answer", lambda text: 1 / 0)
    app._handle("hello")  # must not raise


def test_shutdown_closes_the_session(app):
    app._shutdown()
    assert app._jarvis.slept


def test_terminal_approval_treats_anything_but_yes_as_no(app, monkeypatch):
    decision = type("D", (), {"reason": "it deletes things"})()
    for answer in ("", "n", "no", "maybe", "sure"):
        monkeypatch.setattr("builtins.input", lambda _: answer)
        assert app._ask_in_terminal("rm x", decision) is False
    monkeypatch.setattr("builtins.input", lambda _: "y")
    assert app._ask_in_terminal("rm x", decision) is True


def test_an_unanswerable_prompt_is_a_refusal(app, monkeypatch):
    """Piped input with nobody there must not mean yes."""
    def no_stdin(_):
        raise EOFError

    monkeypatch.setattr("builtins.input", no_stdin)
    decision = type("D", (), {"reason": "it deletes things"})()
    assert app._ask_in_terminal("rm x", decision) is False
