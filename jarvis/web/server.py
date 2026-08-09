"""A local web server that presents Jarvis, and takes typing back.

Deliberately built on ``http.server`` from the standard library rather than
Flask or FastAPI. Jarvis has to install on a Chromebook where pip sometimes
cannot reach the network at all, and a face that disappears when a
dependency fails is not much of a face.

The shape matches :class:`jarvis.gui.hud.HUD` method for method -- ``say``,
``set_mode``, ``status``, ``tool_call``, ``summon``, ``dismiss``, ``ask`` --
so :mod:`jarvis.app` drives either one without knowing which it has.

Nothing is pushed to the browser. The page asks for state a few times a
second and re-renders when a version number changes, which is less elegant
than a websocket and has the considerable advantage of never getting stuck
in a half-open state that needs a reload to clear.
"""

from __future__ import annotations

import json
import logging
import queue
import socket
import threading
import webbrowser
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

PAGE = Path(__file__).parent / "ui.html"

# Only ever bound to the loopback address. Jarvis holds a shell, the
# filesystem and an API key; a face that answers on the network would hand
# all three to anyone else on the wifi.
HOST = "127.0.0.1"
DEFAULT_PORT = 7842

# Enough scrollback to follow a conversation, bounded so a long session
# cannot grow the payload without limit.
MAX_LINES = 300


def find_free_port(preferred: int = DEFAULT_PORT, tries: int = 20) -> int:
    """A port nothing else is using, starting from the usual one."""
    for offset in range(tries):
        candidate = preferred + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind((HOST, candidate))
                return candidate
            except OSError:
                continue
    raise RuntimeError("no free port for the Jarvis window")


@dataclass
class Line:
    who: str          # 'jarvis' | 'caleb' | 'tool' | 'note'
    text: str
    ok: bool = True


@dataclass
class State:
    mode: str = "idle"
    status: str = "standing by"
    visible: bool = True
    lines: list[Line] = field(default_factory=list)
    pending: dict[str, Any] | None = None   # an approval waiting on Caleb
    version: int = 0


