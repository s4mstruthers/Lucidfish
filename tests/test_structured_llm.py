# Copyright (C) 2026 Sam Struthers
# SPDX-License-Identifier: AGPL-3.0-or-later
# This file is part of Lucidfish <https://github.com/s4mstruthers/Lucidfish>; see LICENSE for the terms.

"""Structured outputs and tool use across providers, and the `ask` / `run_tools` helpers."""
import copy
import json
from types import SimpleNamespace
from unittest import mock

import pytest

from lucidfish import llm
from lucidfish.config import LLMConfig
from lucidfish.llm import Reply, ToolCall, ToolSpec, ToolsUnsupported, ask, make_provider, run_tools
from lucidfish.schemas import NOTE, parse_json

TOOL = ToolSpec("check_line", "test a line", {"type": "object", "properties": {"moves": {"type": "array"}},
                                               "required": ["moves"]})


def _response(status, payload):
    r = mock.Mock(status_code=status, headers={})
    r.json.return_value = payload
    r.text = str(payload)
    return r


def test_parse_json_tolerates_fences_and_chatter():
    assert parse_json('Sure!\n```json\n{"explanation": "x", "line_ideas": []}\n```') == {"explanation": "x",
                                                                                           "line_ideas": []}
    assert parse_json("no json here") is None and parse_json("") is None
    assert parse_json("[1, 2]") is None                       # only objects count


def test_ask_adapts_to_whatever_the_stand_in_offers():
    seen = {}

    class Full:
        def complete(self, system, messages, *, max_tokens=None, schema=None, tools=None, effort=None):
            seen.update(schema=schema, effort=effort)
            return Reply(text='{"a": 1}')

    class ChatOnly:
        def chat(self, system, messages, *, max_tokens=None):
            return "plain"

    assert ask(Full(), "s", [{"role": "user", "content": "q"}], schema=NOTE, effort="high") == '{"a": 1}'
    assert seen == {"schema": NOTE, "effort": "high"}
    assert ask(ChatOnly(), "s", [{"role": "user", "content": "q"}], schema=NOTE) == "plain"
    gen = SimpleNamespace(generate=lambda system, prompt: f"gen:{prompt}")
    assert ask(gen, "s", [{"role": "user", "content": "q"}]) == "gen:q"


class ToolModel:
    """A provider that asks for one tool call, then answers (JSON only when the schema is enforced)."""

    def __init__(self, *, unsupported=False, rounds=1):
        self.requests = []
        self.unsupported = unsupported
        self.rounds = rounds

    def complete(self, system, messages, *, max_tokens=None, schema=None, tools=None, effort=None):
        self.requests.append({"messages": list(messages), "schema": schema, "tools": tools})
        if tools and self.unsupported:
            raise ToolsUnsupported("no tools")
        calls_so_far = sum(1 for m in messages if m.get("role") == "tool")
        if tools and calls_so_far < self.rounds:
            return Reply(text="", tool_calls=[ToolCall("c1", "check_line", {"moves": ["Nf3"]})],
                         assistant={"role": "assistant", "content": "", "tool_calls": ["x"]})
        if schema:
            return Reply(text=json.dumps({"explanation": f"checked {calls_so_far} line(s)", "line_ideas": []}))
        return Reply(text="prose answer")

    def tool_turn(self, reply, results):
        return [reply.assistant, *({"role": "tool", "content": out, "tool_name": c.name} for c, out in results)]


def test_run_tools_loops_then_asks_for_the_json_answer():
    model = ToolModel()
    handled = []
    text = run_tools(model, "sys", [{"role": "user", "content": "q"}], [TOOL],
                     lambda call: handled.append(call.input) or '{"ok": true}', schema=NOTE)
    assert handled == [{"moves": ["Nf3"]}]
    assert parse_json(text) == {"explanation": "checked 1 line(s)", "line_ideas": []}
    # Tool rounds carry tools but no schema; the final request carries the schema and the tool results.
    assert model.requests[0]["tools"] == [TOOL] and model.requests[0]["schema"] is None
    assert model.requests[-1]["schema"] == NOTE
    assert any(m.get("role") == "tool" for m in model.requests[-1]["messages"])


