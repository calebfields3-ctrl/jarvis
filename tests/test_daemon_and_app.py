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

    def __init__(self):
        self.profile = FakeProfile()


class FakeJarvis:
    def __init__(self, first_today=False):
        self.asked = []
        self.slept = False
        self.briefed = 0
        self._first_today = first_today
        self.voice = FakeVoicePersona()
        self.memory = FakeMemory()
        self.spoken_name = "sir"

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


# ------------------------------------------------- the window must be reachable


def test_the_window_starts_visible_when_the_microphone_failed(app, monkeypatch):
    """A hidden window with no working wake word can never be summoned.

    The program would start, appear to do nothing, and there would be no way
    to tell it apart from a crash.
    """
    monkeypatch.setattr("jarvis.app.hud_problem", lambda: None)
    app.use_hud = True
    app.use_voice = True
    monkeypatch.setattr(app, "_probe_voice", lambda: None)  # mic didn't come up

    app._probe_voice()
    app._build()

    assert app.voice is None
    assert app.hud.visible, "the window was hidden with nothing able to summon it"


def test_the_window_starts_hidden_when_the_wake_word_is_live(app, monkeypatch):
    monkeypatch.setattr("jarvis.app.hud_problem", lambda: None)
    app.use_hud = True
    app.voice = FakeVoice()

    app._build()

    assert not app.hud.visible


def test_a_missing_microphone_says_how_to_fix_it(app, monkeypatch):
    class DeadChannel:
        is_voice = False

        def __init__(self, config):
            pass

    monkeypatch.setattr("jarvis.voice.io.VoiceChannel", DeadChannel)
    app.use_voice = True
    app._probe_voice()

    assert app.voice is None
    note = " ".join(app._notes)
    assert "hey Jarvis" in note and "pip install" in note


def test_listening_does_not_begin_before_there_is_a_window_to_summon(app):
    """Waking with no HUD built yet would answer into nothing."""
    app.voice = None
    app._start_voice()
    assert app.daemon is None


def test_voice_is_on_by_default_so_a_correct_install_is_not_silent(home):
    """It used to need an environment variable nobody knew to set."""
    from jarvis.config import Config

    assert Config().voice_enabled is True


def test_voice_can_still_be_turned_off(home, monkeypatch):
    from jarvis.config import Config

    monkeypatch.setenv("JARVIS_VOICE", "0")
    assert Config().voice_enabled is False


# ---------------------------------------------------- the terminal is the home


def test_the_window_pops_up_by_default(home):
    """He asked for it to appear on screen when he says the wake word."""
    from jarvis.config import Config

    assert Config().window is True


def test_the_terminal_is_one_flag_away(home, monkeypatch):
    from jarvis.cli import build_parser
    from jarvis.config import Config

    assert build_parser().parse_args(["start", "--no-window"]).no_window is True

    monkeypatch.setenv("JARVIS_WINDOW", "0")
    assert Config().window is False


def test_both_window_flags_parse():
    from jarvis.cli import build_parser

    args = build_parser().parse_args(["start", "--window"])
    assert args.window is True and args.no_window is False


def test_bare_jarvis_starts_him_rather_than_printing_help(monkeypatch, home):
    """He asked not to have to remember a command to summon him."""
    import jarvis.cli as cli

    started = []
    monkeypatch.setattr(cli, "cmd_start", lambda args, jarvis: started.append(True) or 0)
    monkeypatch.setattr(cli, "Jarvis", lambda *a, **k: FakeJarvis())
    monkeypatch.setattr(FakeJarvis, "memory", FakeMemory(), raising=False)
    monkeypatch.setattr(FakeMemory, "close", lambda self: None, raising=False)

    assert cli.main([]) == 0
    assert started == [True]


def test_voice_can_be_turned_off_from_the_command_line():
    from jarvis.cli import build_parser

    assert build_parser().parse_args(["start", "--no-voice"]).no_voice is True


def test_in_the_terminal_he_speaks_and_prints(app, capsys):
    """Talking to him has to work without a window in the way."""
    app.hud = None
    app.voice = FakeVoice()
    app._speak("Morning, sir.")

    assert app.voice.spoken == ["Morning, sir."]
    assert "Morning, sir." in capsys.readouterr().out


def test_the_wake_word_works_with_no_window(app, capsys):
    app.hud = None
    app.voice = FakeVoice("what's my portfolio")
    app._woken()

    assert app._jarvis.asked == ["what's my portfolio"]
    assert "What can I do for you" in app.voice.spoken[0]


def test_nothing_is_hidden_when_there_is_no_window(app):
    """`_idle` must not try to dismiss a window that does not exist."""
    app.hud = None
    app.voice = FakeVoice()
    app._idle(delay=0.0)  # must not raise
    assert app.hud is None


