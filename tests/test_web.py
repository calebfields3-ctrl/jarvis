"""The browser face.

It has to match the HUD method for method, because `jarvis.app` drives
either without knowing which it holds. And it must never answer on anything
but loopback -- Jarvis has a shell, the filesystem and an API key behind it.
"""

from __future__ import annotations

import json
import threading
import time
import types
import urllib.error
import urllib.request

import pytest

from jarvis.web.server import HOST, MAX_LINES, WebFace, find_free_port


@pytest.fixture
def face():
    served = WebFace(open_browser=False)
    thread = threading.Thread(target=served.run, daemon=True)
    thread.start()
    for _ in range(100):
        try:
            urllib.request.urlopen(served.url, timeout=1).read()
            break
        except Exception:
            time.sleep(0.02)
    yield served
    served.close()


def get(face, path="api/state"):
    return json.load(urllib.request.urlopen(face.url + path, timeout=3))


def post(face, path, payload):
    request = urllib.request.Request(
        face.url + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    return urllib.request.urlopen(request, timeout=3).read()


# --------------------------------------------------------------- the page


def test_the_page_is_served(face):
    page = urllib.request.urlopen(face.url, timeout=3).read().decode()
    assert "<!doctype html>" in page.lower()
    assert 'id="orb"' in page


def test_the_orb_has_a_setting_for_every_state():
    """A state with no tuning falls back to idle and the face stops meaning anything."""
    from jarvis.gui.hud import Mode
    from jarvis.web.server import PAGE

    page = PAGE.read_text()
    tuning = page[page.index("const TUNING"):page.index("let mode =")]
    for mode in Mode:
        assert f"{mode.value}:" in tuning, f"the orb has no look for '{mode.value}'"


def test_listening_is_visibly_livelier_than_idle():
    """The whole point is answering "is it hearing me" without words."""
    import re

    from jarvis.web.server import PAGE

    page = PAGE.read_text()
    block = page[page.index("const TUNING"):page.index("let mode =")]

    def value(state, key):
        line = re.search(rf"{state}:\s*\{{([^}}]*)\}}", block).group(1)
        return float(re.search(rf"{key}:\s*([\d.]+)", line).group(1))

    for key in ("wobble", "churn", "glow"):
        assert value("listening", key) > value("idle", key), f"{key} does not rise"


def test_only_thinking_spawns_the_mind_particles():
    """Showing him "working" when he is idle would be decoration pretending to be state."""
    import re

    from jarvis.web.server import PAGE

    page = PAGE.read_text()
    block = page[page.index("const TUNING"):page.index("let mode =")]
    spawns = dict(re.findall(r"(\w+):\s*\{[^}]*spawn:\s*([\d.]+)", block))
    assert float(spawns["thinking"]) > 0
    for state in ("idle", "listening", "speaking", "asking"):
        assert float(spawns[state]) == 0, f"{state} spawns particles it has not earned"


def test_the_page_needs_nothing_from_the_internet():
    """A face that needs a CDN is a face that breaks on a bad connection."""
    from jarvis.web.server import PAGE

    page = PAGE.read_text()
    for offender in ("http://", "https://", "//cdn", "src=\"//"):
        assert offender not in page, f"the page reaches out to {offender}"


def test_it_only_ever_listens_on_loopback():
    """Jarvis holds a shell and an API key. The wifi must not reach him."""
    assert HOST == "127.0.0.1"


def test_a_busy_port_is_stepped_over():
    """Two Jarvises, or anything else on 7842, must not stop him starting."""
    import socket

    with socket.socket() as taken:
        # Port 0 lets the OS pick one that is definitely free, so this cannot
        # race whatever else the test suite has running.
        taken.bind((HOST, 0))
        taken.listen(1)
        busy = taken.getsockname()[1]

        chosen = find_free_port(busy)
        assert chosen != busy
        assert chosen > busy


# -------------------------------------------------------------- the state


def test_what_he_says_reaches_the_page(face):
    face.say("Good morning, sir.")
    lines = get(face)["lines"]
    assert lines[-1]["text"] == "Good morning, sir."
    assert lines[-1]["who"] == "jarvis"


def test_tool_calls_are_shown_live(face):
    face.tool_call(types.SimpleNamespace(name="run_command", summary="ls", ok=True))
    line = get(face)["lines"][-1]
    assert line["who"] == "tool" and "run_command" in line["text"]


def test_a_refused_tool_call_is_marked_as_such(face):
    face.tool_call(types.SimpleNamespace(name="read_file", summary="/etc/shadow", ok=False))
    assert get(face)["lines"][-1]["ok"] is False


def test_the_mode_drives_the_animation(face):
    face.set_mode("listening")
    assert get(face)["mode"] == "listening"


def test_the_version_changes_so_the_page_knows_to_redraw(face):
    before = get(face)["version"]
    face.say("something")
    assert get(face)["version"] != before


def test_the_transcript_is_bounded(face):
    for i in range(MAX_LINES + 60):
        face.say(f"line {i}")
    assert len(get(face)["lines"]) <= MAX_LINES


def test_typing_reaches_the_app(face):
    heard = []
    face.on_submit = heard.append
    post(face, "api/say", {"text": "how's my portfolio"})
    for _ in range(50):
        if heard:
            break
        time.sleep(0.02)
    assert heard == ["how's my portfolio"]


def test_an_empty_message_is_ignored(face):
    heard = []
    face.on_submit = heard.append
    post(face, "api/say", {"text": "   "})
    time.sleep(0.1)
    assert heard == []


def test_a_slow_turn_does_not_block_the_page(face):
    """The answer arrives by polling, so the request must return at once."""
    face.on_submit = lambda text: time.sleep(3)
    began = time.time()
    post(face, "api/say", {"text": "something slow"})
    assert time.time() - began < 1.0


# ------------------------------------------------------------- approvals


def test_an_approval_round_trips(face):
    decision = types.SimpleNamespace(
        reason="rm can delete things", risk=types.SimpleNamespace(name="DANGEROUS")
    )
    result = []
    threading.Thread(
        target=lambda: result.append(face.ask("run: rm notes.txt", decision)), daemon=True
    ).start()

    for _ in range(100):
        pending = get(face)["pending"]
        if pending:
            break
        time.sleep(0.02)
    assert pending["action"] == "run: rm notes.txt"
    assert pending["reason"] == "rm can delete things"

    post(face, "api/approve", {"id": pending["id"], "approved": True})
    for _ in range(100):
        if result:
            break
        time.sleep(0.02)
    assert result == [True]


def test_denying_is_a_no(face):
    decision = types.SimpleNamespace(reason="deletes things", risk=None)
    result = []
    threading.Thread(
        target=lambda: result.append(face.ask("rm x", decision)), daemon=True
    ).start()

    for _ in range(100):
        pending = get(face)["pending"]
        if pending:
            break
        time.sleep(0.02)
    post(face, "api/approve", {"id": pending["id"], "approved": False})
    for _ in range(100):
        if result:
            break
        time.sleep(0.02)
    assert result == [False]


def test_the_prompt_clears_once_answered(face):
    decision = types.SimpleNamespace(reason="deletes things", risk=None)
    threading.Thread(target=lambda: face.ask("rm x", decision), daemon=True).start()
    for _ in range(100):
        pending = get(face)["pending"]
        if pending:
            break
        time.sleep(0.02)
    post(face, "api/approve", {"id": pending["id"], "approved": False})
    for _ in range(100):
        if get(face)["pending"] is None:
            break
        time.sleep(0.02)
    assert get(face)["pending"] is None


def test_asking_a_closed_face_is_a_refusal():
    """A prompt nobody can see is not consent."""
    dead = WebFace(open_browser=False)
    dead._closed.set()
    assert dead.ask("rm x", types.SimpleNamespace(reason="")) is False


def test_an_approval_makes_him_visible_again(face):
    """He must not ask for permission from behind another tab, silently."""
    face.dismiss()
    decision = types.SimpleNamespace(reason="deletes things", risk=None)
    threading.Thread(target=lambda: face.ask("rm x", decision), daemon=True).start()
    for _ in range(100):
        if get(face)["visible"]:
            break
        time.sleep(0.02)
    assert get(face)["visible"] is True


# ------------------------------------------------------------- lifecycle


def test_closing_is_visible_to_other_threads(face):
    assert not face.closed
    face.close()
    assert face.closed


def test_the_close_hook_runs():
    fired = []
    served = WebFace(open_browser=False, on_close=lambda: fired.append(True))
    served.close()
    assert fired == [True]


def test_a_broken_close_hook_still_closes():
    served = WebFace(open_browser=False, on_close=lambda: 1 / 0)
    served.close()
    assert served.closed


def test_closing_twice_is_harmless():
    served = WebFace(open_browser=False)
    served.close()
    served.close()


def test_bad_json_does_not_take_the_server_down(face):
    request = urllib.request.Request(
        face.url + "api/say", data=b"{not json",
        headers={"Content-Type": "application/json"},
    )
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(request, timeout=3)
    assert get(face)["version"] >= 0, "the server died on malformed input"


def test_an_unknown_path_is_a_clean_404(face):
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(face.url + "nope", timeout=3)
    assert caught.value.code == 404


# ------------------------------------------------- it stands in for the HUD


def test_it_matches_the_hud_surface():
    """`jarvis.app` drives either one without knowing which it holds."""
    from jarvis.gui.hud import HUD

    for name in ("say", "status", "set_mode", "tool_call", "summon",
                 "dismiss", "close", "ask", "run", "closed", "visible"):
        assert hasattr(WebFace, name), f"WebFace is missing {name}"
        assert hasattr(HUD, name), f"HUD is missing {name}"