def test_run_tools_falls_back_when_tools_are_unsupported_or_exhausted():
    model = ToolModel(unsupported=True)
    text = run_tools(model, "sys", [{"role": "user", "content": "q"}], [TOOL], lambda c: "{}", schema=NOTE)
    assert parse_json(text)["explanation"] == "checked 0 line(s)"
    # A handler error becomes a tool result the model can read, never an exception.
    model = ToolModel()
    text = run_tools(model, "sys", [{"role": "user", "content": "q"}], [TOOL],
                     lambda c: (_ for _ in ()).throw(ValueError("bad fen")), schema=NOTE)
    tool_msg = next(m for m in model.requests[-1]["messages"] if m.get("role") == "tool")
    assert json.loads(tool_msg["content"]) == {"ok": False, "error": "bad fen"}
    assert parse_json(text)
    # Out of rounds: the answer is requested without tools.
    model = ToolModel(rounds=99)
    text = run_tools(model, "sys", [{"role": "user", "content": "q"}], [TOOL], lambda c: "{}", max_rounds=2,
                     schema=NOTE)
    assert parse_json(text) and model.requests[-1]["tools"] is None


def test_ollama_sends_schema_and_tools_and_reads_tool_calls():
    provider = make_provider(LLMConfig(provider="ollama", model="qwen3:8b"))
    llm.OllamaProvider._rejected.clear()
    reply = {"message": {"content": "", "tool_calls": [{"function": {"name": "check_line",
                                                                     "arguments": {"moves": ["Nf3"]}}}]}}
    with mock.patch("requests.Session.post", return_value=_response(200, reply)) as post:
        out = provider.complete("sys", [{"role": "user", "content": "q"}], schema=NOTE, tools=[TOOL])
    payload = post.call_args.kwargs["json"]
    assert payload["format"] == NOTE
    assert payload["tools"] == [{"type": "function", "function": {"name": "check_line", "description": "test a line",
                                                                   "parameters": TOOL.input_schema}}]
    assert out.tool_calls[0].name == "check_line" and out.tool_calls[0].input == {"moves": ["Nf3"]}
    turn = provider.tool_turn(out, [(out.tool_calls[0], '{"ok": true}')])
    assert turn[0]["role"] == "assistant" and turn[1] == {"role": "tool", "content": '{"ok": true}',
                                                          "tool_name": "check_line"}


def test_ollama_model_without_tool_support_is_reported_and_remembered():
    provider = make_provider(LLMConfig(provider="ollama", model="gemma3:12b"))
    llm.OllamaProvider._rejected.clear()
    replies = [_response(400, {"error": "gemma3:12b does not support tools"})]
    with mock.patch("requests.Session.post", side_effect=replies):
        with pytest.raises(ToolsUnsupported):
            provider.complete("sys", [{"role": "user", "content": "q"}], tools=[TOOL])
    with mock.patch("requests.Session.post") as post:        # no request is wasted the second time
        with pytest.raises(ToolsUnsupported):
            provider.complete("sys", [{"role": "user", "content": "q"}], tools=[TOOL])
    assert post.call_count == 0


def test_ollama_drops_a_rejected_format_and_remembers_it():
    provider = make_provider(LLMConfig(provider="ollama", model="old:7b"))
    llm.OllamaProvider._rejected.clear()
    ok = _response(200, {"message": {"content": '{"explanation": "fine", "line_ideas": []}'}})
    replies = [_response(400, {"error": "invalid format: schema not supported"}), ok, ok]
    with mock.patch("requests.Session.post", side_effect=replies) as post:
        assert parse_json(provider.complete("sys", [{"role": "user", "content": "q"}], schema=NOTE).text)
        assert "format" not in post.call_args.kwargs["json"]
        provider.complete("sys", [{"role": "user", "content": "q"}], schema=NOTE)
    assert post.call_count == 3


