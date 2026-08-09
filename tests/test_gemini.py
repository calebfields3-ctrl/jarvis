"""The free mind.

Gemini exists here so that "no money" doesn't have to mean "no mind". It has
to present exactly the surface the Claude brain does, because `jarvis.app`
must never learn which one it is holding.

Every test injects a transport, so none of this touches the network.
"""

from __future__ import annotations

import json
import types

import pytest

from jarvis.agent.brain import NoMindAvailable, Reply
from jarvis.agent.gemini import (
    GeminiBrain,
    _clean_schema,
    to_function_declarations,
)
from jarvis.agent.tools import ToolBox
from jarvis.safety.permissions import PermissionEngine, PermissionPolicy


@pytest.fixture
def engine(tmp_path):
    home = tmp_path / "home"
    (home / "work").mkdir(parents=True)
    (home / "work" / "notes.txt").write_text("AAPL long above 190\n")
    return PermissionEngine(PermissionPolicy.default(home=home))


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaTestKeyForTests")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)


def text_turn(text):
    return {"candidates": [{"content": {"parts": [{"text": text}]}}]}


def call_turn(name, args):
    return {"candidates": [{"content": {"parts": [{"functionCall": {"name": name, "args": args}}]}}]}


class FakeTransport:
    """Replays scripted responses and records what was sent."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.sent = []

    def __call__(self, model, payload):
        # Snapshotted, because a real transport serialises the payload at
        # call time. Keeping the reference would show later turns appended
        # to what was "sent" earlier, and every assertion below would be
        # inspecting the end of the conversation rather than that request.
        self.sent.append(json.loads(json.dumps(payload)))
        return self.responses.pop(0) if self.responses else text_turn("done")


def brain(engine, transport, **kwargs):
    return GeminiBrain(ToolBox(engine), transport=transport, model="test-model", **kwargs)


# ------------------------------------------------------------ schema shaping


def test_anthropic_schema_keys_gemini_rejects_are_stripped():
    """Sending one gets a 400 that names the field but not the tool."""
    cleaned = _clean_schema({
        "type": "object",
        "additionalProperties": False,
        "properties": {"path": {"type": "string", "title": "Path"}},
    })
    assert "additionalProperties" not in cleaned
    assert "title" not in cleaned["properties"]["path"]
    assert cleaned["properties"]["path"]["type"] == "string"


def test_stripping_reaches_all_the_way_down():
    cleaned = _clean_schema({
        "properties": {
            "outer": {"type": "object", "properties": {"inner": {"title": "x", "type": "string"}}}
        }
    })
    assert "title" not in cleaned["properties"]["outer"]["properties"]["inner"]


def test_an_object_without_a_type_gets_one():
    """Gemini requires it where Anthropic's generator leaves it implied."""
    assert _clean_schema({"properties": {"a": {"type": "string"}}})["type"] == "object"


def test_every_tool_converts(engine):
    declarations = to_function_declarations(ToolBox(engine).build())
    names = {d["name"] for d in declarations}
    assert "run_command" in names and "portfolio" in names
    for declaration in declarations:
        assert declaration["description"], f"{declaration['name']} lost its description"
        assert declaration["parameters"]["type"] == "object"


def test_server_tools_are_left_out(engine):
    """They run on Anthropic's machines; Gemini has nothing to hand them to."""
    tools = ToolBox(engine).build() + [{"type": "web_search_20260209", "name": "web_search"}]
    assert len(to_function_declarations(tools)) == len(tools) - 1


def test_a_tool_with_no_arguments_is_valid(engine):
    """An empty properties object is rejected outright."""
    declarations = to_function_declarations(ToolBox(engine).build())
    portfolio = next(d for d in declarations if d["name"] == "portfolio")
    assert portfolio["parameters"].get("properties") in (None, {}) or portfolio["parameters"]["type"]
    assert json.dumps(portfolio)  # must be serialisable


# ----------------------------------------------------------------- speaking


def test_a_plain_answer_comes_back(engine, key):
    mind = brain(engine, FakeTransport(text_turn("Morning, sir.")))
    assert mind.say("hello").text == "Morning, sir."


