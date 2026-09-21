"""Tests for the native Anthropic Messages channel adaptations (2026-08-26).

Offline: block-list content flattening, stop_reason mapping, provider branch
env guards. Live streaming is covered by the deployment smoke.
"""

from __future__ import annotations

import pytest

from src.providers.chat import (
    ChatLLM,
    _content_text,
    _content_thinking,
)


class _FakeMessage:
    """Duck-typed AIMessage stand-in for _parse_response."""

    def __init__(self, content, tool_calls=None, response_metadata=None,
                 additional_kwargs=None, usage_metadata=None):
        self.content = content
        self.tool_calls = tool_calls or []
        self.response_metadata = response_metadata or {}
        self.additional_kwargs = additional_kwargs or {}
        self.usage_metadata = usage_metadata


ANTHROPIC_BLOCKS = [
    {"type": "thinking", "thinking": "先比较两只票……", "signature": "sig=="},
    {"type": "text", "text": "查 INTC。"},
    {"type": "tool_use", "id": "toolu_1", "name": "get_price", "input": {"symbol": "INTC"}},
]


class TestContentFlattening:
    def test_str_passthrough(self):
        assert _content_text("hello") == "hello"
        assert _content_thinking("hello") == ""

    def test_block_list(self):
        assert _content_text(ANTHROPIC_BLOCKS) == "查 INTC。"
        assert _content_thinking(ANTHROPIC_BLOCKS) == "先比较两只票……"

    def test_none_and_empty(self):
        assert _content_text(None) == ""
        assert _content_text([]) == ""
        assert _content_thinking([]) == ""


class TestParseResponseAnthropic:
    def test_block_content_and_stop_reason(self):
        msg = _FakeMessage(
            content=ANTHROPIC_BLOCKS,
            tool_calls=[{"id": "toolu_1", "name": "get_price", "args": {"symbol": "INTC"}}],
            response_metadata={"stop_reason": "tool_use"},
            usage_metadata={"input_tokens": 10, "output_tokens": 20, "total_tokens": 30},
        )
        r = ChatLLM._parse_response(msg)
        assert r.content == "查 INTC。"
        assert r.reasoning_content == "先比较两只票……"
        assert r.finish_reason == "tool_calls"
        assert r.tool_calls[0].name == "get_price"
        assert r.tool_calls[0].arguments == {"symbol": "INTC"}
        assert r.usage_metadata["total_tokens"] == 30

    def test_stop_reason_mapping(self):
        for stop, expected in (
            ("end_turn", "stop"),
            ("max_tokens", "length"),
            ("stop_sequence", "stop"),
        ):
            r = ChatLLM._parse_response(_FakeMessage("hi", response_metadata={"stop_reason": stop}))
            assert r.finish_reason == expected, stop

    def test_openai_finish_reason_untouched(self):
        r = ChatLLM._parse_response(
            _FakeMessage("hi", response_metadata={"finish_reason": "tool_callstool_calls"})
        )
        assert r.finish_reason == "tool_calls"

    def test_missing_metadata_defaults_stop(self):
        r = ChatLLM._parse_response(_FakeMessage("hi"))
        assert r.finish_reason == "stop"

    def test_openai_reasoning_content_priority(self):
        msg = _FakeMessage(
            content="正文",
            additional_kwargs={"reasoning_content": "openai 通道思考"},
        )
        assert ChatLLM._parse_response(msg).reasoning_content == "openai 通道思考"


class TestBuildAnthropicBranch:
    def test_requires_credentials(self, monkeypatch):
        from src.providers.llm import _build_native_anthropic

        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        try:
            _build_native_anthropic("claude-opus-5")
            raise AssertionError("expected RuntimeError")
        except RuntimeError as exc:
            assert "ANTHROPIC" in str(exc)

    def test_adaptive_default_for_5_family(self, monkeypatch):
        from src.providers.llm import _build_native_anthropic

        monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
        monkeypatch.delenv("VIBE_ANTHROPIC_THINKING", raising=False)
        llm = _build_native_anthropic("claude-opus-5")
        assert getattr(llm, "thinking", None) == {"type": "adaptive"}

    def test_thinking_off_for_legacy_models(self, monkeypatch):
        from src.providers.llm import _build_native_anthropic

        monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
        monkeypatch.delenv("VIBE_ANTHROPIC_THINKING", raising=False)
        llm = _build_native_anthropic("claude-3-7-sonnet-latest")
        assert getattr(llm, "thinking", None) in (None, {})

    def test_sync_env_leaves_openai_alone(self, monkeypatch):
        from src.providers.llm import _sync_provider_env

        monkeypatch.setenv("LANGCHAIN_PROVIDER", "anthropic")
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api-direct.example")
        monkeypatch.setenv("OPENAI_BASE_URL", "https://openai.example/v1")
        _sync_provider_env()
        import os

        assert os.environ["OPENAI_BASE_URL"] == "https://openai.example/v1"


