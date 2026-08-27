"""Tests for the native Anthropic Messages channel adaptations (2026-08-26).

Offline: block-list content flattening, stop_reason mapping, provider branch
env guards. Live streaming is covered by the deployment smoke.
"""

from __future__ import annotations

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