def test_openai_compatible_sends_json_schema_and_tools():
    provider = make_provider(LLMConfig(provider="openai", api_key="sk-live-abcdefgh1234"))
    llm.OpenAICompatibleProvider._rejected.clear()
    reply = {"choices": [{"message": {"content": None, "tool_calls": [
        {"id": "call_9", "type": "function", "function": {"name": "check_line", "arguments": '{"moves": ["e4"]}'}}]}}]}
    with mock.patch("requests.Session.post", return_value=_response(200, reply)) as post:
        out = provider.complete("sys", [{"role": "user", "content": "q"}], schema=NOTE, tools=[TOOL])
    payload = post.call_args.kwargs["json"]
    assert payload["response_format"] == {"type": "json_schema",
                                          "json_schema": {"name": "note", "schema": NOTE, "strict": True}}
    assert payload["tools"][0]["function"]["name"] == "check_line"
    assert out.tool_calls == [ToolCall("call_9", "check_line", {"moves": ["e4"]})]
    turn = provider.tool_turn(out, [(out.tool_calls[0], "{}")])
    assert turn[1] == {"role": "tool", "tool_call_id": "call_9", "content": "{}"}
    # Other OpenAI-compatible servers do not get `strict` (many reject it).
    groq = make_provider(LLMConfig(provider="groq", api_key="gsk-abcdefgh1234"))
    with mock.patch("requests.Session.post", return_value=_response(
            200, {"choices": [{"message": {"content": "{}"}}]})) as post:
        groq.complete("sys", [{"role": "user", "content": "q"}], schema=NOTE)
    assert "strict" not in post.call_args.kwargs["json"]["response_format"]["json_schema"]


def test_openai_compatible_retries_without_response_format():
    provider = make_provider(LLMConfig(provider="custom", model="local-7b"))
    llm.OpenAICompatibleProvider._rejected.clear()
    replies = [_response(400, {"error": {"message": "response_format is not supported by this server"}}),
               _response(200, {"choices": [{"message": {"content": "EXPLANATION: plain"}}]})]
    with mock.patch("requests.Session.post", side_effect=replies) as post:
        assert provider.complete("sys", [{"role": "user", "content": "q"}], schema=NOTE).text == "EXPLANATION: plain"
    assert "response_format" not in post.call_args.kwargs["json"]


def test_claude_capability_flags_follow_the_model_generation():
    pytest.importorskip("anthropic")
    flags = {}
    for model in ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-haiku-4-5", "claude-opus-4-6",
                  "claude-fable-5-1"):
        p = make_provider(LLMConfig(provider="anthropic", model=model, api_key="sk-ant-abcdef123456"))
        flags[model] = (p._sampling, p._effort, p._fallbacks)
    assert flags["claude-opus-5-5"] == (False, True, True)       # no temperature, effort + fallbacks supported
    assert flags["claude-haiku-5-5"] == (False, True, False)
    assert flags["claude-haiku-4-5"] == (True, False, False)     # older: temperature yes, effort no
    assert flags["claude-opus-4-6"] == (True, True, False)       # the last Opus that takes a temperature
    assert flags["claude-fable-5-1"] == (False, True, True)


def test_claude_requests_carry_effort_and_schema_and_drop_rejected_options():
    anthropic = pytest.importorskip("anthropic")
    provider = make_provider(LLMConfig(provider="anthropic", model="claude-opus-5-5", api_key="sk-ant-abcdef123456"))
    llm.AnthropicProvider._rejected.clear()
    text_block = SimpleNamespace(type="text", text='{"explanation": "fine", "line_ideas": []}')
    response = SimpleNamespace(content=[text_block], stop_reason="end_turn")
    sent = []

    def create(**kwargs):
        sent.append(copy.deepcopy(kwargs))         # the provider edits its request between attempts
        if len(sent) == 1:
            raise anthropic.BadRequestError(message="output_config.format: unsupported for this model",
                                            response=mock.Mock(status_code=400, headers={}), body=None)
        return response

    provider._client.beta.messages.create = create
    provider._client.messages.create = create
    out = provider.complete("sys", [{"role": "user", "content": "q"}], schema=NOTE, effort="high")
    assert parse_json(out.text)["explanation"] == "fine"
    assert sent[0]["output_config"] == {"effort": "high", "format": {"type": "json_schema", "schema": NOTE}}
    assert sent[1]["output_config"] == {"effort": "high"}                 # the format was dropped and remembered
    assert "temperature" not in str(sent[0].get("extra_body", ""))       # 5.x models reject sampling parameters
    provider.complete("sys", [{"role": "user", "content": "q"}], schema=NOTE, effort="low")
    assert sent[-1]["output_config"] == {"effort": "low"} and len(sent) == 3


