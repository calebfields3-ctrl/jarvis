"""The mind: Claude, holding Jarvis's tools.

``jarvis.brain.Jarvis`` matches what you said against a list of things it
knows how to do. This drives the same machinery with a model that decides
for itself which tool to reach for, and can answer things nobody wrote a
route for.

The loop is the SDK's ``tool_runner`` rather than a hand-written one. Two
details it does not handle, which this file does:

* **Refusals.** ``stop_reason == "refusal"`` means the content blocks are not
  a normal answer. Reading them as one produces gibberish, so the check
  comes first.
* **Paused turns.** A long server-side turn can stop with ``pause_turn``, and
  the Python runner exits rather than resuming. That looks exactly like a
  finished answer, just truncated, with no error. So the conversation is
  mirrored as the runner iterates and the runner restarted on a pause.

Without an API key none of this is reachable, and Jarvis falls back to the
router. He is less capable that way, but he still works, which matters
because the fallback is also what runs when the network is down.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from jarvis.agent.prompt import build_system_prompt
from jarvis.agent.tools import ToolBox

log = logging.getLogger(__name__)

MODEL = "claude-opus-5"

# How hard he thinks before answering. 'medium' is the default because this
# is a conversation -- he is being spoken to out loud and a ten-second pause
# before "morning, sir" is worse than a slightly shallower answer. Analysis
# that deserves more can raise it per call.
DEFAULT_EFFORT = os.environ.get("JARVIS_EFFORT", "medium")

MAX_TOKENS = 8_000
# A paused turn that never resumes would loop forever; a handful of restarts
# is enough for any real server-tool run.
MAX_PAUSE_RESTARTS = 5
# Turns kept in context. Older ones are dropped from the front, always in
# whole user/assistant pairs so a tool_use never loses its tool_result.
MAX_HISTORY_TURNS = 40

# Anthropic-hosted search and page fetching. These matter more than they look:
# Jarvis's own `search_web` needs Google Programmable Search keys, which almost
# nobody has, so without these he is a research assistant who cannot reach the
# internet. These need no keys at all beyond the API key already required.
#
# They do not pass through `jarvis.safety.url_safety` -- the fetching happens on
# Anthropic's servers, not here. That gate still guards every URL *this* machine
# opens, which is the one that matters, since a page Jarvis merely reads cannot
# run anything and a page opened in Caleb's browser can.
SERVER_TOOLS: tuple[dict[str, str], ...] = (
    {"type": "web_search_20260209", "name": "web_search"},
    {"type": "web_fetch_20260209", "name": "web_fetch"},
)


class NoMindAvailable(RuntimeError):
    """No API key, or no anthropic package. Jarvis falls back to the router."""


@dataclass
class Reply:
    """One complete answer, and what it cost to produce."""

    text: str
    tool_calls: list = field(default_factory=list)
    refused: bool = False
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def cost_estimate(self) -> float:
        """Roughly what that turn cost, at Opus 5 list prices."""
        return (self.input_tokens / 1e6) * 5.0 + (self.output_tokens / 1e6) * 25.0


def api_key_available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


class AgentBrain:
    """Claude with Jarvis's tools, his memory, and his manners."""

    def __init__(
        self,
        toolbox: ToolBox,
        *,
        jarvis: Any = None,
        model: str = MODEL,
        effort: str = DEFAULT_EFFORT,
        api_key: str | None = None,
        on_text: Callable[[str], None] | None = None,
        client: Any = None,
        web_tools: bool = True,
    ) -> None:
        self.toolbox = toolbox
        self.jarvis = jarvis
        self.model = model
        self.effort = effort
        self.on_text = on_text
        self.messages: list[dict[str, Any]] = []
        self.total_cost = 0.0

        if client is not None:
            self.client = client
        else:
            key = api_key or os.environ.get("ANTHROPIC_API_KEY")
            if not key:
                raise NoMindAvailable(
                    "No ANTHROPIC_API_KEY is set, so there's no mind to run. "
                    "Jarvis will use his built-in routing instead."
                )
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover - install-time path
                raise NoMindAvailable(
                    "The anthropic package isn't installed. Run: pip install anthropic"
                ) from exc
            self.client = anthropic.Anthropic(api_key=key)

        # Server tools are plain dicts alongside the decorated functions; the
        # runner passes them through and Anthropic executes them.
        self.tools = [*toolbox.build(), *SERVER_TOOLS] if web_tools else toolbox.build()
        self.system = self._build_system()

    # ------------------------------------------------------------- context
    def _build_system(self) -> str:
        """Built once per session, on purpose.

        Rebuilding it per turn would put a fresh clock reading in the prompt
        prefix, which changes the cached bytes and silently throws away the
        prompt cache on every single message -- the whole system block and
        tool schemas re-billed at full rate each turn.
        """
        owner, expertise, knowledge, portfolio_line = "Caleb", None, None, None
        jarvis = self.jarvis or self.toolbox._jarvis
        if jarvis is not None:
            try:
                owner = jarvis.memory.profile.name or owner
                expertise = jarvis.memory.knowledge.expertise_level()
                knowledge = jarvis.memory.knowledge.stats()
            except Exception:
                log.debug("couldn't read state for the prompt", exc_info=True)
        return build_system_prompt(
            owner=owner,
            expertise=expertise,
            knowledge=knowledge,
            portfolio_line=portfolio_line,
        )

    @property
    def _system_blocks(self) -> list[dict[str, Any]]:
        """The system prompt with a cache breakpoint at its end.

        The prompt and the tool schemas are the same on every turn of a
        session and they are not small. Caching them is most of the
        difference between this costing pennies a day and dollars.
        """
        return [{
            "type": "text",
            "text": self.system,
            "cache_control": {"type": "ephemeral"},
        }]

    def _trim(self) -> None:
        """Drop the oldest turns once the conversation gets long.

        Cuts only at user turns that begin a fresh exchange. Slicing anywhere
        else can strand a ``tool_result`` whose ``tool_use`` has just been
        dropped, and the API rejects that outright.
        """
        while len(self.messages) > MAX_HISTORY_TURNS:
            del self.messages[0]
            while self.messages and not self._starts_a_turn(self.messages[0]):
                del self.messages[0]

    @staticmethod
    def _starts_a_turn(message: dict[str, Any]) -> bool:
        if message.get("role") != "user":
            return False
        content = message.get("content")
        if isinstance(content, str):
            return True
        return not any(
            getattr(block, "type", None) == "tool_result"
            or (isinstance(block, dict) and block.get("type") == "tool_result")
            for block in content or []
        )

    # ---------------------------------------------------------------- turn
    def say(self, text: str, *, effort: str | None = None) -> Reply:
        """One exchange: Caleb speaks, Jarvis works, Jarvis answers."""
        self.messages.append({"role": "user", "content": text})
        self._trim()

        before = len(self.toolbox.calls)
        reply = self._run(effort or self.effort)
        reply.tool_calls = self.toolbox.calls[before:]
        self.total_cost += reply.cost_estimate

        if self.on_text is not None and reply.text:
            try:
                self.on_text(reply.text)
            except Exception:
                log.debug("on_text hook raised", exc_info=True)
        return reply

    def _run(self, effort: str) -> Reply:
        restarts = 0
        last = None

        while True:
            runner = self.client.beta.messages.tool_runner(
                model=self.model,
                max_tokens=MAX_TOKENS,
                system=self._system_blocks,
                thinking={"type": "adaptive"},
                output_config={"effort": effort},
                tools=self.tools,
                messages=self.messages,
            )

            for message in runner:
                last = message
                # The runner keeps its own history and doesn't expose it, so
                # this mirror is the only way to resume a paused turn.
                self.messages.append({"role": "assistant", "content": message.content})
                tool_response = runner.generate_tool_call_response()
                if tool_response is not None:
                    self.messages.append(tool_response)

            if last is None:
                return Reply(text="", refused=False)
            if last.stop_reason != "pause_turn":
                break

            restarts += 1
            if restarts > MAX_PAUSE_RESTARTS:
                log.warning("turn still paused after %d restarts", MAX_PAUSE_RESTARTS)
                break

        usage = getattr(last, "usage", None)
        reply = Reply(
            text=self._text_of(last),
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
        )

        if last.stop_reason == "refusal":
            # The content blocks on a refusal are not an answer, so they are
            # not read. Say so honestly rather than inventing a reply.
            reply.refused = True
            reply.text = (
                "I'm not going to help with that one, sir. Ask me something else."
            )
        return reply

    @staticmethod
    def _text_of(message) -> str:
        parts = [
            block.text for block in getattr(message, "content", []) or []
            if getattr(block, "type", None) == "text"
        ]
        return "\n".join(p for p in parts if p).strip()

    # -------------------------------------------------------------- session
    def reset(self) -> None:
        """Forget this conversation. Long-term memory is untouched."""
        self.messages.clear()