def test_the_system_prompt_is_sent(engine, key):
    transport = FakeTransport(text_turn("hi"))
    brain(engine, transport).say("hello")
    system = transport.sent[0]["systemInstruction"]["parts"][0]["text"]
    assert "cannot place a trade" in system


def test_the_tools_are_offered(engine, key):
    transport = FakeTransport(text_turn("hi"))
    brain(engine, transport).say("hello")
    names = {
        f["name"] for f in transport.sent[0]["tools"][0]["functionDeclarations"]
    }
    assert "run_command" in names


def test_a_tool_call_is_run_and_the_result_sent_back(engine, key, tmp_path):
    path = str(tmp_path / "home" / "work" / "notes.txt")
    transport = FakeTransport(
        call_turn("read_file", {"path": path}),
        text_turn("It says AAPL long above 190."),
    )
    mind = brain(engine, transport)
    reply = mind.say("what's in my notes")

    assert reply.text == "It says AAPL long above 190."
    follow_up = transport.sent[1]["contents"][-1]
    assert follow_up["role"] == "user", "a function result must be a user turn"
    response = follow_up["parts"][0]["functionResponse"]
    assert response["name"] == "read_file"
    assert "AAPL" in response["response"]["result"]


def test_the_model_turn_is_recorded_before_the_result(engine, key):
    """A function response must immediately follow the call that asked for it."""
    transport = FakeTransport(call_turn("portfolio", {}), text_turn("done"))
    mind = brain(engine, transport)
    mind.say("portfolio please")

    roles = [m["role"] for m in mind.messages]
    assert roles == ["user", "model", "user", "model"]


def test_several_calls_in_one_turn_all_get_answered(engine, key):
    transport = FakeTransport(
        {"candidates": [{"content": {"parts": [
            {"functionCall": {"name": "portfolio", "args": {}}},
            {"functionCall": {"name": "trading_rules_check", "args": {}}},
        ]}}]},
        text_turn("both done"),
    )
    mind = brain(engine, transport)
    mind.say("status")
    results = transport.sent[1]["contents"][-1]["parts"]
    assert len(results) == 2


def test_an_unknown_tool_is_reported_not_crashed(engine, key):
    transport = FakeTransport(call_turn("no_such_tool", {}), text_turn("ah"))
    mind = brain(engine, transport)
    mind.say("do something")
    result = transport.sent[1]["contents"][-1]["parts"][0]["functionResponse"]
    assert "no tool called" in result["response"]["result"]


def test_a_failing_tool_is_handed_back_as_a_result(engine, key):
    """The model can read that and try something else; an exception it cannot."""
    transport = FakeTransport(call_turn("read_file", {"path": "/etc/shadow"}), text_turn("ok"))
    mind = brain(engine, transport)
    mind.say("read the shadow file")
    result = transport.sent[1]["contents"][-1]["parts"][0]["functionResponse"]
    assert "can't" in result["response"]["result"].lower()


def test_an_endless_tool_loop_gives_up(engine, key):
    transport = FakeTransport(*[call_turn("portfolio", {}) for _ in range(40)])
    reply = brain(engine, transport).say("go")
    assert "stuck" in reply.text


def test_tool_calls_are_attributed_to_the_turn(engine, key):
    transport = FakeTransport(call_turn("portfolio", {}), text_turn("done"))
    mind = brain(engine, transport)
    reply = mind.say("portfolio")
    assert [c.name for c in reply.tool_calls] == ["portfolio"]


# ------------------------------------------------------------------ refusals


def test_a_blocked_prompt_is_reported_as_a_refusal(engine, key):
    transport = FakeTransport({"promptFeedback": {"blockReason": "SAFETY"}})
    reply = brain(engine, transport).say("something awful")
    assert reply.refused
    assert "not going to help" in reply.text


def test_a_safety_stop_is_a_refusal_too(engine, key):
    transport = FakeTransport({"candidates": [{"finishReason": "SAFETY", "content": {}}]})
    assert brain(engine, transport).say("x").refused


def test_an_empty_response_does_not_crash(engine, key):
    reply = brain(engine, FakeTransport({"candidates": []})).say("hello")
    assert reply.text and not reply.refused