def test_you_can_see_what_he_touches_from_the_terminal(app, capsys):
    """With a shell and no window, this is the only live visibility there is."""
    import types

    app.hud = None
    app._show_tool_call(types.SimpleNamespace(name="run_command", summary="rm notes.txt", ok=True))
    app._show_tool_call(types.SimpleNamespace(name="read_file", summary="/etc/shadow", ok=False))

    out = capsys.readouterr().out
    assert "run_command" in out and "rm notes.txt" in out
    assert "×" in out, "a refused call should look different from an allowed one"


# ------------------------------------------------- what actually gets spoken


def test_command_examples_are_never_read_aloud():
    """"jarvis deposit 10000" pronounced as words is not a briefing."""
    from jarvis.app import speakable

    said = speakable(
        "Your portfolio is empty, sir. Fund it and I'll keep score:\n"
        "  jarvis deposit 10000\n"
        "  jarvis buy AAPL 10 185.50"
    )
    assert "deposit 10000" not in said
    assert "portfolio is empty" in said


def test_bullets_lose_their_punctuation_but_keep_their_words():
    from jarvis.app import speakable

    said = speakable("Overnight:\n  - Fed signals slower cuts (Reuters)\n  - Oil down 3%")
    assert "Fed signals slower cuts" in said
    assert "- " not in said


def test_a_long_briefing_is_cut_rather_than_read_for_five_minutes():
    from jarvis.app import SPOKEN_LIMIT, speakable

    said = speakable(". ".join(f"Sentence number {i} about the market" for i in range(200)))
    assert len(said) < SPOKEN_LIMIT + 60
    assert said.endswith("The rest is on your screen.")


def test_the_cut_lands_on_a_sentence_end_not_mid_word():
    from jarvis.app import speakable

    said = speakable(" ".join(f"This is sentence {i}." for i in range(100)))
    body = said.replace(" The rest is on your screen.", "")
    assert body.endswith(".")


def test_a_short_answer_is_spoken_exactly_as_written():
    from jarvis.app import speakable

    assert speakable("Hello, Caleb. What can I do for you?") == \
        "Hello, Caleb. What can I do for you?"


def test_the_screen_still_gets_everything(app, capsys):
    """Trimming is for the ear only. Nothing is lost from the transcript."""
    app.hud = None
    app.voice = FakeVoice()
    full = "Portfolio update.\n  jarvis deposit 10000\n  - Fed cut rates"
    app._speak(full)

    assert "jarvis deposit 10000" in capsys.readouterr().out
    assert "deposit 10000" not in app.voice.spoken[0]


# ------------------------------------------------- the conversation stays open


def test_he_keeps_listening_after_answering(app):
    """Saying his name before every sentence is commands, not conversation."""
    app.hud = FakeHUD()
    app.voice = FakeVoice("what's my portfolio", "and NVDA?", "what about AAPL")
    app._woken()

    assert app._jarvis.asked == ["what's my portfolio", "and NVDA?", "what about AAPL"]


def test_silence_ends_the_conversation(app):
    app.hud = FakeHUD()
    app.voice = FakeVoice("one question")
    app._woken()

    assert app._jarvis.asked == ["one question"]
    assert app.voice.heard == []


def test_the_follow_up_window_is_longer_than_the_first(app):
    """After an answer you may be thinking; being cut off feels like a machine."""
    from jarvis.app import FIRST_LISTEN_SECONDS, FOLLOW_UP_SECONDS

    assert FOLLOW_UP_SECONDS > FIRST_LISTEN_SECONDS

    waits = []

    class TimedVoice(FakeVoice):
        def listen(self, timeout=None):
            waits.append(timeout)
            return super().listen(timeout)

    app.hud = FakeHUD()
    app.voice = TimedVoice("first", "second")
    app._woken()

    assert waits[0] == FIRST_LISTEN_SECONDS
    assert waits[1] == FOLLOW_UP_SECONDS


def test_goodbye_ends_the_conversation_mid_flow(app):
    app.hud = FakeHUD()
    app.voice = FakeVoice("what's my portfolio", "goodbye", "this is never heard")
    app._woken()

    assert app._jarvis.asked == ["what's my portfolio"]
    assert "dismiss" in app.hud.actions


def test_no_dismissal_timer_races_the_next_question(app):
    """A timer started mid-conversation could hide the window while he talks."""
    app.hud = FakeHUD()
    app.voice = FakeVoice("one", "two")
    app._handle("one", then_idle=False)

    assert getattr(app, "_dismiss_timer", None) is None


def test_each_turn_in_the_conversation_is_answered(app):
    app.hud = FakeHUD()
    app.voice = FakeVoice("a", "b")
    app._woken()

    spoken = " ".join(app.voice.spoken)
    assert "answer to a" in spoken and "answer to b" in spoken


# ---------------------------------------------- saying why he cannot answer