class WebFace:
    """Jarvis's face, served on localhost."""

    def __init__(
        self,
        *,
        title: str = "J.A.R.V.I.S.",
        on_submit: Callable[[str], None] | None = None,
        on_close: Callable[[], None] | None = None,
        port: int | None = None,
        open_browser: bool = True,
        start_hidden: bool = False,
    ) -> None:
        self.title = title
        self.on_submit = on_submit
        self.on_close = on_close
        self.port = port or find_free_port()
        self.open_browser = open_browser

        self.state = State(visible=not start_hidden)
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._server: ThreadingHTTPServer | None = None
        self._answers: dict[str, queue.Queue] = {}
        self._next_id = 0

    # ------------------------------------------------------------ the API
    @property
    def url(self) -> str:
        return f"http://{HOST}:{self.port}/"

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    @property
    def visible(self) -> bool:
        return self.state.visible

    def _touch(self) -> None:
        self.state.version += 1

    def say(self, text: str, *, who: str = "jarvis") -> None:
        with self._lock:
            self.state.lines.append(Line(who, text))
            del self.state.lines[:-MAX_LINES]
            self._touch()

    def status(self, text: str) -> None:
        with self._lock:
            self.state.status = text
            self._touch()

    def set_mode(self, mode) -> None:
        with self._lock:
            self.state.mode = getattr(mode, "value", str(mode))
            self._touch()

    def tool_call(self, call) -> None:
        with self._lock:
            self.state.lines.append(
                Line("tool", f"{call.name}  {call.summary}", ok=call.ok)
            )
            del self.state.lines[:-MAX_LINES]
            self._touch()

    def summon(self) -> None:
        with self._lock:
            self.state.visible = True
            self._touch()

    def dismiss(self) -> None:
        with self._lock:
            self.state.visible = False
            self._touch()

    def close(self) -> None:
        self._handle_close()

    def ask(self, action: str, decision) -> bool:
        """Put an approval in front of Caleb and block until he answers.

        Returns False if the page is gone. An approval nobody can see is not
        an approval, and defaulting to yes would undo the permission layer.
        """
        if self._closed.is_set():
            return False
        with self._lock:
            self._next_id += 1
            request_id = str(self._next_id)
            answer: queue.Queue = queue.Queue(maxsize=1)
            self._answers[request_id] = answer
            self.state.pending = {
                "id": request_id,
                "action": action,
                "reason": getattr(decision, "reason", ""),
                "risk": getattr(getattr(decision, "risk", None), "name", "DANGEROUS"),
            }
            self.state.visible = True
            self._touch()
        try:
            return answer.get(timeout=300)
        except queue.Empty:
            # He walked away. Silence is not consent.
            return False
        finally:
            with self._lock:
                self._answers.pop(request_id, None)
                if self.state.pending and self.state.pending["id"] == request_id:
                    self.state.pending = None
                    self._touch()

    # --------------------------------------------------------------- serving
    def run(self) -> None:
        """Serve until closed. Blocks, like the tkinter loop it replaces."""
        face = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass  # one line per poll, several times a second, is not a log

            def _send(self, code: int, body: bytes, content_type: str) -> None:
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _json(self, payload: dict) -> None:
                self._send(200, json.dumps(payload).encode(), "application/json")

            def do_GET(self):
                if self.path in ("/", "/index.html"):
                    try:
                        page = PAGE.read_bytes()
                    except OSError:
                        self._send(500, b"the page is missing", "text/plain")
                        return
                    self._send(200, page, "text/html; charset=utf-8")
                elif self.path.startswith("/api/state"):
                    with face._lock:
                        payload = {
                            "title": face.title,
                            "mode": face.state.mode,
                            "status": face.state.status,
                            "visible": face.state.visible,
                            "version": face.state.version,
                            "pending": face.state.pending,
                            "lines": [asdict(line) for line in face.state.lines],
                        }
                    self._json(payload)
                else:
                    self._send(404, b"no", "text/plain")

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                except json.JSONDecodeError:
                    self._send(400, b"bad json", "text/plain")
                    return

                if self.path == "/api/say":
                    text = str(body.get("text") or "").strip()
                    if text:
                        face.say(text, who="caleb")
                        if face.on_submit is not None:
                            # On a worker thread: a turn takes seconds, and
                            # holding the request open would stall the polls
                            # that draw the answer.
                            threading.Thread(
                                target=face.on_submit, args=(text,), daemon=True
                            ).start()
                    self._json({"ok": True})

                elif self.path == "/api/approve":
                    request_id = str(body.get("id") or "")
                    approved = bool(body.get("approved"))
                    with face._lock:
                        answer = face._answers.get(request_id)
                    if answer is not None:
                        try:
                            answer.put_nowait(approved)
                        except queue.Full:
                            pass
                    self._json({"ok": True})

                elif self.path == "/api/close":
                    self._json({"ok": True})
                    face._handle_close()
                else:
                    self._send(404, b"no", "text/plain")

        self._server = ThreadingHTTPServer((HOST, self.port), Handler)
        self._server.daemon_threads = True

        if self.open_browser:
            # A browser that cannot be launched is not an error worth
            # stopping for -- the address is printed either way.
            threading.Timer(0.4, self._open).start()

        try:
            self._server.serve_forever(poll_interval=0.2)
        finally:
            self._server.server_close()

    def _open(self) -> None:
        try:
            webbrowser.open(self.url)
        except Exception:
            log.debug("could not open a browser", exc_info=True)

    def _handle_close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        if self.on_close is not None:
            try:
                self.on_close()
            except Exception:
                log.debug("close hook raised", exc_info=True)
        if self._server is not None:
            threading.Thread(target=self._server.shutdown, daemon=True).start()
