"""The arc reactor.

Caleb asked for amazing art, and the honest constraint is that this has to
run on a Chromebook's Linux container with whatever is already installed.
That rules out a game engine and rules in tkinter's Canvas, which draws
arcs, lines, and ovals and nothing else. So the look is built out of what
concentric arcs can do: layered rings at different speeds, a glow made by
stacking translucent-looking outlines, and a sweep line that reads as a
scan. Restraint is doing most of the work -- three colours, one shape, and
motion that means something.

The motion is not decoration. Each ring speed and colour maps to a state,
so the reactor answers "is it listening to me?" without a word of text:

    idle       slow drift, dim              he is there, not listening
    listening  fast spin, bright, pulsing   your voice is going in
    thinking   medium, tools ticking past   he is working
    speaking   pulse on the syllables       that is him talking

The state machine and the geometry live in :class:`ReactorState`, which
imports nothing. tkinter is imported inside :class:`HUD` and nowhere else,
so the rest of Jarvis -- and these tests -- run on a box that has no display
and no python3-tk.
"""

from __future__ import annotations

import logging
import math
import queue
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

log = logging.getLogger(__name__)

# ---------------------------------------------------------------- palette
BACKDROP = "#05080d"
PANEL = "#0a1018"
CYAN = (0x22, 0xD3, 0xEE)
PALE = (0xE0, 0xF7, 0xFF)
AMBER = (0xF5, 0x9E, 0x0B)   # approval pending -- the one thing that isn't cyan
RED = (0xEF, 0x44, 0x44)     # refused

RING_COUNT = 4


class Mode(str, Enum):
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"
    ASKING = "asking"        # waiting on Caleb to approve something


# Per-mode: revolutions per second, base brightness, pulse depth, colour.
MODE_STYLE: dict[Mode, tuple[float, float, float, tuple[int, int, int]]] = {
    Mode.IDLE:      (0.05, 0.30, 0.06, CYAN),
    Mode.LISTENING: (0.45, 1.00, 0.30, PALE),
    Mode.THINKING:  (0.22, 0.75, 0.12, CYAN),
    Mode.SPEAKING:  (0.12, 0.95, 0.40, PALE),
    Mode.ASKING:    (0.08, 0.90, 0.55, AMBER),
}


def blend(colour: tuple[int, int, int], level: float) -> str:
    """A colour dimmed toward the backdrop, as a hex string.

    tkinter has no alpha channel, so "dim" has to be mixed by hand against
    the background it will sit on. Levels outside 0..1 are clamped rather
    than rejected: a pulse that overshoots slightly should saturate, not
    raise in the middle of a redraw.
    """
    level = max(0.0, min(1.0, level))
    base = (0x05, 0x08, 0x0D)
    mixed = tuple(int(b + (c - b) * level) for b, c in zip(base, colour))
    return "#%02x%02x%02x" % mixed


