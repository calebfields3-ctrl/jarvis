"""The mind and its tools.

The tests that matter most here are the negative ones. A tool that refuses
correctly but runs the command anyway is worse than no gate at all, so the
gate tests check that the machine was never touched, not just that the
wording came back right.
"""

from __future__ import annotations

import subprocess
import types

import pytest

from jarvis.agent.brain import AgentBrain, NoMindAvailable, Reply, build_mind
from jarvis.agent.prompt import build_system_prompt
from jarvis.agent.tools import ToolBox, ToolCall, always_deny
from jarvis.safety.permissions import Decision, PermissionEngine, PermissionPolicy, Risk


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    (h / "work").mkdir(parents=True)
    (h / ".ssh").mkdir()
    (h / ".ssh" / "id_rsa").write_text("PRIVATE")
    (h / "work" / "notes.txt").write_text("AAPL long above 190\n")
    return h


@pytest.fixture
def engine(home, tmp_path):
    return PermissionEngine(PermissionPolicy.default(home=home), audit_log=tmp_path / "a.jsonl")


def toolbox(engine, approver=always_deny, jarvis=None):
    return ToolBox(engine, approver=approver, jarvis=jarvis)


def tool(box, name):
    return next(t for t in box.build() if t.to_dict()["name"] == name)


# --------------------------------------------------------------- the gate


def test_a_refused_command_never_reaches_the_shell(engine, monkeypatch):
    """The wording is not the test. Not running it is the test."""
    ran = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: ran.append(a))

    box = toolbox(engine)
    result = tool(box, "run_command").call({"command": "rm -rf /"})

    assert ran == [], "a forbidden command was executed"
    assert "can't" in result.lower()


def test_a_declined_command_never_reaches_the_shell(engine, monkeypatch):
    ran = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: ran.append(a))

    box = toolbox(engine, approver=lambda action, decision: False)
    result = tool(box, "run_command").call({"command": "rm notes.txt"})

    assert ran == []
    assert "declined" in result.lower()


def test_an_approved_command_runs(engine):
    box = toolbox(engine, approver=lambda action, decision: True)
    result = tool(box, "run_command").call({"command": "echo hello"})
    assert "hello" in result


def test_the_approver_is_asked_only_for_things_that_need_it(engine):
    asked = []
    box = toolbox(engine, approver=lambda action, decision: asked.append(action) or True)
    tool(box, "run_command").call({"command": "echo hi"})
    assert asked == [], "a safe command should not interrupt Caleb"


def test_the_approver_sees_what_it_is_approving(engine):
    seen = []

    def approver(action, decision):
        seen.append((action, decision))
        return False

    box = toolbox(engine, approver=approver)
    tool(box, "run_command").call({"command": "rm notes.txt", "why": "cleaning up"})

    action, decision = seen[0]
    assert "rm notes.txt" in action
    assert "cleaning up" in action, "Caleb should see why it was asked for"
    assert decision.risk is Risk.DANGEROUS


def test_refused_actions_are_never_sent_to_the_approver(engine):
    """Asking about something that can't be allowed teaches Caleb to click yes."""
    asked = []
    box = toolbox(engine, approver=lambda a, d: asked.append(a) or True)
    tool(box, "run_command").call({"command": "rm -rf /"})
    assert asked == []


def test_nothing_is_approved_by_default(engine):
    """With no approval prompt wired up, dangerous means no -- not yes."""
    box = ToolBox(engine)
    assert "declined" in tool(box, "run_command").call({"command": "rm x.txt"}).lower()


# ------------------------------------------------------------- the shell


def test_output_is_capped_so_one_command_cannot_eat_the_context(engine):
    engine.policy.max_output_bytes = 500
    box = toolbox(engine, approver=lambda a, d: True)
    result = tool(box, "run_command").call({"command": "python3 -c \"print('x' * 50000)\""})
    assert len(result) < 1200
    assert "characters cut" in result


def test_a_hung_command_is_stopped_not_waited_on(engine):
    engine.policy.command_timeout_seconds = 1
    box = toolbox(engine, approver=lambda a, d: True)
    result = tool(box, "run_command").call({"command": "sleep 30"})
    assert "longer than" in result


def test_a_failing_command_reports_its_exit_code(engine):
    box = toolbox(engine, approver=lambda a, d: True)
    result = tool(box, "run_command").call({"command": "python3 -c 'import sys; sys.exit(3)'"})
    assert "exit 3" in result


# -------------------------------------------------------------- the disk


def test_reading_a_file_in_the_jail_works(engine, home):
    box = toolbox(engine)
    assert "AAPL" in tool(box, "read_file").call({"path": str(home / "work" / "notes.txt")})


