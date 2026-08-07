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


class FakeJarvis:
    def __init__(self):
        self.asked = []
        self.slept = False

    def wake(self, channel="text"):
        return "Morning, Caleb."

    def ask(self, text):
        self.asked.append(text)
        return f"answer to {text}"

    def sleep(self, summary=None):
        self.slept = True


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