def test_claude_client_names_the_workspace_when_the_key_needs_one(monkeypatch):
    pytest.importorskip("anthropic")
    monkeypatch.delenv("ANTHROPIC_WORKSPACE_ID", raising=False)
    plain = make_provider(LLMConfig(provider="anthropic", model="claude-opus-5-5", api_key="sk-ant-abcdef123456"))
    assert "anthropic-workspace-id" not in {k.lower() for k in plain._client.default_headers}
    monkeypatch.setenv("ANTHROPIC_WORKSPACE_ID", " wrkspc_01abc ")
    scoped = make_provider(LLMConfig(provider="anthropic", model="claude-opus-5-5", api_key="sk-ant-abcdef123456"))
    assert scoped._client.default_headers["anthropic-workspace-id"] == "wrkspc_01abc"


def test_claude_workspace_error_explains_the_fix():
    anthropic = pytest.importorskip("anthropic")
    provider = make_provider(LLMConfig(provider="anthropic", model="claude-opus-5-5", api_key="sk-ant-abcdef123456"))
    llm.AnthropicProvider._rejected.clear()

    def create(**kwargs):
        raise anthropic.BadRequestError(
            message="This API key is not scoped to a workspace, so this request must include the "
                    "anthropic-workspace-id header with the ID of the workspace to use.",
            response=mock.Mock(status_code=400, headers={}), body=None)

    provider._client.beta.messages.create = create
    provider._client.messages.create = create
    with pytest.raises(llm.LLMError) as info:
        provider.complete("sys", [{"role": "user", "content": "q"}])
    assert info.value.fatal and "ANTHROPIC_WORKSPACE_ID" in str(info.value)
    assert provider._rejected == {}                       # nothing was mistaken for an unsupported option


def test_note_paragraphs_survive_parsing_and_fact_cleaning():
    from lucidfish.prompts import _drop_sentences
    from lucidfish.schemas import note_from
    text, _ = note_from({"explanation": "Verdict first.  It hangs a rook.\n\n  Then the line.\nStill the same "
                                        "paragraph.\n\n\nThe lesson.", "line_ideas": []})
    assert text == "Verdict first. It hangs a rook.\n\nThen the line. Still the same paragraph.\n\nThe lesson."
    cleaned, removed = _drop_sentences(text, lambda s: ["bad"] if "line" in s else [])
    assert (cleaned, removed) == ("Verdict first. It hangs a rook.\n\nStill the same paragraph.\n\nThe lesson.", 1)
    assert _drop_sentences("Only this. Gone.", lambda s: ["x"] if "Gone" in s else []) == ("Only this.", 1)


def test_run_tools_keeps_tools_defined_and_replays_full_assistant_turns():
    """A history with tool calls is only valid alongside the tools it called, and the assistant turn that
    gets replayed must be the provider's complete message, not a text-only copy of it."""
    pytest.importorskip("anthropic")
    provider = make_provider(LLMConfig(provider="anthropic", model="claude-opus-5-5", api_key="sk-ant-abcdef123456"))
    llm.AnthropicProvider._rejected.clear()
    thinking = SimpleNamespace(type="thinking", thinking="", signature="sig")
    sent = []

    def create(**kwargs):
        sent.append(copy.deepcopy(kwargs))
        n = len(sent)
        if n == 1:   # a tool call
            return SimpleNamespace(stop_reason="tool_use", content=[
                thinking, SimpleNamespace(type="tool_use", id="t1", name="check_line", input={"moves": ["Nf3"]})])
        if n == 2:   # prose instead of JSON
            return SimpleNamespace(stop_reason="end_turn",
                                   content=[thinking, SimpleNamespace(type="text", text="prose")])
        return SimpleNamespace(stop_reason="end_turn", content=[
            SimpleNamespace(type="text", text='{"explanation": "done", "line_ideas": []}')])

    provider._client.beta.messages.create = create
    provider._client.messages.create = create
    text = run_tools(provider, "sys", [{"role": "user", "content": "q"}], [TOOL], lambda c: "{}", schema=NOTE)
    assert parse_json(text)["explanation"] == "done"
    assert all("tools" in req for req in sent)                                   # never dropped
    assert sent[-1]["output_config"]["format"]["schema"] == NOTE
    replayed = [m for m in sent[-1]["messages"] if m["role"] == "assistant"]
    assert replayed and all(m["content"][0].type == "thinking" for m in replayed)   # complete turns, reasoning included
