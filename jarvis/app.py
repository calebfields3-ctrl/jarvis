"""Everything wired together: say "hey Jarvis" and he is there.

This is the piece that turns a pile of modules into the thing Caleb asked
for. It owns the assembly and the degradation, and the degradation is most
of the code:

    HUD + Claude + microphone      what it is meant to be
    HUD + Claude + typing          no microphone, or no mic packages
    HUD + router + typing          no API key, or no network
    terminal + router + typing     no display, no python3-tk

Every step down is announced once, in a sentence, and then he gets on with
it. Nothing here is allowed to be a hard failure -- a Chromebook with no
API key and no tkinter should still greet him by name and tell him what his
portfolio did, because that is what he had before and taking it away would
be a regression.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from jarvis.agent.brain import build_mind
from jarvis.agent.tools import ToolBox
from jarvis.config import Config, get_config
from jarvis.gui.hud import HUD, Mode, hud_problem
from jarvis.safety.permissions import PermissionEngine, PermissionPolicy
from jarvis.voice.daemon import WakeDaemon

log = logging.getLogger(__name__)

# How long the window stays up after he's finished answering. Long enough to
# read what he said and ask a follow-up without saying his name again.
IDLE_DISMISS_SECONDS = 45.0

DISMISSALS = {
    "goodbye", "bye", "that's all", "thats all", "thanks", "thank you",
    "nothing", "never mind", "nevermind", "dismissed", "sleep", "go away",
    "stop", "exit", "quit",
}


# Roughly a minute of speech. Past this he is talking at Caleb rather than
# to him, and the screen has the whole thing anyway.
SPOKEN_LIMIT = 600


def speakable(text: str) -> str:
    """Turn something written for the screen into something worth hearing.

    A morning briefing is a page of text. Read out verbatim it includes
    command examples -- "jarvis deposit 10000" pronounced as words -- and
    bullet punctuation, and it runs for minutes. What comes out of the
    speaker should be the substance of it and then stop; the screen keeps
    the rest.
    """
    keep: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        # Command examples are for reading and typing, never for hearing.
        if stripped.startswith(("jarvis ", "$ ", "sudo ", "pip ", "export ")):
            continue
        # Bullets and dashes are punctuation the ear can't use.
        if stripped.startswith(("- ", "* ", "• ")):
            stripped = stripped[2:].strip()
        keep.append(stripped)

    spoken = " ".join(keep)
    if len(spoken) <= SPOKEN_LIMIT:
        return spoken

    # Cut at a sentence end rather than mid-word.
    cut = spoken[:SPOKEN_LIMIT]
    for stop in (". ", "? ", "! "):
        index = cut.rfind(stop)
        if index > SPOKEN_LIMIT // 2:
            cut = cut[: index + 1]
            break
    return f"{cut.rstrip()} The rest is on your screen."


class JarvisApp:
    """Assembles Jarvis and runs him until Caleb closes the window."""

    def __init__(
        self,
        config: Config | None = None,
        *,
        jarvis: Any = None,
        use_hud: bool = True,
        use_voice: bool = True,
    ) -> None:
        self.config = config or get_config()
        self._jarvis = jarvis
        self.use_hud = use_hud
        self.use_voice = use_voice

        self.engine = PermissionEngine(
            PermissionPolicy.default(),
            audit_log=self.config.home / "audit.jsonl",
        )
        self.hud: HUD | None = None
        self.mind = None
        self.daemon: WakeDaemon | None = None
        self.voice = None
        self._notes: list[str] = []

    # ------------------------------------------------------------ assembly
    @property
    def jarvis(self):
        if self._jarvis is None:
            from jarvis.brain import Jarvis

            self._jarvis = Jarvis()
        return self._jarvis

    def _build(self) -> None:
        problem = hud_problem() if self.use_hud else "not asked for"
        if problem is None:
            self.hud = HUD(
                on_submit=self._handle,
                on_close=self._shutdown,
                # Only hide it if a wake word is actually live to bring it
                # back. Deciding this from `use_voice` rather than from a
                # working microphone means a failed mic leaves the window
                # hidden with nothing able to summon it -- a program that
                # starts and then appears to do nothing at all.
                start_hidden=self.voice is not None,
            )
        elif self.use_hud:
            self._note(f"Running in the terminal -- no window available.\n{problem}")

        toolbox = ToolBox(
            self.engine,
            jarvis=self.jarvis,
            approver=self.hud.ask if self.hud else self._ask_in_terminal,
            on_call=self._show_tool_call,
        )
        self.mind = build_mind(toolbox, jarvis=self.jarvis)
        if self.mind is None:
            self._note(
                "No ANTHROPIC_API_KEY is set, so I'm on my built-in routing "
                "rather than a full mind. Set the key and restart for the real thing."
            )

    def _probe_voice(self) -> None:
        """Find out whether the microphone actually works.

        Runs before the window is built, because whether there is a live wake
        word decides whether the window may start hidden.
        """
        if not self.use_voice:
            return
        try:
            from jarvis.voice.io import VoiceChannel

            channel = VoiceChannel(self.config)
        except Exception as exc:
            self._note(f"No microphone, so type to me instead. ({exc})")
            return

        if not channel.is_voice:
            # A text listener as the wake detector would sit on stdin
            # competing with the window for input, which is worse than
            # simply not having a wake word.
            self._note(
                "No microphone, so \"hey Jarvis\" won't work -- type to me instead. "
                "To fix it: sudo apt install -y portaudio19-dev espeak-ng "
                "&& pip install SpeechRecognition pyaudio pyttsx3"
            )
            return

        self.voice = channel

    def _start_voice(self) -> None:
        """Begin listening. Only once the window exists to be summoned."""
        if self.voice is None:
            return
        self.daemon = WakeDaemon(
            self.voice.wake, on_wake=self._woken, on_error=self._note
        )
        self.daemon.start()

    # --------------------------------------------------------------- events
    def _woken(self) -> None:
        """He said the words. Window up, greeting out, then listen."""
        self._cancel_dismissal()

        # The window and the greeting come first, before anything is looked
        # up. Say "hey Jarvis" and wait three seconds for a database query and
        # it reads as the wake word not working, so you say it again.
        if self.hud is not None:
            self.hud.summon()
            self.hud.set_mode(Mode.LISTENING)
            self.hud.status("listening")

        greeting = self.jarvis.voice.summoned(self.jarvis.spoken_name)
        self._speak(greeting)

        # The briefing is the slow part, so it goes out after the greeting
        # rather than instead of it.
        if self.jarvis.first_time_today():
            self._speak(self.jarvis.summoned())

        heard = self.voice.listen(timeout=12.0) if self.voice else None
        if not heard:
            self._idle()
            return
        if self.hud is not None:
            self.hud.say(heard, who="caleb")
        self._handle(heard)

    def _speak(self, text: str) -> None:
        """Put a line in front of Caleb, in whatever channels exist.

        The screen gets everything; the speaker gets a version fit to listen
        to. They are not the same thing, and reading the screen out verbatim
        is how a useful briefing becomes something you talk over.
        """
        if self.hud is not None:
            self.hud.say(text)
        else:
            print(f"\n{text}\n")
        if self.voice is not None:
            try:
                self.voice.say(speakable(text))
            except Exception:
                log.debug("speaking failed", exc_info=True)

    def _handle(self, text: str) -> None:
        """One exchange, from whichever direction it arrived."""
        self._cancel_dismissal()

        if text.strip().lower() in DISMISSALS:
            self._speak(self.jarvis.voice.dismissed())
            self._idle(delay=0.0)
            return

        if self.hud is not None:
            self.hud.set_mode(Mode.THINKING)
            self.hud.status("working")
        try:
            answer = self._answer(text)
        except Exception as exc:
            log.exception("the turn failed")
            answer = f"That went wrong on my end, sir: {exc}"

        if self.hud is not None:
            self.hud.set_mode(Mode.SPEAKING)
            self.hud.status("speaking")
        self._speak(answer)
        self._idle()

    # ----------------------------------------------------------- dismissal
    def _idle(self, delay: float | None = None) -> None:
        """Back to standing by, and out of the way once he's not needed.

        Only when there's a wake word. Without one, hiding the window would
        leave Caleb with no way to get it back.
        """
        if self.hud is not None:
            self.hud.set_mode(Mode.IDLE)
            self.hud.status("standing by")
        if self.hud is None or self.voice is None:
            return

        wait = IDLE_DISMISS_SECONDS if delay is None else delay
        timer = threading.Timer(wait, self._dismiss)
        timer.daemon = True
        self._dismiss_timer = timer
        timer.start()

    def _dismiss(self) -> None:
        if self.hud is not None:
            self.hud.dismiss()

    def _cancel_dismissal(self) -> None:
        timer = getattr(self, "_dismiss_timer", None)
        if timer is not None:
            timer.cancel()
            self._dismiss_timer = None

    def _answer(self, text: str) -> str:
        if self.mind is not None:
            reply = self.mind.say(text)
            return reply.text or "..."
        return self.jarvis.ask(text)

    def _show_tool_call(self, call) -> None:
        """Show what he's touching, as he touches it.

        In the terminal this is the only visibility Caleb has into a thing
        with a shell. Watching it work is how you notice it doing something
        you did not mean, and noticing has to be possible in the moment
        rather than afterwards in the audit log.
        """
        if self.hud is not None:
            self.hud.tool_call(call)
            return
        mark = "·" if call.ok else "×"
        print(f"    {mark} {call.name}  {call.summary}")

    def _ask_in_terminal(self, action: str, decision) -> bool:
        """Approval with no window. Anything but an explicit yes is a no."""
        print(f"\n  Jarvis wants to: {action}")
        print(f"  {decision.reason}")
        try:
            return input("  Allow it? [y/N] ").strip().lower() in {"y", "yes"}
        except (EOFError, KeyboardInterrupt):
            return False

    def _note(self, message: str) -> None:
        self._notes.append(message)
        log.info("%s", message)

    # ------------------------------------------------------------------ run
    def run(self) -> None:
        self._probe_voice()
        self._build()
        self._start_voice()

        if self.hud is None:
            self._run_terminal()
            return

        # The window has to own the main thread -- tkinter will not run
        # anywhere else -- so everything below is queued and appears as soon
        # as the loop starts.
        for note in self._notes:
            self.hud.say(note)

        if self.voice is not None:
            # Hidden and waiting. The greeting belongs to the first summon,
            # not to startup -- nobody is looking at the screen yet, and
            # saying it now means he says it twice.
            self.hud.status("say 'hey jarvis'")
            print(f"  Listening for \"{self.config.wake_phrase}\". Close the window to stop.")
        else:
            self.hud.say(self.jarvis.wake(channel="text"))

        try:
            self.hud.run()
        finally:
            self._shutdown()

    def _run_terminal(self) -> None:
        """The terminal is the home. Voice on top of it, not instead of it.

        Typing keeps working the whole time the microphone is live -- the
        wake word answers on a background thread, so both routes are open and
        neither blocks the other.
        """
        for note in self._notes:
            print(f"  {note}\n")

        if self.voice is not None:
            # The greeting belongs to the first "hey Jarvis", not to startup.
            # Printing it here means he says it twice.
            print(
                f'\n  Listening. Say "{self.config.wake_phrase}" -- '
                "or just type to me here.\n  Ctrl-C to stop.\n"
            )
        else:
            print(f"\n{self.jarvis.wake(channel='text')}\n")

        try:
            while True:
                text = input("> ").strip()
                if not text:
                    continue
                if text.lower() in {"exit", "quit"}:
                    break
                self._handle(text)
        except (EOFError, KeyboardInterrupt):
            print()
        finally:
            self._shutdown()

    def _shutdown(self) -> None:
        self._cancel_dismissal()
        if self.daemon is not None:
            self.daemon.stop()
            self.daemon = None
        try:
            self.jarvis.sleep()
        except Exception:
            log.debug("closing the session failed", exc_info=True)


def run(**kwargs) -> None:
    JarvisApp(**kwargs).run()
