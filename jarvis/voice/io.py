"""Voice: waking on "hey Jarvis", listening, and speaking back.

Every piece is optional and degrades in a defined order, so the same code path
runs on a machine with a microphone and on a headless box:

wake word   Picovoice Porcupine (it ships a built-in "jarvis" keyword) ->
            openWakeWord -> continuous speech recognition matching the phrase ->
            typing the phrase at a prompt.
listening   SpeechRecognition + Google/Sphinx -> typed input.
speaking    pyttsx3 (offline) -> macOS `say` / Linux `espeak` -> stdout.

Nothing here raises when a backend is missing; ``TextChannel`` is always a
working answer.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
from typing import Callable, Protocol

log = logging.getLogger(__name__)


class Speaker(Protocol):
    def say(self, text: str) -> None: ...


# ------------------------------------------------------------------ speaking
class PrintSpeaker:
    name = "text"

    def say(self, text: str) -> None:
        print(text)


class Pyttsx3Speaker:
    name = "pyttsx3"

    def __init__(self, rate: int = 185) -> None:
        import pyttsx3  # type: ignore

        self.engine = pyttsx3.init()
        self.engine.setProperty("rate", rate)

    def say(self, text: str) -> None:
        print(text)
        try:
            self.engine.say(text)
            self.engine.runAndWait()
        except Exception as exc:
            log.debug("pyttsx3 failed: %s", exc)


class CommandSpeaker:
    """macOS ``say`` or Linux ``espeak``."""

    def __init__(self, binary: str) -> None:
        self.binary = binary
        self.name = binary

    def say(self, text: str) -> None:
        print(text)
        try:
            subprocess.run([self.binary, text], check=False, capture_output=True, timeout=90)
        except Exception as exc:
            log.debug("%s failed: %s", self.binary, exc)


def build_speaker(enabled: bool = True) -> Speaker:
    if not enabled:
        return PrintSpeaker()
    try:
        return Pyttsx3Speaker()
    except Exception:
        pass
    for binary in ("say", "espeak-ng", "espeak"):
        if shutil.which(binary):
            return CommandSpeaker(binary)
    return PrintSpeaker()


# ------------------------------------------------------------------ listening
class TextListener:
    """Reads from stdin. The universal fallback."""

    name = "text"

    def __init__(self, prompt: str = "> ") -> None:
        self.prompt = prompt

    def listen(self, timeout: float | None = None) -> str | None:
        try:
            return input(self.prompt).strip() or None
        except (EOFError, KeyboardInterrupt):
            return None


class SpeechListener:
    """Microphone speech-to-text via the SpeechRecognition package."""

    name = "speech"

    def __init__(self, *, energy_threshold: int = 300, pause_threshold: float = 0.8) -> None:
        import speech_recognition as sr  # type: ignore

        self.sr = sr
        self.recognizer = sr.Recognizer()
        self.recognizer.energy_threshold = energy_threshold
        self.recognizer.pause_threshold = pause_threshold
        self.microphone = sr.Microphone()
        with self.microphone as source:
            self.recognizer.adjust_for_ambient_noise(source, duration=0.6)

    def listen(self, timeout: float | None = 8.0) -> str | None:
        try:
            with self.microphone as source:
                audio = self.recognizer.listen(source, timeout=timeout, phrase_time_limit=15)
        except Exception:
            return None
        for method, kwargs in (
            ("recognize_google", {}),
            ("recognize_sphinx", {}),
        ):
            recognise = getattr(self.recognizer, method, None)
            if recognise is None:
                continue
            try:
                return str(recognise(audio, **kwargs)).strip() or None
            except Exception:
                continue
        return None


def build_listener(voice_enabled: bool = True):
    if voice_enabled:
        try:
            return SpeechListener()
        except Exception as exc:
            log.info("microphone unavailable (%s) -- falling back to typed input", exc)
    return TextListener()


# ------------------------------------------------------------------ wake word
class WakeWordDetector(Protocol):
    def wait_for_wake(self) -> bool: ...


class PorcupineWake:
    """Picovoice Porcupine. Ships a built-in "jarvis" keyword, which is exactly
    the phrase we want, and runs fully on-device."""

    name = "porcupine"

    def __init__(self, access_key: str, keyword: str = "jarvis") -> None:
        import pvporcupine  # type: ignore
        import pyaudio  # type: ignore

        self.porcupine = pvporcupine.create(access_key=access_key, keywords=[keyword])
        self.pa = pyaudio.PyAudio()
        self.stream = self.pa.open(
            rate=self.porcupine.sample_rate,
            channels=1,
            format=pyaudio.paInt16,
            input=True,
            frames_per_buffer=self.porcupine.frame_length,
        )

    def wait_for_wake(self) -> bool:
        import struct

        while True:
            try:
                pcm = self.stream.read(self.porcupine.frame_length, exception_on_overflow=False)
                frame = struct.unpack_from("h" * self.porcupine.frame_length, pcm)
                if self.porcupine.process(frame) >= 0:
                    return True
            except KeyboardInterrupt:
                return False
            except Exception as exc:
                log.debug("porcupine read failed: %s", exc)
                return False

    def close(self) -> None:
        for closer in (
            lambda: self.stream.close(),
            lambda: self.pa.terminate(),
            lambda: self.porcupine.delete(),
        ):
            try:
                closer()
            except Exception:
                pass


class PhraseWake:
    """Listens continuously and fires when the wake phrase is heard.

    Heavier than a real keyword spotter, but it needs no access key and works
    with whatever STT backend is available.
    """

    name = "phrase"

    def __init__(self, listener, wake_phrase: str = "hey jarvis") -> None:
        self.listener = listener
        self.wake_phrase = wake_phrase.lower()
        self.tokens = [t for t in self.wake_phrase.split() if t]

    def heard_wake(self, text: str | None) -> bool:
        if not text:
            return False
        lowered = text.lower()
        if self.wake_phrase in lowered:
            return True
        # Tolerate STT dropping the "hey" or mangling spacing.
        return self.tokens[-1] in lowered.split()

    def wait_for_wake(self) -> bool:
        while True:
            heard = self.listener.listen(timeout=None)
            if heard is None:
                return False
            if self.heard_wake(heard):
                return True


def build_wake_detector(config, listener) -> WakeWordDetector:
    if config.voice_enabled and config.porcupine_key:
        try:
            return PorcupineWake(config.porcupine_key)
        except Exception as exc:
            log.info("Porcupine unavailable (%s) -- using phrase matching", exc)
    return PhraseWake(listener, config.wake_phrase)


# --------------------------------------------------------------------- channel
class VoiceChannel:
    """Bundles the three backends and reports which ones are actually live."""

    def __init__(self, config) -> None:
        self.config = config
        self.speaker = build_speaker(config.voice_enabled)
        self.listener = build_listener(config.voice_enabled)
        self.wake = build_wake_detector(config, self.listener)

    @property
    def mode(self) -> str:
        return f"{getattr(self.wake, 'name', '?')}/{self.listener.name}/{self.speaker.name}"

    @property
    def is_voice(self) -> bool:
        return self.listener.name != "text"

    def say(self, text: str) -> None:
        self.speaker.say(text)

    def listen(self, timeout: float | None = 12.0) -> str | None:
        return self.listener.listen(timeout)

    def wait_for_wake(self) -> bool:
        if isinstance(self.listener, TextListener):
            # Typed mode: the user types the wake phrase to start a session.
            while True:
                heard = self.listener.listen()
                if heard is None:
                    return False
                if self.config.wake_phrase in heard.lower() or heard.lower() in {"wake", "hi"}:
                    return True
                print(f'(say "{self.config.wake_phrase}" to wake me)')
        return self.wake.wait_for_wake()
