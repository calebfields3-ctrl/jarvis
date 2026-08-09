"""The agent: a real mind on top of the trading machinery.

``jarvis.brain`` is a router -- it matches what you said against a list of
things it knows how to do. That is predictable and cheap, and it is why the
answer to anything off the list is a shrug.

This package puts Claude in that seat instead. He gets the same trading
tools, plus a shell, the filesystem, and a browser, and decides for himself
which to reach for. Everything he does goes through
``jarvis.safety.permissions`` first.
"""

from jarvis.agent.tools import ToolBox, ToolCall
from jarvis.agent.prompt import build_system_prompt

__all__ = ["ToolBox", "ToolCall", "build_system_prompt"]