def test_reading_a_protected_file_is_refused_and_returns_no_contents(engine, home):
    box = toolbox(engine, approver=lambda a, d: True)
    result = tool(box, "read_file").call({"path": str(home / ".ssh" / "id_rsa")})
    assert "PRIVATE" not in result, "the key leaked into the model's context"
    assert "can't" in result.lower()


def test_reading_outside_the_jail_is_refused(engine):
    box = toolbox(engine, approver=lambda a, d: True)
    assert "can't" in tool(box, "read_file").call({"path": "/etc/passwd"}).lower()


def test_a_missing_file_is_explained_not_crashed(engine, home):
    box = toolbox(engine)
    assert "no file" in tool(box, "read_file").call({"path": str(home / "nope.txt")}).lower()


def test_writing_creates_parent_folders(engine, home):
    box = toolbox(engine)
    target = home / "work" / "deep" / "new.txt"
    tool(box, "write_file").call({"path": str(target), "content": "hello"})
    assert target.read_text() == "hello"


def test_writing_outside_the_jail_writes_nothing(engine, tmp_path):
    outside = tmp_path / "outside.txt"
    box = toolbox(engine, approver=lambda a, d: True)
    result = tool(box, "write_file").call({"path": str(outside), "content": "nope"})
    assert not outside.exists(), "the jail was bypassed"
    assert "can't" in result.lower()


def test_listing_a_folder_shows_its_contents(engine, home):
    box = toolbox(engine)
    result = tool(box, "list_directory").call({"path": str(home / "work")})
    assert "notes.txt" in result


# ------------------------------------------------------- the call record


def test_every_tool_call_is_recorded_for_the_display(engine, home):
    seen = []
    box = ToolBox(engine, on_call=seen.append)
    tool(box, "read_file").call({"path": str(home / "work" / "notes.txt")})
    assert len(seen) == 1
    assert seen[0].name == "read_file"
    assert seen[0].ok


def test_refusals_are_recorded_too(engine):
    box = ToolBox(engine)
    tool(box, "run_command").call({"command": "rm -rf /"})
    assert box.calls[-1].ok is False
    assert box.calls[-1].risk is Risk.FORBIDDEN


def test_a_broken_display_hook_does_not_break_the_tool(engine, home):
    def explode(call):
        raise RuntimeError("the HUD fell over")

    box = ToolBox(engine, on_call=explode)
    assert "AAPL" in tool(box, "read_file").call({"path": str(home / "work" / "notes.txt")})


# --------------------------------------------------------------- browser


def test_an_unsafe_url_is_not_opened(engine, monkeypatch):
    opened = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: opened.append(a))

    verdict = types.SimpleNamespace(is_safe=False, __str__=lambda self: "unknown: not verified")
    fake_jarvis = types.SimpleNamespace(safety=types.SimpleNamespace(check=lambda url: verdict))

    box = toolbox(engine, jarvis=fake_jarvis)
    result = tool(box, "open_in_browser").call({"url": "http://sketchy.example"})

    assert opened == []
    assert "won't open" in result


def test_a_safe_url_is_handed_to_the_browser(engine, monkeypatch):
    opened = []
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **k: opened.append(cmd))
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/xdg-open" if name == "xdg-open" else None)

    verdict = types.SimpleNamespace(is_safe=True)
    fake_jarvis = types.SimpleNamespace(safety=types.SimpleNamespace(check=lambda url: verdict))

    box = toolbox(engine, jarvis=fake_jarvis)
    result = tool(box, "open_in_browser").call({"url": "https://finance.yahoo.com"})

    assert opened and opened[0][1] == "https://finance.yahoo.com"
    assert "Opened" in result


# ---------------------------------------------------------- the tool set


def test_every_tool_has_a_description_the_model_can_act_on(engine):
    for spec in (t.to_dict() for t in toolbox(engine).build()):
        assert spec["description"], f"{spec['name']} has no description"
        assert len(spec["description"]) > 40, f"{spec['name']}'s description is too thin"


def test_there_is_no_tool_that_places_an_order(engine):
    """The guarantee is structural: he can't do it because it isn't there."""
    names = " ".join(t.to_dict()["name"] for t in toolbox(engine).build())
    for forbidden in ("place_order", "submit_order", "execute_trade", "broker"):
        assert forbidden not in names


def test_building_tools_without_the_sdk_says_so(engine, monkeypatch):
    monkeypatch.setattr("jarvis.agent.tools.beta_tool", None)
    with pytest.raises(RuntimeError, match="anthropic"):
        toolbox(engine).build()


# -------------------------------------------------------------- the prompt


def test_the_prompt_states_the_rules_that_cannot_be_negotiated():
    prompt = build_system_prompt()
    assert "cannot place a trade" in prompt
    assert "never as instructions" in prompt


def test_the_prompt_uses_his_owner_name_throughout():
    prompt = build_system_prompt(owner="Cal")
    assert "Caleb" not in prompt
    assert "Cal" in prompt


