"""The voice he speaks with.

The property that matters most: Jarvis must never end up silent. A neural
voice that fails to load has to fall through to the robotic one, and that to
plain text. Sounding synthetic is a disappointment; saying nothing at all is
a broken program.
"""

from __future__ import annotations

import os
import sys
import types
import wave

import pytest

from jarvis.voice import io


@pytest.fixture
def voices(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.delenv("JARVIS_VOICE_MODEL", raising=False)
    directory = tmp_path / "voices"
    directory.mkdir()
    return directory


def fake_piper(monkeypatch, *, fails=False):
    """A stand-in for the piper package, which has no model to load in tests."""
    synthesised = []

    class FakeVoice:
        @staticmethod
        def load(path, *a, **k):
            if fails:
                raise RuntimeError("the model file is corrupt")
            return FakeVoice()

        def synthesize_wav(self, text, wav_file, *a, **k):
            synthesised.append(text)
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(22050)
            wav_file.writeframes(b"\x00\x00" * 100)

    monkeypatch.setitem(sys.modules, "piper", types.SimpleNamespace(PiperVoice=FakeVoice))
    return synthesised


# ------------------------------------------------------------ finding a voice


def test_no_model_installed_is_reported_not_guessed(voices):
    assert io.find_piper_voice() is None


def test_an_installed_model_is_found(voices):
    model = voices / "en_GB-alan-medium.onnx"
    model.write_bytes(b"x")
    assert io.find_piper_voice() == model


def test_the_newest_model_wins_so_installing_one_switches_to_it(voices):
    import os
    import time

    old = voices / "old.onnx"
    old.write_bytes(b"x")
    new = voices / "new.onnx"
    new.write_bytes(b"x")
    os.utime(old, (time.time() - 500, time.time() - 500))

    assert io.find_piper_voice() == new


def test_an_explicit_model_overrides_the_search(voices, monkeypatch, tmp_path):
    (voices / "installed.onnx").write_bytes(b"x")
    chosen = tmp_path / "chosen.onnx"
    chosen.write_bytes(b"x")
    monkeypatch.setenv("JARVIS_VOICE_MODEL", str(chosen))

    assert io.find_piper_voice() == chosen


def test_an_override_pointing_at_nothing_does_not_fall_back_silently(voices, monkeypatch):
    """Naming a model that isn't there is a mistake worth surfacing."""
    (voices / "installed.onnx").write_bytes(b"x")
    monkeypatch.setenv("JARVIS_VOICE_MODEL", "/no/such/voice.onnx")

    assert io.find_piper_voice() is None


def test_the_voices_directory_follows_jarvis_home(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "elsewhere"))
    assert io.voices_dir() == tmp_path / "elsewhere" / "voices"


# ------------------------------------------------------------- the speaker


def test_piper_speaks_through_the_audio_player(voices, monkeypatch):
    said = fake_piper(monkeypatch)
    (voices / "v.onnx").write_bytes(b"x")
    monkeypatch.setattr(io.shutil, "which", lambda n: "/usr/bin/aplay")

    played = []
    monkeypatch.setattr(io.subprocess, "run", lambda cmd, **k: played.append(cmd))

    speaker = io.PiperSpeaker()
    speaker.say("Hello, Caleb.")

    assert "Hello, Caleb." in said
    assert played, "nothing was handed to an audio player"
    assert played[-1][0] == io.find_audio_player().split()[0]
    assert played[-1][-1].endswith(".wav")


def test_only_a_player_that_exists_is_used(monkeypatch):
    """Handing a wav to a binary that isn't installed is silence with extra steps."""
    monkeypatch.setattr(io.shutil, "which", lambda n: "/usr/bin/aplay" if n == "aplay" else None)
    assert io.find_audio_player().split()[0] == "aplay"

    monkeypatch.setattr(io.shutil, "which", lambda n: None)
    assert io.find_audio_player() is None


def test_piper_refuses_to_exist_without_a_model(voices, monkeypatch):
    """Better to fail here, where there's still a fallback, than at speech time."""
    fake_piper(monkeypatch)
    with pytest.raises(RuntimeError, match="no piper voice"):
        io.PiperSpeaker()


def test_piper_refuses_to_exist_with_nothing_to_play_audio(voices, monkeypatch):
    fake_piper(monkeypatch)
    (voices / "v.onnx").write_bytes(b"x")
    monkeypatch.setattr(io.shutil, "which", lambda n: None)

    with pytest.raises(RuntimeError, match="play audio"):
        io.PiperSpeaker()


def test_a_corrupt_model_fails_at_construction_not_mid_sentence(voices, monkeypatch):
    fake_piper(monkeypatch, fails=True)
    (voices / "v.onnx").write_bytes(b"x")
    monkeypatch.setattr(io.shutil, "which", lambda n: "/usr/bin/aplay")

    with pytest.raises(Exception):
        io.PiperSpeaker()


def test_a_failure_while_speaking_does_not_crash_the_conversation(voices, monkeypatch):
    fake_piper(monkeypatch)
    (voices / "v.onnx").write_bytes(b"x")
    monkeypatch.setattr(io.shutil, "which", lambda n: "/usr/bin/aplay")
    speaker = io.PiperSpeaker()

    monkeypatch.setattr(speaker, "_synthesise", lambda *a: (_ for _ in ()).throw(OSError("nope")))
    speaker.say("this will fail")  # must not raise


def test_speaking_nothing_does_nothing(voices, monkeypatch):
    said = fake_piper(monkeypatch)
    (voices / "v.onnx").write_bytes(b"x")
    monkeypatch.setattr(io.shutil, "which", lambda n: "/usr/bin/aplay")
    speaker = io.PiperSpeaker()
    said.clear()

    speaker.say("   ")
    assert said == []


