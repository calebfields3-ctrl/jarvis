"""One command that says what works, what doesn't, and how to fix it.

Written because "it did not work" and "command not found" are the two things
you actually get told when something is wrong, and neither narrows anything
down. Every check here answers a question someone has actually been stuck
on, and every failure carries the exact line to paste.

Two rules. A check never crashes -- an exception is a failed check with the
exception as its reason, because a diagnostic that dies has told you nothing.
And a failure always names the fix; "microphone: no" is not a diagnosis.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

OK = "ok"
WARN = "warn"      # works, but degraded -- he'll be less capable
FAIL = "fail"      # this is why the thing you tried didn't work

MARKS = {OK: "  ok  ", WARN: " warn ", FAIL: " FAIL "}


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""
    fix: str = ""


def _run(name: str, probe: Callable[[], Check]) -> Check:
    try:
        return probe()
    except Exception as exc:
        return Check(name, FAIL, f"the check itself failed: {exc}")


# ------------------------------------------------------------------ checks
def check_python() -> Check:
    version = ".".join(str(n) for n in sys.version_info[:3])
    if sys.version_info < (3, 10):
        return Check("python", FAIL, version, "sudo apt install -y python3")
    return Check("python", OK, version)


def check_mind() -> Check:
    """The one that decides whether he can answer anything, or only finance."""
    gemini = (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip()
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key and gemini:
        return Check("his mind (API key)", OK, f"Gemini, free tier ({gemini[:6]}...{gemini[-4:]})")
    if not key:
        return Check(
            "his mind (API key)", FAIL,
            "not set -- he can only answer finance questions from his built-in list",
            "jarvis key      (takes a free Google key or a paid Anthropic one)",
        )
    if not key.startswith("sk-"):
        return Check(
            "his mind (API key)", FAIL,
            "that doesn't look like an Anthropic key (they start with 'sk-')",
            "Get one at console.anthropic.com -> API keys, then run ./setup.sh",
        )
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return Check(
            "his mind (API key)", FAIL, "key is set but the anthropic package is missing",
            "pip install anthropic",
        )
    return Check("his mind (API key)", OK, f"set ({key[:7]}...{key[-4:]})")


def check_gemini_reaches_google() -> Check:
    """Prove the free key actually answers, not merely that it is set."""
    import json
    import urllib.request

    key = (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip()
    if not key:
        return Check("can he think", FAIL, "no Gemini key set", "jarvis key")

    from jarvis.agent.gemini import API_ROOT

    try:
        with urllib.request.urlopen(f"{API_ROOT}/models?key={key}", timeout=25) as response:
            catalogue = json.loads(response.read().decode())
    except Exception as exc:
        text = str(exc)
        if "403" in text or "400" in text:
            return Check(
                "can he think", FAIL, "Google rejected that key",
                "Make a new one at aistudio.google.com -> Get API key",
            )
        return Check("can he think", FAIL, text[:120], "Check your internet connection")

    count = len(catalogue.get("models") or [])
    if not count:
        return Check(
            "can he think", FAIL, "the key works but no models are available",
            "Check the key has the Generative Language API enabled",
        )
    return Check("can he think", OK, f"Gemini answered -- {count} models available")


def check_mind_reaches_anthropic() -> Check:
    """A key that is set but rejected looks identical to no key at all."""
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        if (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip():
            return check_gemini_reaches_google()
        return Check("can he think", FAIL, "no key to try", "see above")
    try:
        import anthropic
    except ImportError:
        return Check("can he think", FAIL, "anthropic package missing", "pip install anthropic")

    try:
        client = anthropic.Anthropic()
        client.messages.create(
            model="claude-opus-5",
            max_tokens=4,
            messages=[{"role": "user", "content": "hi"}],
        )
    except Exception as exc:
        text = str(exc)
        if "authentication" in text.lower() or "401" in text:
            return Check(
                "can he think", FAIL, "the key was rejected",
                "Make a new key at console.anthropic.com, then run ./setup.sh",
            )
        if "credit" in text.lower() or "billing" in text.lower() or "402" in text:
            return Check(
                "can he think", FAIL, "the account is out of credit",
                "Add credit at console.anthropic.com -> Billing",
            )
        return Check("can he think", FAIL, text[:120], "Check your internet connection")
    return Check("can he think", OK, "reached Claude and got an answer back")


def check_microphone() -> Check:
    try:
        import speech_recognition as sr  # type: ignore
    except ImportError:
        return Check(
            "microphone", FAIL, "the speech packages aren't installed",
            "sudo apt install -y portaudio19-dev && pip install SpeechRecognition pyaudio",
        )

    from jarvis.voice.io import hushed

    try:
        with hushed():
            names = sr.Microphone.list_microphone_names()
    except Exception as exc:
        return Check("microphone", FAIL, str(exc)[:100], "Settings -> Linux -> Microphone")

    if not names:
        return Check(
            "microphone", FAIL, "no input devices found",
            "ChromeOS: Settings -> Linux -> turn Microphone on, then restart Linux",
        )
    try:
        with hushed():
            with sr.Microphone() as source:
                sr.Recognizer().adjust_for_ambient_noise(source, duration=0.3)
    except Exception as exc:
        return Check(
            "microphone", FAIL, f"found {len(names)} devices but couldn't open one: {str(exc)[:60]}",
            "ChromeOS: Settings -> Linux -> turn Microphone on, then restart Linux",
        )
    return Check("microphone", OK, f"{len(names)} input device(s), opened fine")


def check_speech_to_text() -> Check:
    """Hearing you needs the network unless an offline recogniser is installed."""
    try:
        import speech_recognition as sr  # type: ignore
    except ImportError:
        return Check("understanding speech", FAIL, "speech packages missing", "see microphone")

    recognizer = sr.Recognizer()
    if hasattr(recognizer, "recognize_google"):
        return Check("understanding speech", OK, "Google speech recognition (needs internet)")
    return Check(
        "understanding speech", WARN, "no recogniser available",
        "pip install SpeechRecognition",
    )


def check_voice() -> Check:
    from jarvis.voice.io import build_speaker, find_audio_player, find_piper_voice

    player = find_audio_player()
    if player is None:
        return Check(
            "his voice", FAIL, "nothing on this machine can play audio",
            "sudo apt install -y alsa-utils",
        )
    speaker = build_speaker(True)
    if speaker.name == "piper":
        return Check("his voice", OK, f"neural voice, played through {player.split()[0]}")
    if find_piper_voice() is None:
        return Check(
            "his voice", WARN, f"using {speaker.name} -- he'll sound robotic",
            "jarvis voice --install",
        )
    return Check("his voice", WARN, f"using {speaker.name} -- he'll sound robotic",
                 "jarvis voice --install")


def check_window() -> Check:
    from jarvis.gui.hud import hud_problem

    problem = hud_problem()
    if problem is None:
        return Check("the window", OK, "opens fine")
    return Check(
        "the window", WARN, "not available -- he'll run in this terminal",
        "sudo apt install -y python3-tk",
    )


def check_memory() -> Check:
    from jarvis.config import get_config

    config = get_config()
    try:
        config.home.mkdir(parents=True, exist_ok=True)
        probe = config.home / ".doctor"
        probe.write_text("ok")
        probe.unlink()
    except OSError as exc:
        return Check("memory", FAIL, f"can't write to {config.home}: {exc}", "")
    size = config.db_path.stat().st_size if config.db_path.exists() else 0
    return Check("memory", OK, f"{config.home} ({size / 1e6:.1f} MB)")


def check_market_data() -> Check:
    from jarvis.market.yahoo import yfinance_available

    if not yfinance_available():
        return Check(
            "market data", WARN, "yfinance missing -- prices will be simulated",
            "pip install yfinance",
        )
    return Check("market data", OK, "yfinance installed")


def check_command() -> Check:
    where = shutil.which("jarvis")
    if where is None:
        return Check(
            "the 'jarvis' command", WARN, "not on your PATH in this shell",
            "cd ~/jarvis && ./setup.sh",
        )
    return Check("the 'jarvis' command", OK, where)


CHECKS: tuple[tuple[str, Callable[[], Check]], ...] = (
    ("python", check_python),
    ("the 'jarvis' command", check_command),
    ("memory", check_memory),
    ("his mind (API key)", check_mind),
    ("can he think", check_mind_reaches_anthropic),
    ("microphone", check_microphone),
    ("understanding speech", check_speech_to_text),
    ("his voice", check_voice),
    ("the window", check_window),
    ("market data", check_market_data),
)


def run_all(*, skip_network: bool = False) -> list[Check]:
    checks = []
    for name, probe in CHECKS:
        if skip_network and name == "can he think":
            continue
        checks.append(_run(name, probe))
    return checks


def report(checks: list[Check]) -> str:
    lines = ["", "  Jarvis self-check", ""]
    width = max(len(c.name) for c in checks)
    for check in checks:
        lines.append(f"  [{MARKS[check.status]}] {check.name.ljust(width)}  {check.detail}")

    broken = [c for c in checks if c.status == FAIL and c.fix]
    degraded = [c for c in checks if c.status == WARN and c.fix]

    if broken:
        lines += ["", "  What's actually broken, and the fix:", ""]
        for check in broken:
            lines.append(f"    {check.name}:")
            lines.append(f"      {check.fix}")
    if degraded:
        lines += ["", "  Working, but he'd be better with:", ""]
        for check in degraded:
            lines.append(f"    {check.fix}")
    if not broken and not degraded:
        lines += ["", "  Everything is working. Run 'jarvis' and say \"hey Jarvis\".", ""]
    elif not broken:
        lines += ["", "  Nothing is broken -- he'll run. Run 'jarvis' and say \"hey Jarvis\".", ""]
    else:
        lines.append("")
    return "\n".join(lines)
