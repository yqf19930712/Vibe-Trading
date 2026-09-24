"""Prompt-cache counters ride the usage pipeline (llm_usage / attempt_stats).

``usage_metadata.input_token_details`` carries LangChain's cache breakdown
(``cache_read`` / ``cache_creation``, or the per-TTL split langchain-anthropic
reports instead of the generic creation key). They are a breakdown of
``input_tokens``, never added to it, and only appear when non-zero so a
channel without caching keeps the original three-field shape.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from src.agent.loop import AgentLoop, _normalize_llm_usage
from src.providers.chat import LLMResponse


def test_plain_usage_keeps_three_fields() -> None:
    assert _normalize_llm_usage(
        {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
    ) == {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}


def test_cache_read_and_creation_are_passed_through() -> None:
    usage = {
        "input_tokens": 1000,
        "output_tokens": 50,
        "total_tokens": 1050,
        "input_token_details": {"cache_read": 800, "cache_creation": 150},
    }
    assert _normalize_llm_usage(usage) == {
        "input_tokens": 1000,
        "output_tokens": 50,
        "total_tokens": 1050,
        "cache_read_tokens": 800,
        "cache_creation_tokens": 150,
    }


def test_per_ttl_creation_split_is_summed() -> None:
    usage = {
        "input_tokens": 500,
        "output_tokens": 1,
        "input_token_details": {
            "cache_read": 0,
            "cache_creation": 0,
            "ephemeral_5m_input_tokens": 300,
            "ephemeral_1h_input_tokens": 20,
        },
    }
    normalized = _normalize_llm_usage(usage)
    assert normalized["cache_creation_tokens"] == 320
    assert "cache_read_tokens" not in normalized


class _UsageLLM:
    model_name = "stub"

    def stream_chat(self, messages, tools=None, **_: Any) -> LLMResponse:
        return LLMResponse(
            content="答案",
            usage_metadata={
                "input_tokens": 1200,
                "output_tokens": 30,
                "total_tokens": 1230,
                "input_token_details": {"cache_read": 1000, "cache_creation": 100},
            },
        )


def test_loop_emits_cache_counters(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.agent.tools import ToolRegistry

    monkeypatch.setenv("VIBE_DATA_DIR", str(tmp_path / "tenant"))
    events: list[tuple[str, dict]] = []
    agent = AgentLoop(
        registry=ToolRegistry(),
        llm=_UsageLLM(),
        max_iterations=2,
        event_callback=lambda ev, data: events.append((ev, data)),
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    agent.memory.run_dir = str(run_dir)

    result = agent.run("q")

    assert result["status"] == "success"
    usage_events = [d for ev, d in events if ev == "llm_usage"]
    assert usage_events[0]["cache_read_tokens"] == 1000
    assert usage_events[0]["cache_creation_tokens"] == 100
    stats = [d for ev, d in events if ev == "attempt_stats"][-1]
    assert stats["tokens"] == {
        "input": 1200,
        "output": 30,
        "total": 1230,
        "cache_read": 1000,
        "cache_creation": 100,
    }
