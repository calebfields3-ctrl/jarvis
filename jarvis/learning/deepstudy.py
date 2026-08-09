"""Studying while Caleb is out.

"Hey Jarvis, I'm leaving, go study the market" is a different shape of work
from anything else he does. Every other task is a question with an answer at
the end of it. This one has no answer -- it runs until he comes back, and
what it produces is not a reply but a changed Jarvis.

So it is built as a background worker rather than a tool call that returns.
One cycle of :meth:`Jarvis.learn_cycle` takes tens of seconds; this repeats
it, spacing the passes out, until a deadline or until told to stop.

Three things it must not do, each of which is a way for a well-meaning
background loop to become a problem:

* **Run forever.** There is a hard deadline, and a cap on cycles. An
  unattended loop against paid APIs and other people's servers needs a stop
  that does not depend on anyone remembering to press it.
* **Hammer anyone.** It pauses between passes. Studying is not scraping, and
  the difference is mostly politeness about rate.
* **Die quietly.** A failed cycle is logged and the next one runs. Coming
  home to a worker that stopped an hour in without saying so is worse than
  it never having started.

What it did is recorded as it goes, not at the end, so an interrupted
session still has something to report.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Callable

log = logging.getLogger(__name__)

# Where the record of a session lives, so the next greeting can report it.
PROGRESS_KEY = "away_study"

DEFAULT_HOURS = 2.0
# Nothing is allowed to ask for more than this in one go. A typo in an hours
# argument should not mean a loop running until the laptop dies.
MAX_HOURS = 12.0
DEFAULT_PAUSE_SECONDS = 90.0


@dataclass
class StudyProgress:
    """What a session has done so far. Written after every cycle."""

    topic: str = ""
    started_at: float = 0.0
    finished_at: float | None = None
    cycles: int = 0
    sources_read: int = 0
    lessons_learned: int = 0
    signals_graded: int = 0
    promoted: int = 0
    retired: int = 0
    errors: list[str] = field(default_factory=list)
    stopped_because: str = ""

    @property
    def minutes(self) -> float:
        end = self.finished_at or time.time()
        return max(0.0, (end - self.started_at) / 60.0)


class StudySession:
    """Repeats the learning cycle in the background until told to stop."""

    def __init__(
        self,
        jarvis,
        *,
        topic: str = "",
        hours: float = DEFAULT_HOURS,
        pause_seconds: float = DEFAULT_PAUSE_SECONDS,
        max_cycles: int = 500,
        on_note: Callable[[str], None] | None = None,
    ) -> None:
        self.jarvis = jarvis
        self.topic = topic.strip()
        self.hours = max(0.0, min(float(hours), MAX_HOURS))
        self.pause_seconds = max(1.0, pause_seconds)
        self.max_cycles = max_cycles
        self.on_note = on_note

        self.progress = StudyProgress(topic=self.topic)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # -------------------------------------------------------------- control
    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self.progress = StudyProgress(topic=self.topic, started_at=time.time())
        self._save()
        self._thread = threading.Thread(
            target=self._loop, name="jarvis-study", daemon=True
        )
        self._thread.start()

    def stop(self, reason: str = "you asked me to stop", timeout: float = 5.0) -> None:
        """Ask it to finish. Returns once the current cycle has wound up."""
        if not self.running:
            return
        self.progress.stopped_because = reason
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self._finish(reason)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ----------------------------------------------------------------- loop
    def _loop(self) -> None:
        deadline = self.progress.started_at + self.hours * 3600

        while not self._stop.is_set():
            if time.time() >= deadline:
                self._finish("I ran out of the time you gave me")
                return
            if self.progress.cycles >= self.max_cycles:
                self._finish("I'd done as many passes as I'm allowed")
                return

            try:
                self._one_cycle()
            except Exception as exc:
                # One bad cycle is a bad cycle, not the end of the session.
                # A dropped connection mid-study is ordinary.
                log.warning("study cycle failed: %s", exc)
                self.progress.errors.append(str(exc)[:200])

            self.progress.cycles += 1
            self._save()

            # Interruptible sleep: `stop` should not wait out a 90s pause.
            if self._stop.wait(self.pause_seconds):
                return

    def _one_cycle(self) -> None:
        """One pass: study, distil, grade. The same work he does when asked."""
        if self.topic:
            report = self.jarvis.web.study(
                self.jarvis.memory.knowledge, self.topic, max_pages=3
            )
            self.progress.sources_read += len(report.opened)
            if report.error:
                self.progress.errors.append(report.error[:200])

        result = self.jarvis.learn_cycle(study_web=not self.topic)

        web = result.get("web") or {}
        self.progress.sources_read += int(web.get("pages_read", 0) or 0)
        for key in ("youtube", "social"):
            block = result.get(key) or {}
            self.progress.sources_read += int(block.get("videos") or block.get("items") or 0)

        distilled = result.get("distilled") or {}
        self.progress.lessons_learned += int(distilled.get("lessons", 0) or 0)

        evaluation = result.get("evaluation") or {}
        self.progress.signals_graded += int(evaluation.get("graded", 0) or 0)
        self.progress.promoted += len(evaluation.get("promoted") or ())
        self.progress.retired += len(evaluation.get("retired") or ())

    def _finish(self, reason: str) -> None:
        if self.progress.finished_at is None:
            self.progress.finished_at = time.time()
        if not self.progress.stopped_because:
            self.progress.stopped_because = reason
        self._save()
        if self.on_note is not None:
            try:
                self.on_note(self.describe())
            except Exception:
                log.debug("study note hook raised", exc_info=True)

    # ------------------------------------------------------------- reporting
    def _save(self) -> None:
        try:
            self.jarvis.memory.profile.set(PROGRESS_KEY, json.dumps(asdict(self.progress)))
        except Exception:
            # Losing the record must not stop the studying.
            log.debug("couldn't save study progress", exc_info=True)

    def describe(self) -> str:
        return describe_progress(self.progress, running=self.running)


def describe_progress(progress: StudyProgress, *, running: bool = False) -> str:
    """What he says about a study session, finished or in flight."""
    if not progress.started_at:
        return "I haven't studied on my own yet."

    span = _humanise(progress.minutes)
    opener = (
        f"I'm still at it -- {span} so far"
        if running else
        f"While you were out I studied for {span}"
    )
    if progress.topic:
        opener += f", on {progress.topic}"

    lines = [
        f"{opener}: {progress.cycles} passes, "
        f"{progress.sources_read} sources read, "
        f"{progress.lessons_learned} new lessons."
    ]

    if progress.signals_graded:
        lines.append(
            f"I graded {progress.signals_graded} of my own calls against what "
            "actually happened."
        )
    # Retiring a pattern is the more useful half and the easier half to bury,
    # so it is said plainly rather than folded into a total.
    if progress.promoted or progress.retired:
        parts = []
        if progress.promoted:
            parts.append(f"{progress.promoted} patterns earned their place")
        if progress.retired:
            parts.append(f"{progress.retired} turned out not to work and I've dropped them")
        lines.append(" and ".join(parts).capitalize() + ".")

    if progress.errors:
        failed, total = len(progress.errors), progress.cycles
        reason = sorted(set(progress.errors))[0]
        if total and failed >= total:
            # Everything failed. Saying "the rest went through" here would be
            # a comfortable sentence that isn't true, and the whole point of
            # him is that he doesn't do that.
            lines = [
                f"{opener}, but I got nowhere -- every one of the "
                f"{total} passes failed. Last reason: {reason}"
            ]
        else:
            lines.append(
                f"{failed} of {total} passes hit trouble ({reason}); "
                "the rest went through."
            )
    if not running and progress.stopped_because:
        lines.append(f"I stopped because {progress.stopped_because}.")
    return "\n".join(lines)


def _humanise(minutes: float) -> str:
    """A duration in words. '0 minutes' reads as nothing having happened."""
    if minutes < 1:
        return "under a minute"
    if minutes < 90:
        return f"{minutes:.0f} minute{'s' if minutes >= 1.5 else ''}"
    hours = minutes / 60
    return f"{hours:.1f} hours"


def load_progress(memory) -> StudyProgress | None:
    """The last recorded session, or None if he's never studied alone."""
    raw = memory.profile.get(PROGRESS_KEY)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    known = {f for f in StudyProgress.__dataclass_fields__}
    return StudyProgress(**{k: v for k, v in data.items() if k in known})


def clear_progress(memory) -> None:
    """Forget the last session, so it is not reported twice."""
    try:
        memory.profile.set(PROGRESS_KEY, "")
    except Exception:
        log.debug("couldn't clear study progress", exc_info=True)
