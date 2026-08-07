"""Always listening for "hey Jarvis", so nothing has to be typed to start him.

This is the part Caleb actually asked for: he says the words and Jarvis is
just there. No terminal, no command, no window to find first.

The daemon owns one background thread that does nothing but wait on the wake
detector. When it fires, it calls back and goes straight back to waiting --
it does not handle the conversation itself. Keeping it that dumb is what
makes it reliable: the listener blocks on the microphone for as long as it
likes without freezing a window, and a crash in the conversation never takes
the ears down with it.

Microphones fail in ordinary ways -- unplugged, busy, no permission, no
package installed. None of those should stop Jarvis working; they should
degrade him to typed input and say so once.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

log = logging.getLogger(__name__)

# How many times the listener may blow up before we stop trying. A dead
# microphone raises instantly, and retrying forever would spin a core at
# 100% for as long as Jarvis is running.
MAX_CONSECUTIVE_FAILURES = 5


class WakeDaemon:
    """Waits for the wake word in the background and calls back when it hears it."""

    def __init__(
        self,
        detector,
        on_wake: Callable[[], None],
        *,
        on_error: Callable[[str], None] | None = None,
        name: str = "jarvis-wake",
    ) -> None:
        self.detector = detector
        self.on_wake = on_wake
        self.on_error = on_error
        self._name = name
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.wakes = 0
        self.failures = 0

    # -------------------------------------------------------------- control
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        # Daemon thread: a blocking microphone read cannot be interrupted, so
        # a non-daemon thread here would hang the whole process on exit.
        self._thread = threading.Thread(target=self._loop, name=self._name, daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        closer = getattr(self.detector, "close", None)
        if closer is not None:
            try:
                closer()
            except Exception:
                log.debug("closing the wake detector raised", exc_info=True)
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ----------------------------------------------------------------- loop
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                heard = self.detector.wait_for_wake()
            except Exception as exc:
                self.failures += 1
                log.debug("wake detector raised", exc_info=True)
                if self.failures >= MAX_CONSECUTIVE_FAILURES:
                    self._report(
                        "I've lost the microphone, sir. Type to me instead. "
                        f"({exc})"
                    )
                    return
                continue

            if self._stop.is_set():
                return
            if not heard:
                # A clean False means the input stream ended -- end of piped
                # input, or the listener was closed under us. Retrying would
                # spin, so stop.
                return

            self.failures = 0
            self.wakes += 1
            try:
                self.on_wake()
            except Exception:
                # The conversation falling over must not cost him his ears.
                log.exception("the wake handler raised")

    def _report(self, message: str) -> None:
        if self.on_error is not None:
            try:
                self.on_error(message)
            except Exception:
                log.debug("error hook raised", exc_info=True)
        else:
            log.warning("%s", message)