@dataclass
class ReactorState:
    """Where every ring is right now, and how bright.

    Pure arithmetic -- no tkinter, no window, no clock of its own. The HUD
    drives it with :meth:`tick` and reads the results back out, which is
    what makes the animation testable on a machine with no display.
    """

    mode: Mode = Mode.IDLE
    phase: float = 0.0
    pulse: float = 0.0
    # Rises when he speaks or listens, decays on its own. Gives the reactor
    # its heartbeat instead of a mechanical throb.
    energy: float = 0.0

    def set_mode(self, mode: Mode) -> None:
        if mode != self.mode:
            self.mode = mode
            # A mode change kicks the reactor, so switching states reads as
            # a surge rather than a jump cut.
            self.energy = min(1.0, self.energy + 0.5)

    def tick(self, dt: float) -> None:
        """Advance by ``dt`` seconds."""
        speed, _, _, _ = MODE_STYLE[self.mode]
        self.phase = (self.phase + speed * dt) % 1.0
        self.pulse = (self.pulse + dt * (1.6 if self.mode is Mode.SPEAKING else 0.8)) % 1.0
        # Exponential decay, framerate-independent: halves about every 0.7s
        # whatever the tick rate, so a slow frame doesn't produce a lurch.
        self.energy *= math.exp(-dt / 0.7)

    def excite(self, amount: float = 0.6) -> None:
        """Something happened -- a syllable, a tool call. Flare the reactor."""
        self.energy = min(1.0, self.energy + amount)

    # ------------------------------------------------------------ geometry
    def ring_angle(self, index: int) -> float:
        """Degrees of rotation for one ring.

        Alternate rings turn the other way and at different rates, which is
        what stops four concentric circles from reading as one solid disc.
        """
        direction = 1 if index % 2 == 0 else -1
        rate = 1.0 + index * 0.35
        return (self.phase * 360.0 * rate * direction) % 360.0

    def ring_radius(self, index: int, base: float) -> float:
        """Radius including the breathing pulse.

        Outer rings move more than inner ones, so the reactor breathes from
        the edge rather than swelling like a balloon.
        """
        _, _, depth, _ = MODE_STYLE[self.mode]
        breath = math.sin(self.pulse * math.tau + index * 0.8)
        swell = depth * (0.4 + 0.6 * self.energy) * (0.5 + index / RING_COUNT)
        return base * (1.0 + breath * swell * 0.12)

    def ring_colour(self, index: int) -> str:
        _, brightness, _, colour = MODE_STYLE[self.mode]
        # Inner rings sit brighter; the falloff is what gives it depth.
        falloff = 1.0 - (index / (RING_COUNT + 1)) * 0.55
        level = brightness * falloff * (0.65 + 0.35 * self.energy)
        return blend(colour, level)

    def core_colour(self) -> str:
        _, brightness, _, colour = MODE_STYLE[self.mode]
        return blend(colour, min(1.0, brightness * (0.7 + 0.5 * self.energy)))

    def core_radius(self, base: float) -> float:
        return base * (0.85 + 0.30 * self.energy)

    def sweep_angle(self) -> float:
        """The scan line. Always turns the same way, so it reads as a sweep."""
        return (self.phase * 360.0 * 2.0) % 360.0


def hud_available() -> bool:
    """Whether a window can actually be opened here.

    Two ways this fails and they need different advice, so the caller gets a
    bool and :func:`hud_problem` explains.
    """
    return hud_problem() is None


def hud_problem() -> str | None:
    """What's stopping the HUD, in words Caleb can act on."""
    try:
        import tkinter  # noqa: F401
    except ImportError:
        return (
            "The window toolkit isn't installed. Run this once:\n"
            "    sudo apt install -y python3-tk"
        )
    try:
        import tkinter

        root = tkinter.Tk()
        root.destroy()
    except Exception:
        return (
            "There's no display to open a window on. On a Chromebook that "
            "usually means the Linux container needs restarting."
        )
    return None


