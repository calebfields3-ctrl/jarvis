"""Voice: waking on "hey Jarvis", listening, and speaking back.

Every piece is optional and degrades in a defined order, so the same code path
runs on a machine with a microphone and on a headless box:

wake word   Picovoice Porcupine (it ships a built-in "jarvis" keyword) ->
            openWakeWord -> continuous speech recognition matching the phrase ->
            typing the phrase at a prompt.
listening   SpeechRecognition + Google/Sphinx -> typed input.
speaking    Piper (neural, offline, sounds human) -> pyttsx3 -> macOS `say` /
            Linux `espeak` -> stdout.

Nothing here raises when a backend is missing; ``TextChannel`` is always a
working answer.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tempfile
import wave
from pathlib import Path
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


class PiperSpeaker:
    """Neural text-to-speech, offline, and it does not sound like a robot.

    ``espeak`` is intelligible and free and sounds like 1985. Piper runs a
    small neural model locally -- no API, no key, no network once the voice
    is downloaded -- and the difference is the difference between a machine
    reading at you and someone talking to you.

    The cost is a voice model of about sixty megabytes and a synthesis pass
    that takes a moment. Both are paid once and worth it for the thing you
    hear every time you say his name.

    Constructing this proves it works end to end: the model loads and a word
    is synthesised. Failing here is the point -- ``build_speaker`` falls
    through to espeak, and a Jarvis that says nothing at all is far worse
    than one that sounds synthetic.
    """

    name = "piper"

    def __init__(self, model: Path | str | None = None, *, player: str | None = None) -> None:
        from piper import PiperVoice  # type: ignore

        path = Path(model) if model else find_piper_voice()
        if path is None:
            raise RuntimeError("no piper voice model installed")

        self.model_path = Path(path)
        self.voice = PiperVoice.load(self.model_path)
        self.player = player or find_audio_player()
        if self.player is None:
            raise RuntimeError("nothing on this machine can play audio")

        # Prove the whole path works now, while there is still a fallback.
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=True) as probe:
            self._synthesise("ready", Path(probe.name))

    def _synthesise(self, text: str, target: Path) -> None:
        with wave.open(str(target), "wb") as handle:
            self.voice.synthesize_wav(text, handle)

    def say(self, text: str) -> None:
        print(text)
        if not text.strip():
            return
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                target = Path(tmp.name)
            try:
                self._synthesise(text, target)
                subprocess.run(
                    [*self.player.split(), str(target)],
                    check=False, capture_output=True, timeout=180,
                )
            finally:
                target.unlink(missing_ok=True)
        except Exception as exc:
            log.debug("piper failed: %s", exc)


def find_audio_player() -> str | None:
    """Something that can play a wav file, in order of how likely it is to work."""
    for candidate in ("paplay", "aplay -q", "ffplay -nodisp -autoexit -loglevel quiet", "afplay"):
        if shutil.which(candidate.split()[0]):
            return candidate
    return None


def voices_dir() -> Path:
    """Where downloaded voice models live."""
    home = Path(os.environ.get("JARVIS_HOME", Path.home() / ".jarvis")).expanduser()
    return home / "voices"


def find_piper_voice() -> Path | None:
    """The voice model to use, if one has been installed.

    An explicit ``JARVIS_VOICE_MODEL`` wins. Otherwise the most recently
    downloaded model in the voices directory, so installing a new one
    switches to it without any further configuration.
    """
    override = os.environ.get("JARVIS_VOICE_MODEL")
    if override:
        path = Path(override).expanduser()
        return path if path.exists() else None

    directory = voices_dir()
    if not directory.is_dir():
        return None
    models = sorted(
        directory.glob("*.onnx"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    return models[0] if models else None


# A British male voice, because that is what the character sounds like.
DEFAULT_PIPER_VOICE = "en_GB-alan-medium"


def install_piper_voice(voice: str = DEFAULT_PIPER_VOICE) -> Path:
    """Download a voice model. Returns where it landed.

    Raises with a readable message rather than a stack trace -- the likely
    caller is someone at a terminal for the first time.
    """
    try:
        from piper.download_voices import download_voice  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "piper isn't installed. Run:  pip install piper-tts"
        ) from exc

    target = voices_dir()
    target.mkdir(parents=True, exist_ok=True)
    try:
        download_voice(voice, target)
    except Exception as exc:
        raise RuntimeError(
            f"Couldn't download the voice '{voice}': {exc}\n"
            "Check your internet connection, or pick another from "
            "https://rhasspy.github.io/piper-samples/"
        ) from exc

    model = target / f"{voice}.onnx"
    if not model.exists():
        found = sorted(target.glob("*.onnx"))
        if not found:
            raise RuntimeError(f"The download finished but left no model in {target}")
        model = found[-1]
    return model


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
    # Piper first: it is the only one that sounds like a person, and it has
    # already proved it works by the time its constructor returns.
    try:
        return PiperSpeaker()
    except Exception as exc:
        log.debug("piper unavailable (%s) -- falling back", exc)
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