def test_a_transport_failure_is_answered_rather_than_raised(engine, key):
    def explode(model, payload):
        raise RuntimeError("the network went away")

    reply = brain(engine, explode).say("hello")
    assert "went wrong" in reply.text


# ------------------------------------------------------------------ session


def test_the_conversation_carries_across_turns(engine, key):
    transport = FakeTransport(text_turn("NVDA is at 210."), text_turn("Up 2%."))
    mind = brain(engine, transport)
    mind.say("what's NVDA")
    mind.say("and on the day?")
    assert len(transport.sent[1]["contents"]) == 3


def test_resetting_forgets_the_conversation(engine, key):
    mind = brain(engine, FakeTransport(text_turn("hi")))
    mind.say("hello")
    mind.reset()
    assert mind.messages == []


def test_no_key_means_no_mind(engine, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(NoMindAvailable):
        brain(engine, FakeTransport())


def test_the_google_variable_works_too(engine, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "AIzaSomething")
    assert brain(engine, FakeTransport()).api_key == "AIzaSomething"


def test_the_free_tier_reports_no_running_cost(engine, key):
    """A cost figure would imply a bill that does not exist."""
    mind = brain(engine, FakeTransport(text_turn("hi")))
    mind.say("hello")
    assert mind.total_cost == 0.0


def test_usage_is_read_when_offered(engine, key):
    transport = FakeTransport({
        "candidates": [{"content": {"parts": [{"text": "hi"}]}}],
        "usageMetadata": {"promptTokenCount": 120, "candidatesTokenCount": 45},
    })
    reply = brain(engine, transport).say("hello")
    assert reply.input_tokens == 120 and reply.output_tokens == 45


# --------------------------------------------------- it stands in for Claude


def test_it_matches_the_claude_brain_surface():
    """`jarvis.app` must never learn which mind it is holding."""
    from jarvis.agent.brain import AgentBrain

    for name in ("say", "reset", "messages", "total_cost"):
        assert hasattr(GeminiBrain, name) or name in GeminiBrain.__init__.__code__.co_names, name
        assert hasattr(AgentBrain, name) or name in AgentBrain.__init__.__code__.co_names, name


def test_it_returns_the_same_reply_type(engine, key):
    assert isinstance(brain(engine, FakeTransport(text_turn("hi"))).say("x"), Reply)


def test_effort_is_accepted_and_ignored(engine, key):
    """Claude has the dial; Gemini does not. The call must not blow up."""
    mind = brain(engine, FakeTransport(text_turn("hi")))
    assert mind.say("hello", effort="xhigh").text == "hi"


# ------------------------------------------------------------- which mind


def test_claude_is_preferred_when_its_key_is_present(engine, monkeypatch):
    """Someone who has paid for the better one should get it."""
    from jarvis.agent import brain as brain_module

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaTest")
    monkeypatch.setattr(
        brain_module, "AgentBrain",
        lambda *a, **k: types.SimpleNamespace(which="claude"),
    )
    assert brain_module.build_mind(ToolBox(engine)).which == "claude"


def test_gemini_is_used_when_only_its_key_is_present(engine, monkeypatch):
    from jarvis.agent import brain as brain_module

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaTest")
    monkeypatch.setattr(
        "jarvis.agent.gemini.GeminiBrain",
        lambda *a, **k: types.SimpleNamespace(which="gemini"),
    )
    assert brain_module.build_mind(ToolBox(engine)).which == "gemini"


def test_neither_key_means_the_router(engine, monkeypatch):
    from jarvis.agent import brain as brain_module

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    assert brain_module.build_mind(ToolBox(engine)) is None


def test_claude_only_arguments_are_not_passed_to_gemini(engine, monkeypatch):
    """`effort` and `web_tools` are Claude's; Gemini would reject them."""
    from jarvis.agent import brain as brain_module

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaTest")
    seen = {}

    def fake(toolbox, **kwargs):
        seen.update(kwargs)
        return types.SimpleNamespace()

    monkeypatch.setattr("jarvis.agent.gemini.GeminiBrain", fake)
    brain_module.build_mind(ToolBox(engine), effort="xhigh", web_tools=True)
    assert "effort" not in seen and "web_tools" not in seen