class ShruggingJarvis(FakeJarvis):
    """A router that has nothing for the question, like the real one."""

    SHRUG = "I've nothing reliable on that, sir."

    def __init__(self):
        super().__init__()
        self.voice = type(
            "V", (FakeVoicePersona,),
            {"unknown_topic": lambda self: ShruggingJarvis.SHRUG,
             "too_vague": lambda self: "Be more specific."},
        )()

    def ask(self, text):
        self.asked.append(text)
        return self.SHRUG


def test_a_shrug_explains_the_missing_key_rather_than_just_shrugging(app):
    """Otherwise it reads as his opinion of the question, not a missing key."""
    app._jarvis = ShruggingJarvis()
    app.mind = None

    answer = app._answer("what's the capital of France")
    assert "API key" in answer
    assert "jarvis doctor" in answer


def test_the_explanation_is_given_once_not_every_time(app):
    app._jarvis = ShruggingJarvis()
    app.mind = None

    first = app._answer("something")
    second = app._answer("something else")
    assert "API key" in first
    assert "API key" not in second


def test_a_real_answer_is_never_padded_with_the_explanation(app):
    app.mind = None
    answer = app._answer("how's my portfolio")
    assert "API key" not in answer


def test_with_a_mind_the_router_is_not_used_at_all(app):
    class FakeMind:
        def say(self, text):
            return types.SimpleNamespace(text="a real answer about anything")

    import types

    app.mind = FakeMind()
    assert app._answer("what's the capital of France") == "a real answer about anything"
    assert app._jarvis.asked == []


# ------------------------------------------------------------- the doctor


def test_the_doctor_never_crashes_on_a_broken_check(monkeypatch):
    """A diagnostic that dies has told you nothing."""
    from jarvis import doctor

    def explode():
        raise RuntimeError("the check itself is broken")

    monkeypatch.setattr(doctor, "CHECKS", (("exploding", explode),))
    checks = doctor.run_all()
    assert checks[0].status == doctor.FAIL
    assert "broken" in checks[0].detail


def test_every_failure_names_a_fix(monkeypatch):
    """"microphone: no" is not a diagnosis."""
    from jarvis import doctor

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    for check in doctor.run_all(skip_network=True):
        if check.status in (doctor.FAIL, doctor.WARN):
            assert check.fix, f"{check.name} failed without saying what to do"


def test_a_missing_key_is_a_failure_not_a_warning(monkeypatch):
    """It is the whole difference between answering anything and only finance."""
    from jarvis import doctor

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    check = doctor.check_mind()
    assert check.status == doctor.FAIL
    assert "finance" in check.detail


def test_a_key_of_the_wrong_shape_is_caught_before_the_network(monkeypatch):
    from jarvis import doctor

    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-real-key")
    assert doctor.check_mind().status == doctor.FAIL


def test_the_key_is_never_printed_in_full(monkeypatch):
    """The report gets pasted into chat windows."""
    from jarvis import doctor

    secret = "sk-ant-abcdefghijklmnopqrstuvwxyz0123456789"
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
    assert secret not in doctor.check_mind().detail


def test_the_report_lists_fixes_for_what_failed(monkeypatch):
    from jarvis import doctor

    checks = [
        doctor.Check("python", doctor.OK, "3.12"),
        doctor.Check("his mind", doctor.FAIL, "not set", "run ./setup.sh"),
        doctor.Check("his voice", doctor.WARN, "robotic", "jarvis voice --install"),
    ]
    text = doctor.report(checks)
    assert "run ./setup.sh" in text
    assert "jarvis voice --install" in text
    assert "FAIL" in text


def test_an_all_clear_says_what_to_do_next():
    from jarvis import doctor

    text = doctor.report([doctor.Check("python", doctor.OK, "3.12")])
    assert "hey Jarvis" in text


# ------------------------------------------------------- the ./run launcher


def test_the_run_script_exists_and_is_executable():
    """`jarvis` needs a new terminal and a working PATH. This needs neither."""
    import os
    from pathlib import Path

    run = Path(__file__).resolve().parent.parent / "run"
    assert run.exists(), "the ./run launcher is missing"
    assert os.access(run, os.X_OK), "./run is not executable, so it cannot be run"


def test_the_run_script_uses_the_venv_python_by_full_path():
    """Anything relying on PATH or activation is what keeps failing."""
    from pathlib import Path

    text = (Path(__file__).resolve().parent.parent / "run").read_text()
    assert ".venv/bin/python" in text
    # Look at what it runs, not what it says -- the comments mention
    # activation precisely to explain why it does not do it.
    code = "\n".join(
        line for line in text.splitlines() if not line.strip().startswith("#")
    )
    assert "source " not in code, "activation is exactly what it must not need"
    assert "activate" not in code


def test_the_run_script_says_what_to_do_when_nothing_is_installed():
    from pathlib import Path

    text = (Path(__file__).resolve().parent.parent / "run").read_text()
    assert "./setup.sh" in text
