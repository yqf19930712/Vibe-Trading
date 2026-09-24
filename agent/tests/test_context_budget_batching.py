"""Compaction layers rewrite the trajectory in batches, and know the window.

* Layer 1, once armed, cuts again only after the context grew by
  ``MICROCOMPACT_BATCH_RATIO`` of the threshold — not on every turn (its
  release line sits below the usual unprunable floor, so it rarely disarms).
* Layer 2's fold boundary advances in steps of ``COLLAPSE_STRIDE`` messages
  when the loop passes its state, so the message that just slid out of the
  recent window is not rewritten every turn.
* The ``compact`` tool does not summarise a small context.
* ``VIBE_CONTEXT_WINDOW_TOKENS`` caps the threshold, counting tool schemas
  and the measured real/estimate token ratio, which ``attempt_stats`` reports.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import src.agent.loop as loop_mod
from src.agent.loop import (
    COLLAPSE_PRESERVE_RECENT,
    COLLAPSE_STRIDE,
    COLLAPSE_TEXT_MIN,
    MICROCOMPACT_BATCH_RATIO,
    AgentLoop,
    _CLEARED_PLACEHOLDER,
    _context_collapse,
    _microcompact,
    estimate_tokens,
)
from src.agent.tools import ToolRegistry
from src.providers.chat import LLMResponse


def _tool(i: int, size: int = 4000) -> dict:
    return {"role": "tool", "tool_call_id": f"c{i}", "name": "read_file",
            "content": f"result {i} " + "x" * size}


class TestLayer1Batching:
    def test_armed_layer_waits_for_growth_before_cutting_again(self) -> None:
        threshold = 20_000
        messages = [_tool(i) for i in range(20)]
        state: dict[str, Any] = {}
        _microcompact(messages, token_threshold=threshold, state=state)
        assert state["armed"] is True and "cut_at" in state
        snapshot = [m["content"] for m in messages]
        cleared_first = snapshot.count(_CLEARED_PLACEHOLDER)
        assert cleared_first > 0

        # One more ~1k-token result: below the batch margin → untouched.
        messages.append(_tool(100))
        _microcompact(messages, token_threshold=threshold, state=state)
        assert [m["content"] for m in messages[:-1]] == snapshot

        # Enough new results to pass the margin → one more cut.
        extra = int(threshold * MICROCOMPACT_BATCH_RATIO / 1000) + 2
        messages.extend(_tool(200 + i) for i in range(extra))
        _microcompact(messages, token_threshold=threshold, state=state)
        cleared_second = [m["content"] for m in messages].count(_CLEARED_PLACEHOLDER)
        assert cleared_second > cleared_first


class TestLayer2Stride:
    @staticmethod
    def _msg(i: int) -> dict:
        return {"role": "assistant", "content": f"M{i} " + "z" * (COLLAPSE_TEXT_MIN + 500)}

    def test_boundary_advances_in_strides(self) -> None:
        messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "q"}]
        messages += [self._msg(i) for i in range(COLLAPSE_PRESERVE_RECENT + 4)]
        state: dict[str, Any] = {}
        _context_collapse(messages, state=state)
        folded_first = sum("collapsed" in str(m["content"]) for m in messages)
        assert folded_first > 0

        # Two turns' worth of messages: within the stride → no rewrite.
        before = [m["content"] for m in messages]
        messages += [self._msg(100), self._msg(101)]
        _context_collapse(messages, state=state)
        assert [m["content"] for m in messages[: len(before)]] == before

        # Past the stride → the boundary jumps and folds the backlog at once.
        messages += [self._msg(200 + i) for i in range(COLLAPSE_STRIDE)]
        _context_collapse(messages, state=state)
        assert sum("collapsed" in str(m["content"]) for m in messages) > folded_first

    def test_stateless_call_folds_up_to_the_recent_window(self) -> None:
        messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "q"}]
        messages += [self._msg(i) for i in range(COLLAPSE_PRESERVE_RECENT + 2)]
        _context_collapse(messages)
        assert "collapsed" in messages[2]["content"]
        assert "collapsed" not in messages[-1]["content"]


class _LLM:
    model_name = "stub"
    sends_reasoning_content = False

    def __init__(self, responses: list[LLMResponse]) -> None:
        self.responses = responses
        self.calls = 0

    def stream_chat(self, messages, tools=None, **_: Any) -> LLMResponse:
        self.calls += 1
        return self.responses[min(self.calls, len(self.responses)) - 1]

    def chat(self, messages, **_: Any) -> LLMResponse:
        raise AssertionError("no summary expected")


@pytest.fixture(autouse=True)
def _tenant_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIBE_DATA_DIR", str(tmp_path / "tenant"))


def _agent(llm: Any, tmp_path: Path) -> tuple[AgentLoop, list]:
    events: list = []
    agent = AgentLoop(registry=ToolRegistry(), llm=llm, max_iterations=3,
                      event_callback=lambda ev, data: events.append((ev, data)))
    run_dir = tmp_path / "run"
    run_dir.mkdir(exist_ok=True)
    agent.memory.run_dir = str(run_dir)
    return agent, events


def test_compact_tool_on_a_small_context_is_answered_not_run(tmp_path: Path) -> None:
    from src.providers.chat import ToolCallRequest

    llm = _LLM([
        LLMResponse(content="", tool_calls=[ToolCallRequest(id="k", name="compact", arguments={})]),
        LLMResponse(content="done"),
    ])
    agent, _ = _agent(llm, tmp_path)

    result = agent.run("q")

    assert result["status"] == "success"
    trace = [json.loads(line) for line in (tmp_path / "run" / "trace.jsonl").read_text().splitlines()]
    skipped = [e for e in trace if e.get("type") == "compact_skipped"]
    assert skipped and skipped[0]["reason"] == "context_small"
    assert not any(e.get("type") == "compact_requested" for e in trace)


def test_window_cap_counts_tools_and_measured_ratio(monkeypatch) -> None:
    agent = AgentLoop(registry=ToolRegistry(), llm=_LLM([]), max_iterations=1)
    assert agent._effective_threshold() == loop_mod.TOKEN_THRESHOLD

    monkeypatch.setattr(loop_mod, "CONTEXT_WINDOW_TOKENS", 30_000)
    agent._tools_tokens = 5_000
    agent._token_ratio = 1.5
    assert agent._effective_threshold() == int(30_000 * 0.8 / 1.5) - 5_000

    monkeypatch.setattr(loop_mod, "CONTEXT_WINDOW_TOKENS", 1_000_000)
    assert agent._effective_threshold() == loop_mod.TOKEN_THRESHOLD


def test_token_ratio_is_measured_and_reported(tmp_path: Path) -> None:
    class _UsageLLM(_LLM):
        def stream_chat(self, messages, tools=None, **_: Any) -> LLMResponse:
            est = estimate_tokens(messages)
            return LLMResponse(content="done", usage_metadata={
                "input_tokens": est * 2, "output_tokens": 1, "total_tokens": est * 2 + 1,
            })

    agent, events = _agent(_UsageLLM([]), tmp_path)
    agent.run("分析" * 200)

    stats = [d for ev, d in events if ev == "attempt_stats"][-1]
    assert 1.5 <= stats["token_estimate_ratio"] <= 2.1