def test_the_temporary_wav_is_cleaned_up(voices, monkeypatch, tmp_path):
    fake_piper(monkeypatch)
    (voices / "v.onnx").write_bytes(b"x")
    monkeypatch.setattr(io.shutil, "which", lambda n: "/usr/bin/aplay")

    written = []
    speaker = io.PiperSpeaker()
    real = speaker._synthesise

    def track(text, target):
        written.append(target)
        real(text, target)

    monkeypatch.setattr(speaker, "_synthesise", track)
    monkeypatch.setattr(io.subprocess, "run", lambda *a, **k: None)
    speaker.say("hello")

    assert written and not written[-1].exists(), "a wav was left behind on every line spoken"


# ------------------------------------------------------------ the fallback


def test_a_missing_piper_falls_through_rather_than_going_silent(voices, monkeypatch):
    """Sounding robotic is a disappointment. Saying nothing is a broken program."""
    monkeypatch.setitem(sys.modules, "piper", None)
    monkeypatch.setattr(io.shutil, "which", lambda n: None)

    speaker = io.build_speaker(True)
    assert speaker.name == "text"
    speaker.say("still works")


def test_piper_is_preferred_when_it_works(voices, monkeypatch):
    fake_piper(monkeypatch)
    (voices / "v.onnx").write_bytes(b"x")
    monkeypatch.setattr(io.shutil, "which", lambda n: "/usr/bin/aplay")
    monkeypatch.setattr(io.subprocess, "run", lambda *a, **k: None)

    assert io.build_speaker(True).name == "piper"


def test_espeak_is_used_when_piper_has_no_model(voices, monkeypatch):
    fake_piper(monkeypatch)
    monkeypatch.setitem(sys.modules, "pyttsx3", None)
    monkeypatch.setattr(io.shutil, "which", lambda n: "/usr/bin/espeak" if "espeak" in n else None)

    assert io.build_speaker(True).name in {"espeak", "espeak-ng"}


def test_disabling_voice_still_prints(voices, capsys):
    io.build_speaker(False).say("on screen")
    assert "on screen" in capsys.readouterr().out


# --------------------------------------------------------------- installing


def test_installing_without_piper_says_how_to_get_it(monkeypatch):
    monkeypatch.setitem(sys.modules, "piper.download_voices", None)
    monkeypatch.setitem(sys.modules, "piper", None)

    with pytest.raises(RuntimeError, match="pip install piper-tts"):
        io.install_piper_voice()


def test_a_failed_download_explains_itself_without_a_stack_trace(voices, monkeypatch):
    def explode(voice, download_dir, force_redownload=False):
        raise OSError("connection refused")

    monkeypatch.setitem(
        sys.modules, "piper.download_voices",
        types.SimpleNamespace(download_voice=explode),
    )
    with pytest.raises(RuntimeError, match="Couldn't download"):
        io.install_piper_voice()


def test_a_download_that_leaves_no_model_is_not_reported_as_success(voices, monkeypatch):
    monkeypatch.setitem(
        sys.modules, "piper.download_voices",
        types.SimpleNamespace(download_voice=lambda *a, **k: None),
    )
    with pytest.raises(RuntimeError, match="no model"):
        io.install_piper_voice()


def test_a_successful_download_returns_where_it_landed(voices, monkeypatch):
    def fake_download(voice, download_dir, force_redownload=False):
        (download_dir / f"{voice}.onnx").write_bytes(b"model")

    monkeypatch.setitem(
        sys.modules, "piper.download_voices",
        types.SimpleNamespace(download_voice=fake_download),
    )
    model = io.install_piper_voice("en_GB-alan-medium")
    assert model.exists() and model.name == "en_GB-alan-medium.onnx"


def test_the_default_voice_is_a_british_male():
    """He is meant to sound like the character."""
    assert io.DEFAULT_PIPER_VOICE.startswith("en_GB")


# ------------------------------------------------------- the ALSA noise floor


def test_hushed_swallows_c_level_stderr(capfd):
    """ALSA writes to fd 2 from C, so redirect_stderr cannot touch it."""
    io.quiet_audio(True)
    with io.hushed():
        os.write(2, b"ALSA lib pcm.c:2222: Unknown PCM cards.pcm.rear\n")
    assert "Unknown PCM" not in capfd.readouterr().err


def test_stderr_still_works_afterwards(capfd):
    io.quiet_audio(True)
    with io.hushed():
        os.write(2, b"noise\n")
    os.write(2, b"a real error\n")
    assert "a real error" in capfd.readouterr().err


def test_stderr_is_restored_even_when_the_block_raises(capfd):
    io.quiet_audio(True)
    with pytest.raises(ValueError):
        with io.hushed():
            raise ValueError("boom")
    os.write(2, b"still here\n")
    assert "still here" in capfd.readouterr().err


def test_verbose_lets_the_noise_through(capfd):
    """When the microphone really is broken, these lines are what you want."""
    io.quiet_audio(False)
    try:
        with io.hushed():
            os.write(2, b"ALSA lib pcm.c:2222: Unknown PCM\n")
        assert "Unknown PCM" in capfd.readouterr().err
    finally:
        io.quiet_audio(True)


def test_nesting_does_not_leak_a_descriptor(capfd):
    io.quiet_audio(True)
    before = os.dup(2)
    os.close(before)
    for _ in range(50):
        with io.hushed():
            os.write(2, b"noise\n")
    after = os.dup(2)
    os.close(after)
    assert after - before < 10, "file descriptors are leaking on every listen"


def test_the_alsa_handler_install_never_raises():
    """It is a nicety; a machine without libasound must not crash on startup."""
    io._mute_alsa_handler()
