"""Studying while Caleb is away.

An unattended loop against paid APIs and other people's servers is the one
piece of this that can misbehave for hours without anyone watching. Most of
these tests are about it stopping.
"""

from __future__ import annotations

import json
import time
import types

import pytest

from jarvis.learning.deepstudy import (
    MAX_HOURS,
    PROGRESS_KEY,
    StudyProgress,
    StudySession,
    clear_progress,
    describe_progress,
    load_progress,
)


class FakeProfile:
    def __init__(self):
        self.values = {}

    def get(self, key, default=None):
        return self.values.get(key, default)

    def set(self, key, value):
        self.values[key] = value


class FakeMemory:
    def __init__(self):
        self.profile = FakeProfile()
        self.knowledge = object()


class FakeJarvis:
    """Counts cycles and can be told to fail."""

    def __init__(self, *, fail_times=0, lessons=2, graded=5):
        self.memory = FakeMemory()
        self.cycles = 0
        self.topics = []
        self._fail_times = fail_times
        self._lessons = lessons
        self._graded = graded
        self.web = types.SimpleNamespace(study=self._study)

    def _study(self, knowledge, topic, max_pages=3):
        self.topics.append(topic)
        return types.SimpleNamespace(opened=[1, 2], blocked=[], error=None)

    def learn_cycle(self, study_web=True):
        self.cycles += 1
        if self._fail_times > 0:
            self._fail_times -= 1
            raise RuntimeError("the network went away")
        return {
            "web": {"pages_read": 3},
            "youtube": {"videos": 1},
            "social": {"items": 1},
            "distilled": {"lessons": self._lessons},
            "evaluation": {
                "graded": self._graded,
                "promoted": ["breakout"],
                "retired": ["death_cross", "hammer"],
            },
        }