class TestAnthropicCacheBreakpoints:
    """E3: prompt-caching breakpoints injected into the native /v1/messages
    payload — system tail, tools tail, newest non-status message."""

    @staticmethod
    def _apply(payload):
        from src.providers.llm import _apply_anthropic_cache_breakpoints

        _apply_anthropic_cache_breakpoints(payload)
        return payload

    def test_string_system_wrapped_with_cache_control(self):
        payload = self._apply({"system": "you are an agent", "messages": []})
        assert payload["system"] == [
            {
                "type": "text",
                "text": "you are an agent",
                "cache_control": {"type": "ephemeral"},
            }
        ]

    def test_block_system_marks_last_block(self):
        payload = self._apply(
            {"system": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}
        )
        assert "cache_control" not in payload["system"][0]
        assert payload["system"][1]["cache_control"] == {"type": "ephemeral"}

    def test_tools_tail_marked(self):
        payload = self._apply(
            {"tools": [{"name": "t1"}, {"name": "t2"}], "system": "s", "messages": []}
        )
        assert "cache_control" not in payload["tools"][0]
        assert payload["tools"][1]["cache_control"] == {"type": "ephemeral"}

    def test_last_message_string_content_wrapped(self):
        payload = self._apply(
            {"messages": [{"role": "user", "content": "question"}]}
        )
        assert payload["messages"][0]["content"] == [
            {"type": "text", "text": "question", "cache_control": {"type": "ephemeral"}}
        ]

    def test_status_bar_message_skipped(self):
        """The per-iteration <agent_status> tail changes every turn — the
        breakpoint must land on the newest stable message instead."""
        payload = self._apply(
            {
                "messages": [
                    {"role": "user", "content": "real question"},
                    {"role": "user", "content": "<agent_status>\nNow: ...\n</agent_status>"},
                ]
            }
        )
        assert payload["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}
        assert payload["messages"][1]["content"] == "<agent_status>\nNow: ...\n</agent_status>"

    def test_block_content_marks_last_cacheable_block(self):
        payload = self._apply(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "tool_result", "tool_use_id": "t1", "content": "ok"},
                            {"type": "tool_result", "tool_use_id": "t2", "content": "ok"},
                        ],
                    }
                ]
            }
        )
        blocks = payload["messages"][0]["content"]
        assert "cache_control" not in blocks[0]
        assert blocks[1]["cache_control"] == {"type": "ephemeral"}

    def test_thinking_only_message_falls_back_to_older(self):
        payload = self._apply(
            {
                "messages": [
                    {"role": "user", "content": "stable question"},
                    {
                        "role": "assistant",
                        "content": [{"type": "thinking", "thinking": "..."}],
                    },
                ]
            }
        )
        assert payload["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}
        assert "cache_control" not in payload["messages"][1]["content"][0]

    def test_malformed_payload_is_noop(self):
        from src.providers.llm import _apply_anthropic_cache_breakpoints

        _apply_anthropic_cache_breakpoints(None)
        _apply_anthropic_cache_breakpoints({"messages": "not-a-list", "system": 3})


class TestToolChoiceNoneNativeChannel:
    """The forced text turn keeps ``tools`` and sends ``tool_choice={"type":"none"}``.

    Real ``ChatAnthropic`` (langchain-anthropic) request shaping, offline: the
    engine's OpenAI-format tool definitions survive ``bind_tools``, the dict
    form of ``tool_choice`` reaches the payload unchanged, and adaptive
    thinking does not strip it (the thinking guard only drops ``any``/``tool``).
    """

    TOOLS = [{"type": "function", "function": {
        "name": "get_price", "description": "d",
        "parameters": {"type": "object", "properties": {"symbol": {"type": "string"}}},
    }}]

    @pytest.fixture()
    def native_llm(self, monkeypatch):
        pytest.importorskip("langchain_anthropic")
        from src.providers.llm import _build_native_anthropic

        monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
        monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
        monkeypatch.delenv("VIBE_ANTHROPIC_THINKING", raising=False)
        monkeypatch.delenv("VIBE_ANTHROPIC_MAX_TOKENS", raising=False)
        monkeypatch.delenv("VIBE_MAX_OUTPUT_TOKENS", raising=False)
        return _build_native_anthropic("claude-opus-5")

    def test_payload_keeps_tools_and_none_with_adaptive_thinking(self, native_llm, recwarn):
        from langchain_core.messages import HumanMessage

        assert native_llm.thinking == {"type": "adaptive"}
        bound = native_llm.bind_tools(self.TOOLS, tool_choice={"type": "none"})
        assert bound.kwargs["tool_choice"] == {"type": "none"}

        payload = native_llm._get_request_payload([HumanMessage("q")], **bound.kwargs)

        assert payload["tool_choice"] == {"type": "none"}
        assert [t["name"] for t in payload["tools"]] == ["get_price"]
        assert payload["thinking"] == {"type": "adaptive"}
        assert payload["max_tokens"] == 32000
        assert not [w for w in recwarn if "tool_choice" in str(w.message)]

    def test_forced_tool_is_what_the_thinking_guard_drops(self, native_llm):
        """Guard sanity: ``any`` is dropped under thinking, so ``none`` surviving is meaningful."""
        with pytest.warns(UserWarning, match="tool_choice is forced"):
            bound = native_llm.bind_tools(self.TOOLS, tool_choice="any")
        assert "tool_choice" not in bound.kwargs

    def test_chat_llm_bind_reaches_the_same_payload(self, native_llm, monkeypatch):
        from langchain_core.messages import HumanMessage

        from src.providers.chat import TOOL_CHOICE_NONE, ChatLLM

        monkeypatch.setenv("LANGCHAIN_PROVIDER", "anthropic")
        monkeypatch.setenv("LANGCHAIN_MODEL_NAME", "claude-opus-5")
        monkeypatch.setattr("src.providers.chat.build_llm", lambda **_kw: native_llm)
        client = ChatLLM()
        bound = client._bind(self.TOOLS, TOOL_CHOICE_NONE)
        payload = bound.bound._get_request_payload([HumanMessage("q")], **bound.kwargs)
        assert payload["tool_choice"] == {"type": "none"}
        assert payload["tools"]