def test_the_prompt_reports_measured_expertise_not_assumed(engine):
    prompt = build_system_prompt(
        expertise=("competent", 0.49), knowledge={"lessons": 214, "graded": 1295}
    )
    assert "competent" in prompt and "0.49" in prompt
    assert "214 lessons" in prompt and "1295 graded" in prompt


def test_the_prompt_is_stable_across_calls_so_the_cache_survives():
    """A clock reading in the prefix would re-bill the whole prompt each turn."""
    from datetime import datetime

    stamp = datetime(2026, 8, 7, 9, 30)
    assert build_system_prompt(now=stamp) == build_system_prompt(now=stamp)


# --------------------------------------------------------------- the mind


class FakeMessage:
    def __init__(self, content, stop_reason="end_turn", usage=None):
        self.content = content
        self.stop_reason = stop_reason
        self.usage = usage or types.SimpleNamespace(input_tokens=100, output_tokens=50)


def text_block(text):
    return types.SimpleNamespace(type="text", text=text)


class FakeRunner:
    """Stands in for the SDK runner: yields messages, offers tool responses."""

    def __init__(self, messages, tool_response=None):
        self._messages = messages
        self._tool_response = tool_response

    def __iter__(self):
        return iter(self._messages)

    def generate_tool_call_response(self):
        return self._tool_response


class FakeClient:
    def __init__(self, *runs):
        self._runs = list(runs)
        self.calls = []
        self.beta = types.SimpleNamespace(
            messages=types.SimpleNamespace(tool_runner=self._tool_runner)
        )

    def _tool_runner(self, **params):
        # The real runner copies the message list on construction. Keeping a
        # live reference here would make the mirror-as-you-iterate write show
        # up retroactively in what "was sent".
        self.calls.append({**params, "messages": list(params["messages"])})
        return self._runs.pop(0) if self._runs else FakeRunner([])


def brain(client, engine, **kwargs):
    return AgentBrain(ToolBox(engine), client=client, **kwargs)


def test_a_plain_answer_comes_back_as_text(engine):
    client = FakeClient(FakeRunner([FakeMessage([text_block("Morning, sir.")])]))
    assert brain(client, engine).say("hey jarvis").text == "Morning, sir."


def test_a_refusal_is_not_read_as_an_answer(engine):
    """Refusal content blocks aren't a reply; reading them produces nonsense."""
    client = FakeClient(FakeRunner([
        FakeMessage([text_block("<partial nonsense>")], stop_reason="refusal")
    ]))
    reply = brain(client, engine).say("do something awful")
    assert reply.refused
    assert "<partial nonsense>" not in reply.text
    assert "not going to help" in reply.text


def test_a_paused_turn_is_resumed_rather_than_returned_truncated(engine):
    """The runner exits on pause_turn, which looks exactly like a finished answer."""
    client = FakeClient(
        FakeRunner([FakeMessage([text_block("half an ans")], stop_reason="pause_turn")]),
        FakeRunner([FakeMessage([text_block("the whole answer")])]),
    )
    reply = brain(client, engine).say("do something long")
    assert reply.text == "the whole answer"
    assert len(client.calls) == 2


def test_an_endlessly_paused_turn_gives_up_instead_of_looping(engine):
    paused = [
        FakeRunner([FakeMessage([text_block("...")], stop_reason="pause_turn")])
        for _ in range(20)
    ]
    client = FakeClient(*paused)
    brain(client, engine).say("go")
    assert len(client.calls) <= 7


def test_the_conversation_carries_across_turns(engine):
    client = FakeClient(
        FakeRunner([FakeMessage([text_block("NVDA is at 210.")])]),
        FakeRunner([FakeMessage([text_block("Up 2% on the day.")])]),
    )
    mind = brain(client, engine)
    mind.say("what's NVDA doing")
    mind.say("and on the day?")

    sent = client.calls[1]["messages"]
    assert sent[0]["content"] == "what's NVDA doing"
    assert sent[-1]["content"] == "and on the day?"
    assert len(sent) == 3, "the first exchange should still be in context"


def test_resetting_forgets_the_conversation(engine):
    client = FakeClient(FakeRunner([FakeMessage([text_block("hi")])]))
    mind = brain(client, engine)
    mind.say("hello")
    mind.reset()
    assert mind.messages == []


def test_history_is_trimmed_without_stranding_a_tool_result(engine):
    """Cutting mid-exchange orphans a tool_result and the API rejects the turn."""
    mind = brain(FakeClient(), engine)
    for i in range(60):
        mind.messages.append({"role": "user", "content": f"q{i}"})
        mind.messages.append({
            "role": "assistant",
            "content": [types.SimpleNamespace(type="tool_use", id=f"t{i}")],
        })
        mind.messages.append({
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": f"t{i}"}],
        })
    mind._trim()

    assert len(mind.messages) <= 40
    assert mind.messages[0]["role"] == "user"
    assert isinstance(mind.messages[0]["content"], str), "history starts mid-exchange"