def wait_until(predicate, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def session(jarvis, **kwargs):
    kwargs.setdefault("pause_seconds", 0.01)
    kwargs.setdefault("hours", 1.0)
    return StudySession(jarvis, **kwargs)


# ------------------------------------------------------------ it does work


def test_it_keeps_studying_rather_than_running_once(reactor_free=None):
    """A single pass is what the ordinary `study` tool already does."""
    jarvis = FakeJarvis()
    study = session(jarvis)
    study.start()
    try:
        assert wait_until(lambda: jarvis.cycles >= 3)
    finally:
        study.stop()


def test_it_returns_immediately_so_he_can_say_goodbye():
    """Blocking here would leave Caleb standing at the door."""
    jarvis = FakeJarvis()
    study = session(jarvis)
    began = time.time()
    study.start()
    try:
        assert time.time() - began < 0.5
    finally:
        study.stop()


def test_a_topic_is_actually_studied():
    jarvis = FakeJarvis()
    study = session(jarvis, topic="opening range breakouts")
    study.start()
    try:
        assert wait_until(lambda: "opening range breakouts" in jarvis.topics)
    finally:
        study.stop()


def test_progress_accumulates_across_passes():
    jarvis = FakeJarvis()
    study = session(jarvis)
    study.start()
    try:
        assert wait_until(lambda: study.progress.cycles >= 2)
        study.stop()
        assert study.progress.lessons_learned >= 4
        assert study.progress.sources_read >= 10
        assert study.progress.signals_graded >= 10
        assert study.progress.retired >= 4
    finally:
        study.stop()


# --------------------------------------------------------------- it stops


def test_it_stops_when_asked():
    jarvis = FakeJarvis()
    study = session(jarvis)
    study.start()
    study.stop()
    assert not study.running


def test_stopping_does_not_wait_out_the_pause():
    """A 90-second sleep must be interruptible or 'stop' means 'eventually'."""
    jarvis = FakeJarvis()
    study = session(jarvis, pause_seconds=30.0)
    study.start()
    wait_until(lambda: jarvis.cycles >= 1)

    began = time.time()
    study.stop()
    assert time.time() - began < 2.0


def test_it_stops_itself_at_the_deadline():
    """Nobody is there to press stop. It has to end on its own."""
    jarvis = FakeJarvis()
    study = session(jarvis, hours=0.0001)  # ~0.36 seconds
    study.start()
    assert wait_until(lambda: not study.running, timeout=5)
    assert "time" in study.progress.stopped_because


def test_no_one_can_ask_for_an_unbounded_run():
    """A typo in an hours argument shouldn't run until the laptop dies."""
    study = StudySession(FakeJarvis(), hours=10_000)
    assert study.hours == MAX_HOURS


def test_a_negative_duration_is_not_negative():
    assert StudySession(FakeJarvis(), hours=-5).hours == 0.0


def test_the_cycle_cap_ends_it_even_with_time_left():
    jarvis = FakeJarvis()
    study = session(jarvis, hours=5.0, max_cycles=3)
    study.start()
    assert wait_until(lambda: not study.running, timeout=5)
    assert jarvis.cycles <= 4


def test_starting_twice_does_not_run_two_loops():
    jarvis = FakeJarvis()
    study = session(jarvis)
    study.start()
    first = study._thread
    study.start()
    try:
        assert study._thread is first
    finally:
        study.stop()


def test_the_thread_never_holds_the_process_open():
    jarvis = FakeJarvis()
    study = session(jarvis)
    study.start()
    try:
        assert study._thread.daemon
    finally:
        study.stop()


# ------------------------------------------------------------- it survives


def test_a_failed_pass_does_not_end_the_session():
    """A dropped connection mid-study is ordinary, not fatal."""
    jarvis = FakeJarvis(fail_times=2)
    study = session(jarvis)
    study.start()
    try:
        # Passes are a second apart at minimum, so this waits in seconds.
        assert wait_until(lambda: jarvis.cycles >= 3, timeout=8)
        assert study.running
        assert len(study.progress.errors) == 2
    finally:
        study.stop()


def test_it_pauses_between_passes_however_it_is_configured():
    """Studying is not scraping, and the difference is mostly rate."""
    assert StudySession(FakeJarvis(), pause_seconds=0).pause_seconds >= 1.0
    assert StudySession(FakeJarvis(), pause_seconds=-10).pause_seconds >= 1.0


def test_failures_are_reported_rather_than_hidden():
    """However it's phrased, a session that failed must say so."""
    jarvis = FakeJarvis(fail_times=2)
    study = session(jarvis)
    study.start()
    wait_until(lambda: len(study.progress.errors) >= 2)
    study.stop()

    said = study.describe()
    assert "the network went away" in said
    assert any(word in said for word in ("trouble", "failed", "got nowhere"))


def test_an_unwritable_record_does_not_stop_the_studying():
    jarvis = FakeJarvis()

    def explode(key, value):
        raise OSError("disk full")

    jarvis.memory.profile.set = explode
    study = session(jarvis)
    study.start()
    try:
        assert wait_until(lambda: jarvis.cycles >= 2)
    finally:
        study.stop()


# ------------------------------------------------------------- it reports


def test_progress_is_saved_as_it_goes_not_only_at_the_end():
    """An interrupted session still has to have something to report."""
    jarvis = FakeJarvis()
    study = session(jarvis)
    study.start()
    try:
        assert wait_until(lambda: jarvis.cycles >= 2)
        saved = json.loads(jarvis.memory.profile.get(PROGRESS_KEY))
        assert saved["cycles"] >= 1
    finally:
        study.stop()


def test_the_report_says_what_it_actually_did():
    progress = StudyProgress(
        topic="options flow", started_at=time.time() - 3600,
        finished_at=time.time(), cycles=20, sources_read=140,
        lessons_learned=12, signals_graded=80, promoted=2, retired=3,
    )
    said = describe_progress(progress)
    assert "options flow" in said
    assert "140 sources" in said and "12 new lessons" in said
    assert "80" in said


def test_the_report_names_what_it_threw_away():
    """Retiring a pattern is the useful half and the easy half to bury."""
    progress = StudyProgress(started_at=time.time(), cycles=3, retired=4)
    assert "not to work" in describe_progress(progress)


def test_a_running_session_reports_as_still_going():
    progress = StudyProgress(started_at=time.time(), cycles=2)
    assert "still at it" in describe_progress(progress, running=True)


def test_nothing_studied_says_so_plainly():
    assert "haven't studied" in describe_progress(StudyProgress())


def test_progress_survives_a_restart():
    memory = FakeMemory()
    memory.profile.set(PROGRESS_KEY, json.dumps({
        "topic": "vwap", "started_at": 1.0, "finished_at": 61.0,
        "cycles": 4, "sources_read": 20, "lessons_learned": 3,
        "signals_graded": 0, "promoted": 0, "retired": 0,
        "errors": [], "stopped_because": "done",
    }))
    loaded = load_progress(memory)
    assert loaded.topic == "vwap" and loaded.cycles == 4


def test_an_unknown_field_in_the_record_does_not_break_loading():
    """An older or newer Jarvis wrote it. Load what makes sense."""
    memory = FakeMemory()
    memory.profile.set(PROGRESS_KEY, json.dumps({"cycles": 2, "from_the_future": True}))
    assert load_progress(memory).cycles == 2


def test_a_corrupt_record_is_ignored_not_raised():
    memory = FakeMemory()
    memory.profile.set(PROGRESS_KEY, "{not json")
    assert load_progress(memory) is None


def test_no_record_at_all_is_fine():
    assert load_progress(FakeMemory()) is None


def test_clearing_stops_it_being_reported_twice():
    memory = FakeMemory()
    memory.profile.set(PROGRESS_KEY, json.dumps({"cycles": 2}))
    clear_progress(memory)
    assert load_progress(memory) is None


# ----------------------------------------------------- reported on return


def test_he_tells_you_what_he_learned_when_you_get_back(jarvis):
    jarvis.memory.profile.set(PROGRESS_KEY, json.dumps({
        "topic": "vwap", "started_at": time.time() - 1800,
        "finished_at": time.time(), "cycles": 9, "sources_read": 60,
        "lessons_learned": 7, "signals_graded": 30, "promoted": 1,
        "retired": 2, "errors": [], "stopped_because": "I ran out of time",
    }))
    said = jarvis.summoned()
    assert "vwap" in said and "60 sources" in said


def test_the_same_session_is_not_reported_forever(jarvis):
    jarvis.memory.profile.set(PROGRESS_KEY, json.dumps({
        "topic": "vwap", "started_at": time.time() - 60, "cycles": 3,
        "sources_read": 10, "lessons_learned": 1,
    }))
    assert "vwap" in jarvis.summoned()
    assert "vwap" not in jarvis.summoned()


def test_a_session_that_never_ran_is_not_announced(jarvis):
    jarvis.memory.profile.set(PROGRESS_KEY, json.dumps({"cycles": 0}))
    assert "sources read" not in jarvis.summoned()


def test_a_total_failure_is_not_dressed_up_as_partial_success():
    """"The rest went through" when nothing did is a comfortable lie."""
    progress = StudyProgress(
        started_at=time.time() - 600, finished_at=time.time(),
        cycles=3, errors=["no network"] * 3,
    )
    said = describe_progress(progress)
    assert "got nowhere" in said
    assert "the rest went through" not in said
    assert "no network" in said, "he should say why"


def test_a_partial_failure_says_how_many_of_how_many():
    progress = StudyProgress(
        started_at=time.time() - 600, finished_at=time.time(),
        cycles=10, sources_read=40, lessons_learned=3, errors=["timed out"] * 2,
    )
    said = describe_progress(progress)
    assert "2 of 10" in said
    assert "the rest went through" in said


@pytest.mark.parametrize("minutes,expected", [
    (0.2, "under a minute"),
    (1.0, "1 minute"),
    (45.0, "45 minutes"),
    (240.0, "4.0 hours"),
])
def test_durations_read_like_a_person_said_them(minutes, expected):
    """"0 minutes" reads as nothing having happened."""
    progress = StudyProgress(
        started_at=time.time() - minutes * 60, finished_at=time.time(), cycles=2,
    )
    assert expected in describe_progress(progress)