def build_mind(
    toolbox: ToolBox,
    *,
    jarvis: Any = None,
    on_text: Callable[[str], None] | None = None,
    **kwargs,
):
    """Whichever mind the available keys allow, or None.

    Claude first when its key is present -- it is the better one, and someone
    who has paid for it should get it. Gemini otherwise, because its free
    tier means "no money" does not have to mean "no mind". Returns None
    rather than raising: every caller's answer to having neither is the same,
    fall back to the router, and that is an ordinary state rather than an
    error.
    """
    if api_key_available():
        try:
            return AgentBrain(toolbox, jarvis=jarvis, on_text=on_text, **kwargs)
        except NoMindAvailable as exc:
            log.info("Claude unavailable: %s", exc)

    from jarvis.agent.gemini import GeminiBrain
    from jarvis.agent.gemini import api_key_available as gemini_key

    if gemini_key():
        try:
            # `effort` and `web_tools` are Claude's; Gemini has neither.
            usable = {k: v for k, v in kwargs.items() if k in {"model", "api_key"}}
            return GeminiBrain(toolbox, jarvis=jarvis, on_text=on_text, **usable)
        except NoMindAvailable as exc:
            log.info("Gemini unavailable: %s", exc)

    log.info("no API key for either provider -- falling back to the router")
    return None