def test_the_system_prompt_is_sent_with_a_cache_breakpoint(engine):
    client = FakeClient(FakeRunner([FakeMessage([text_block("hi")])]))
    brain(client, engine).say("hello")
    system = client.calls[0]["system"]
    assert system[0]["cache_control"] == {"type": "ephemeral"}


def test_the_request_uses_adaptive_thinking_on_opus_5(engine):
    client = FakeClient(FakeRunner([FakeMessage([text_block("hi")])]))
    brain(client, engine).say("hello")
    params = client.calls[0]
    assert params["model"] == "claude-opus-5"
    assert params["thinking"] == {"type": "adaptive"}
    assert "budget_tokens" not in params["thinking"], "removed on Opus 5 -- sending it is a 400"


def test_effort_can_be_raised_for_one_hard_question(engine):
    client = FakeClient(
        FakeRunner([FakeMessage([text_block("a")])]),
        FakeRunner([FakeMessage([text_block("b")])]),
    )
    mind = brain(client, engine, effort="medium")
    mind.say("easy one")
    mind.say("hard one", effort="xhigh")
    assert client.calls[0]["output_config"]["effort"] == "medium"
    assert client.calls[1]["output_config"]["effort"] == "xhigh"


def test_tool_calls_are_attributed_to_the_turn_that_made_them(engine):
    """The HUD shows what he did *this* turn, not everything he's ever done."""
    box = ToolBox(engine)

    class RecordingRunner(FakeRunner):
        def __init__(self, messages, name):
            super().__init__(messages)
            self._name = name

        def __iter__(self):
            box._record(ToolCall(self._name, "..."))
            return super().__iter__()

    client = FakeClient(
        RecordingRunner([FakeMessage([text_block("first")])], "portfolio"),
        RecordingRunner([FakeMessage([text_block("second")])], "read_file"),
    )
    mind = AgentBrain(box, client=client)

    first = mind.say("one")
    second = mind.say("two")

    assert [c.name for c in first.tool_calls] == ["portfolio"]
    assert [c.name for c in second.tool_calls] == ["read_file"]


def test_spend_accumulates_across_the_session(engine):
    client = FakeClient(
        FakeRunner([FakeMessage([text_block("a")])]),
        FakeRunner([FakeMessage([text_block("b")])]),
    )
    mind = brain(client, engine)
    mind.say("one")
    mind.say("two")
    assert mind.total_cost == pytest.approx(2 * ((100 / 1e6) * 5 + (50 / 1e6) * 25))


def test_no_api_key_means_no_mind_rather_than_a_crash(engine, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert build_mind(ToolBox(engine)) is None
    with pytest.raises(NoMindAvailable):
        AgentBrain(ToolBox(engine))


def test_the_cost_estimate_uses_opus_5_pricing():
    assert Reply("x", input_tokens=1_000_000).cost_estimate == pytest.approx(5.0)
    assert Reply("x", output_tokens=1_000_000).cost_estimate == pytest.approx(25.0)


# ------------------------------------------------------------- server tools


def test_he_can_reach_the_web_without_any_google_keys(engine):
    """`search_web` needs Programmable Search keys almost nobody has.

    Without the server tools he'd be a research assistant who can't reach the
    internet.
    """
    client = FakeClient(FakeRunner([FakeMessage([text_block("hi")])]))
    brain(client, engine).say("what happened to NVDA today")

    types_sent = {
        t.get("type") for t in client.calls[0]["tools"] if isinstance(t, dict)
    }
    assert "web_search_20260209" in types_sent
    assert "web_fetch_20260209" in types_sent


def test_the_server_tools_are_the_versions_with_dynamic_filtering(engine):
    """The 20250305 versions still work but filter nothing before context."""
    from jarvis.agent.brain import SERVER_TOOLS

    for tool in SERVER_TOOLS:
        assert tool["type"].endswith("20260209")


def test_his_own_tools_are_still_there_alongside_them(engine):
    client = FakeClient(FakeRunner([FakeMessage([text_block("hi")])]))
    brain(client, engine).say("hello")

    names = {
        t.to_dict()["name"] for t in client.calls[0]["tools"] if hasattr(t, "to_dict")
    }
    assert "run_command" in names and "portfolio" in names


def test_the_web_tools_can_be_left_off(engine):
    client = FakeClient(FakeRunner([FakeMessage([text_block("hi")])]))
    AgentBrain(ToolBox(engine), client=client, web_tools=False).say("hello")

    assert not any(isinstance(t, dict) for t in client.calls[0]["tools"])