class HUD:
    """The window: arc reactor, transcript, and what he's doing right now.

    Everything that changes the display goes through a queue and is applied
    on the tkinter thread. Widgets are not thread-safe, and the voice
    listener, the agent loop, and the animation all run on different threads
    -- touching a Canvas from any of them is the kind of bug that shows up
    once a week and never in a test.
    """

    WIDTH = 900
    HEIGHT = 620
    FRAME_MS = 33  # ~30fps; enough for this, cheap on a Chromebook

    # Declared on the class, not only assigned in __init__, so this face and
    # the browser one present the same surface -- `jarvis.app` drives either
    # without knowing which it holds, and a difference here shows up as an
    # AttributeError at runtime rather than as a failing test.
    visible = True

    def __init__(
        self,
        *,
        title: str = "J.A.R.V.I.S.",
        on_submit: Callable[[str], None] | None = None,
        on_close: Callable[[], None] | None = None,
        start_hidden: bool = False,
    ) -> None:
        self.on_submit = on_submit
        self.on_close = on_close
        # Hidden until summoned, when there's a wake word to summon him with.
        # Without one, a window that never appears is just a broken program.
        self.visible = not start_hidden
        self._start_hidden = start_hidden
        self.state = ReactorState()
        self._events: queue.Queue = queue.Queue()
        self._closed = threading.Event()
        self._title = title
        self._root = None
        self._canvas = None
        self._entry = None
        self._transcript = None
        self._status_text = "standing by"

    # ----------------------------------------------------- thread-safe API
    def set_mode(self, mode: Mode) -> None:
        self._events.put(("mode", mode))

    def say(self, text: str, *, who: str = "jarvis") -> None:
        self._events.put(("line", (who, text)))

    def status(self, text: str) -> None:
        self._events.put(("status", text))

    def tool_call(self, call) -> None:
        """Show a tool call in the ticker. Takes a :class:`ToolCall`."""
        self._events.put(("tool", call))

    def summon(self) -> None:
        """Bring the window up in front of whatever Caleb is looking at."""
        self._events.put(("summon", None))

    def dismiss(self) -> None:
        """Put it away again, still listening."""
        self._events.put(("dismiss", None))

    def close(self) -> None:
        self._events.put(("close", None))

    def ask(self, action: str, decision) -> bool:
        """Put a yes/no in front of Caleb and block until he answers.

        Called from the agent's worker thread. The prompt itself is queued
        like every other update so it is built on the tkinter thread, and the
        answer comes back through a queue of its own.

        With no window open this returns False. An approval prompt nobody can
        see is not an approval, and defaulting to yes here would quietly undo
        the entire permission layer.
        """
        if self._root is None or self._closed.is_set():
            return False
        answer: queue.Queue = queue.Queue(maxsize=1)
        self._events.put(("ask", (action, decision, answer)))
        try:
            return answer.get(timeout=300)
        except queue.Empty:
            # He walked away. Silence is not consent.
            return False

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    # ---------------------------------------------------------------- run
    def run(self) -> None:
        """Open the window and block until it closes. Must be the main thread."""
        import tkinter as tk

        self._root = tk.Tk()
        self._root.title(self._title)
        self._root.configure(bg=BACKDROP)
        self._root.geometry(f"{self.WIDTH}x{self.HEIGHT}")
        self._root.protocol("WM_DELETE_WINDOW", self._handle_close)

        self._canvas = tk.Canvas(
            self._root, width=self.WIDTH, height=380,
            bg=BACKDROP, highlightthickness=0,
        )
        self._canvas.pack(fill="both", expand=False)

        self._transcript = tk.Text(
            self._root, bg=PANEL, fg=blend(PALE, 0.85), height=8,
            font=("DejaVu Sans Mono", 10), relief="flat",
            padx=16, pady=10, wrap="word", state="disabled",
        )
        self._transcript.pack(fill="both", expand=True, padx=14, pady=(0, 8))
        self._transcript.tag_configure("jarvis", foreground=blend(CYAN, 0.95))
        self._transcript.tag_configure("caleb", foreground=blend(PALE, 0.7))
        self._transcript.tag_configure("tool", foreground=blend(CYAN, 0.45))
        self._transcript.tag_configure("refused", foreground=blend(RED, 0.9))

        self._entry = tk.Entry(
            self._root, bg=PANEL, fg=blend(PALE, 0.9),
            insertbackground=blend(CYAN, 1.0), relief="flat",
            font=("DejaVu Sans Mono", 11),
        )
        self._entry.pack(fill="x", padx=14, pady=(0, 14), ipady=8)
        self._entry.bind("<Return>", self._handle_submit)
        self._entry.focus_set()

        if self._start_hidden:
            self._root.withdraw()

        self._root.after(self.FRAME_MS, self._frame)
        self._root.mainloop()

    # ------------------------------------------------------------- drawing
    def _frame(self) -> None:
        if self._root is None:
            return
        self._drain()
        if self.visible:
            # No point animating a withdrawn window. While he's waiting to be
            # summoned this loop should cost nothing -- it may sit there all
            # day on a laptop battery.
            self.state.tick(self.FRAME_MS / 1000.0)
            self._draw()
        if not self._closed.is_set():
            self._root.after(self.FRAME_MS if self.visible else 250, self._frame)

    def _drain(self) -> None:
        """Apply everything queued since the last frame, on this thread."""
        while True:
            try:
                kind, payload = self._events.get_nowait()
            except queue.Empty:
                return
            try:
                self._apply(kind, payload)
            except Exception:
                # A malformed update must not kill the render loop and take
                # the whole window with it.
                log.debug("HUD update failed", exc_info=True)

    def _apply(self, kind: str, payload) -> None:
        if kind == "mode":
            self.state.set_mode(payload)
        elif kind == "status":
            self._status_text = payload
        elif kind == "line":
            who, text = payload
            self._append(text, tag="jarvis" if who == "jarvis" else "caleb", prefix=(
                "J: " if who == "jarvis" else "C: "
            ))
            self.state.excite(0.5)
        elif kind == "tool":
            call = payload
            tag = "tool" if call.ok else "refused"
            mark = "·" if call.ok else "×"
            self._append(f"{mark} {call.name}  {call.summary}", tag=tag, prefix="   ")
            self.state.excite(0.35)
        elif kind == "summon":
            self.visible = True
            if self._root is not None:
                self._root.deiconify()
                # lift() alone loses to a full-screen Chrome window, which is
                # exactly what will be in front of it. The topmost flag is set
                # and immediately cleared so it comes forward once rather than
                # sitting above everything for the rest of the session.
                self._root.attributes("-topmost", True)
                self._root.lift()
                self._root.after(200, lambda: self._root.attributes("-topmost", False))
                if self._entry is not None:
                    self._entry.focus_force()
        elif kind == "dismiss":
            self.visible = False
            if self._root is not None:
                self._root.withdraw()
        elif kind == "ask":
            action, decision, answer = payload
            answer.put(self._prompt(action, decision))
        elif kind == "close":
            self._handle_close()

    def _prompt(self, action: str, decision) -> bool:
        """The modal. Blocking the frame loop here is the point."""
        from tkinter import messagebox

        previous, self._status_text = self._status_text, "awaiting your call"
        self.state.set_mode(Mode.ASKING)
        self._append(f"? {action}", tag="refused", prefix="   ")
        self._draw()
        try:
            approved = messagebox.askyesno(
                "Jarvis needs your say-so",
                f"{action}\n\n{decision.reason}\n\nGo ahead?",
                default="no",   # the safe answer is the one Enter picks
                icon="warning",
            )
        except Exception:
            log.debug("approval prompt failed", exc_info=True)
            approved = False
        self._append("   -> yes" if approved else "   -> no", tag="tool", prefix="   ")
        self._status_text = previous
        self.state.set_mode(Mode.THINKING)
        return approved

    def _append(self, text: str, *, tag: str, prefix: str = "") -> None:
        if self._transcript is None:
            return
        self._transcript.configure(state="normal")
        self._transcript.insert("end", f"{prefix}{text}\n", tag)
        self._transcript.see("end")
        self._transcript.configure(state="disabled")

    def _draw(self) -> None:
        canvas = self._canvas
        if canvas is None:
            return
        canvas.delete("all")

        cx, cy = self.WIDTH / 2, 190
        base = 120.0

        # Rings, outermost first so inner ones paint over the joins.
        for index in reversed(range(RING_COUNT)):
            radius = self.state.ring_radius(index, base - index * 22)
            colour = self.state.ring_colour(index)
            start = self.state.ring_angle(index)
            # A gap in each ring is what makes it read as machinery rather
            # than as a circle -- the eye needs an edge to track the spin.
            for arc_start, extent in ((start, 140), (start + 180, 140)):
                canvas.create_arc(
                    cx - radius, cy - radius, cx + radius, cy + radius,
                    start=arc_start, extent=extent, style="arc",
                    outline=colour, width=2 if index else 3,
                )

        # The scan sweep.
        sweep = math.radians(self.state.sweep_angle())
        reach = base + 34
        canvas.create_line(
            cx, cy, cx + math.cos(sweep) * reach, cy - math.sin(sweep) * reach,
            fill=blend(CYAN, 0.25 + 0.35 * self.state.energy), width=1,
        )

        # The core.
        core = self.state.core_radius(26)
        canvas.create_oval(
            cx - core, cy - core, cx + core, cy + core,
            fill=self.state.core_colour(), outline="",
        )
        halo = core * 1.9
        canvas.create_oval(
            cx - halo, cy - halo, cx + halo, cy + halo,
            outline=blend(CYAN, 0.18 + 0.3 * self.state.energy), width=1,
        )

        canvas.create_text(
            cx, cy + base + 70, text=self._status_text.upper(),
            fill=blend(CYAN, 0.55), font=("DejaVu Sans Mono", 10), anchor="center",
        )
        canvas.create_text(
            cx, 34, text="J . A . R . V . I . S .",
            fill=blend(PALE, 0.35), font=("DejaVu Sans Mono", 13), anchor="center",
        )

    # ------------------------------------------------------------- events
    def _handle_submit(self, _event=None) -> None:
        if self._entry is None:
            return
        text = self._entry.get().strip()
        if not text:
            return
        self._entry.delete(0, "end")
        self._append(text, tag="caleb", prefix="C: ")
        if self.on_submit is not None:
            # On a worker thread: the agent loop takes seconds, and running
            # it here would freeze the animation and the window with it.
            threading.Thread(
                target=self.on_submit, args=(text,), daemon=True
            ).start()

    def _handle_close(self) -> None:
        self._closed.set()
        if self.on_close is not None:
            try:
                self.on_close()
            except Exception:
                log.debug("close hook raised", exc_info=True)
        if self._root is not None:
            self._root.destroy()
            self._root = None
