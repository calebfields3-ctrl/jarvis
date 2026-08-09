"""A second mind, for when there is no money for the first.

Caleb is a teenager whose parents would rather he did not attach a card to
something running on his laptop. That is a reasonable position, and Google's
Gemini API has a free tier that needs no card at all -- so rather than the
choice being "pay or stay stupid", it is "pay for the better one, or use the
free one".

Built on ``urllib`` rather than ``google-genai``. Every dependency is one
more thing that can fail to install on a Chromebook, and this machine has
already had pip trouble. The REST surface is small enough that the SDK would
be buying very little.

The interface matches :class:`jarvis.agent.brain.AgentBrain` exactly --
``say`` returning a :class:`Reply`, ``reset``, ``messages``, ``total_cost``
-- so :mod:`jarvis.app` never learns which mind it is holding.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any, Callable

from jarvis.agent.brain import NoMindAvailable, Reply
from jarvis.agent.prompt import build_system_prompt
from jarvis.agent.tools import ToolBox

log = logging.getLogger(__name__)

API_ROOT = "https://generativelanguage.googleapis.com/v1beta"

# Free-tier default. `_discover_model` normally replaces this by asking the
# API what it actually offers, because Google renames these regularly and a
# hard-coded name is a time bomb.
DEFAULT_MODEL = "gemini-2.5-flash"

MAX_TOOL_ROUNDS = 12
REQUEST_TIMEOUT = 120

# Keys the Anthropic tool schemas carry that Gemini's stricter OpenAPI subset
# rejects outright.
UNSUPPORTED_SCHEMA_KEYS = frozenset({
    "additionalProperties", "title", "$schema", "default", "examples",
})


def api_key_available() -> bool:
    return bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))


def _clean_schema(node: Any) -> Any:
    """Strip what Gemini will not accept, recursively.

    Sending an unsupported key gets the whole request rejected with a 400
    that names the field but not the tool it came from, which is a miserable
    thing to debug from the outside.
    """
    if isinstance(node, dict):
        cleaned = {
            key: _clean_schema(value)
            for key, value in node.items()
            if key not in UNSUPPORTED_SCHEMA_KEYS
        }
        # Gemini requires a type on every object node; Anthropic's generator
        # occasionally omits it where it is implied.
        if "properties" in cleaned and "type" not in cleaned:
            cleaned["type"] = "object"
        return cleaned
    if isinstance(node, list):
        return [_clean_schema(item) for item in node]
    return node


def to_function_declarations(tools: list) -> list[dict]:
    """Anthropic tool objects -> Gemini function declarations."""
    declarations = []
    for tool in tools:
        # Server tools are plain dicts with no local implementation; Gemini
        # has no equivalent to hand them to.
        if isinstance(tool, dict):
            continue
        spec = tool.to_dict()
        schema = _clean_schema(spec.get("input_schema") or {})
        if not schema.get("properties"):
            # A function with no parameters must omit the key entirely --
            # an empty properties object is rejected.
            schema = {"type": "object"}
        declarations.append({
            "name": spec["name"],
            "description": spec.get("description", ""),
            "parameters": schema,
        })
    return declarations


class GeminiBrain:
    """Gemini holding Jarvis's tools, with the same surface as the Claude one."""

    def __init__(
        self,
        toolbox: ToolBox,
        *,
        jarvis: Any = None,
        model: str | None = None,
        api_key: str | None = None,
        on_text: Callable[[str], None] | None = None,
        transport: Callable[[str, dict], dict] | None = None,
    ) -> None:
        self.toolbox = toolbox
        self.jarvis = jarvis
        self.on_text = on_text
        # Injectable so the tests never touch the network.
        self._transport = transport or self._post

        key = api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not key:
            raise NoMindAvailable(
                "No GEMINI_API_KEY is set. Get one free at aistudio.google.com."
            )
        self.api_key = key

        self.tools = toolbox.build()
        self._by_name = {
            tool.to_dict()["name"]: tool
            for tool in self.tools
            if not isinstance(tool, dict)
        }
        self.declarations = to_function_declarations(self.tools)
        self.model = model or os.environ.get("JARVIS_GEMINI_MODEL") or self._discover_model()
        self.system = self._build_system()
        self.messages: list[dict[str, Any]] = []
        # Free tier. Reporting a running total would imply a bill that does
        # not exist.
        self.total_cost = 0.0

    # ---------------------------------------------------------------- setup
    def _discover_model(self) -> str:
        """Ask the API which models exist rather than hard-coding a name.

        Google renames these often enough that a fixed string eventually
        fails with a 404 that reads like the key is wrong.
        """
        try:
            with urllib.request.urlopen(
                f"{API_ROOT}/models?key={self.api_key}", timeout=20
            ) as response:
                catalogue = json.loads(response.read().decode())
        except Exception as exc:
            log.debug("could not list Gemini models: %s", exc)
            return DEFAULT_MODEL

        usable = [
            entry["name"].split("/")[-1]
            for entry in catalogue.get("models", [])
            if "generateContent" in (entry.get("supportedGenerationMethods") or [])
        ]
        # Prefer flash: it is the one with a real free-tier quota, and speed
        # matters more than depth for something spoken aloud.
        for wanted in ("flash-latest", "2.5-flash", "flash"):
            for name in usable:
                if wanted in name and "thinking" not in name:
                    return name
        return usable[0] if usable else DEFAULT_MODEL

    def _build_system(self) -> str:
        owner, expertise, knowledge = "Caleb", None, None
        jarvis = self.jarvis or self.toolbox._jarvis
        if jarvis is not None:
            try:
                owner = jarvis.memory.profile.name or owner
                expertise = jarvis.memory.knowledge.expertise_level()
                knowledge = jarvis.memory.knowledge.stats()
            except Exception:
                log.debug("couldn't read state for the prompt", exc_info=True)
        return build_system_prompt(
            owner=owner, expertise=expertise, knowledge=knowledge
        )

    # --------------------------------------------------------------- speaking
    def say(self, text: str, *, effort: str | None = None) -> Reply:
        """One exchange. ``effort`` is accepted and ignored -- Gemini has no dial."""
        self.messages.append({"role": "user", "parts": [{"text": text}]})
        before = len(self.toolbox.calls)

        try:
            reply = self._run()
        except NoMindAvailable:
            raise
        except Exception as exc:
            log.exception("the Gemini turn failed")
            reply = Reply(text=f"That went wrong on my end, sir: {exc}")

        reply.tool_calls = self.toolbox.calls[before:]
        if self.on_text is not None and reply.text:
            try:
                self.on_text(reply.text)
            except Exception:
                log.debug("on_text hook raised", exc_info=True)
        return reply

    def _run(self) -> Reply:
        for _ in range(MAX_TOOL_ROUNDS):
            payload: dict[str, Any] = {
                "systemInstruction": {"parts": [{"text": self.system}]},
                "contents": self.messages,
            }
            if self.declarations:
                payload["tools"] = [{"functionDeclarations": self.declarations}]

            data = self._transport(self.model, payload)

            candidates = data.get("candidates") or []
            if not candidates:
                blocked = (data.get("promptFeedback") or {}).get("blockReason")
                if blocked:
                    return Reply(
                        text="I'm not going to help with that one, sir.", refused=True
                    )
                return Reply(text="I didn't get an answer back, sir.")

            candidate = candidates[0]
            if candidate.get("finishReason") == "SAFETY":
                return Reply(
                    text="I'm not going to help with that one, sir.", refused=True
                )

            parts = (candidate.get("content") or {}).get("parts") or []
            self.messages.append({"role": "model", "parts": parts})

            calls = [p["functionCall"] for p in parts if "functionCall" in p]
            if not calls:
                usage = data.get("usageMetadata") or {}
                return Reply(
                    text="\n".join(
                        p["text"] for p in parts if p.get("text")
                    ).strip(),
                    input_tokens=usage.get("promptTokenCount", 0) or 0,
                    output_tokens=usage.get("candidatesTokenCount", 0) or 0,
                )

            # A function response must be its own user turn, immediately
            # after the model turn that asked for it.
            results = []
            for call in calls:
                results.append({
                    "functionResponse": {
                        "name": call.get("name", ""),
                        "response": {"result": self._run_tool(call)},
                    }
                })
            self.messages.append({"role": "user", "parts": results})

        return Reply(
            text="I got stuck going back and forth with myself on that one, sir. "
                 "Try asking it a different way."
        )

    def _run_tool(self, call: dict) -> str:
        name = call.get("name", "")
        tool = self._by_name.get(name)
        if tool is None:
            return f"There's no tool called {name!r}."
        try:
            return str(tool.call(call.get("args") or {}))
        except Exception as exc:
            # Handed back as a result rather than raised: the model can read
            # it and try something else, which is what you want.
            log.debug("tool %s failed", name, exc_info=True)
            return f"That tool failed: {exc}"

    # -------------------------------------------------------------- transport
    def _post(self, model: str, payload: dict) -> dict:
        url = f"{API_ROOT}/models/{model}:generateContent?key={self.api_key}"
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode()[:400]
            if exc.code in (401, 403):
                raise NoMindAvailable(
                    "Gemini rejected the key. Make a new one at aistudio.google.com."
                ) from exc
            if exc.code == 429:
                raise RuntimeError(
                    "That's the free tier's limit for now. It resets shortly."
                ) from exc
            raise RuntimeError(f"Gemini said no ({exc.code}): {detail}") from exc

    # --------------------------------------------------------------- session
    def reset(self) -> None:
        self.messages.clear()
