"""Success means an answer, and writing bookkeeping never costs the answer.

* A backtest's ``metrics.csv`` is an artifact, not an answer: a run that left
  one but produced no text fails with its real reason.
* On a full / read-only disk the answer's trace entries, the end event and
  the state file are best effort — an answer that exists is returned.
* Layer 3 skips (degrades) when its pre-compaction transcript cannot be
  written, instead of failing the attempt.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import src.agent.loop as loop_mod
from src.agent.loop import AgentLoop
from src.agent.tools import BaseTool, ToolRegistry
from src.providers.chat import LLMResponse, ToolCallRequest


@pytest.fixture(autouse=True)
def _tenant_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIBE_DATA_DIR", str(tmp_path / "tenant"))


class _Backtest(BaseTool):
    name = "backtest"
    description = "b"
    is_readonly = False
    parameters = {"type": "object", "properties": {}}

    def execute(self, **kwargs: Any) -> str:
        artifacts = Path(kwargs["run_dir"]) / "artifacts"
        artifacts.mkdir(parents=True, exist_ok=True)
        (artifacts / "metrics.csv").write_text("total_return\n0.1\n", encoding="utf-8")
        return json.dumps({"status": "ok"})


class _LLM:
    model_name = "stub"
    sends_reasoning_content = False

    def __init__(self, script: list[LLMResponse]) -> None:
        self.script = script
        self.calls = 0

    def stream_chat(self, messages, tools=None, **_: Any) -> LLMResponse:
        self.calls += 1
        return self.script[min(self.calls, len(self.script)) - 1]


def _agent(llm: Any, tmp_path: Path, registry: ToolRegistry | None = None) -> AgentLoop:
    agent = AgentLoop(registry=registry or ToolRegistry(), llm=llm, max_iterations=4)
    run_dir = tmp_path / "run"
    run_dir.mkdir(exist_ok=True)
    agent.memory.run_dir = str(run_dir)
    return agent


def test_metrics_csv_without_an_answer_is_not_success(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register(_Backtest())
    llm = _LLM([
        LLMResponse(content="", tool_calls=[ToolCallRequest(id="b", name="backtest", arguments={})]),
        LLMResponse(content=""),
    ])

    result = _agent(llm, tmp_path, registry).run("回测")

    assert (tmp_path / "run" / "artifacts" / "metrics.csv").exists()
    assert result["status"] == "failed"
    assert result["reason"].startswith("empty_model_response")


def test_answer_survives_failing_trace_writes(tmp_path: Path, monkeypatch) -> None:
    class _BrokenTrace(loop_mod.TraceWriter):
        def write_text_entry(self, entry, **kwargs):
            if entry.get("type") in ("answer", "message") and entry.get("role") != "user":
                raise OSError(28, "No space left on device")
            return super().write_text_entry(entry, **kwargs)

        def write(self, entry):
            if entry.get("type") == "end":
                raise OSError(28, "No space left on device")
            return super().write(entry)

    monkeypatch.setattr(loop_mod, "TraceWriter", _BrokenTrace)

    result = _agent(_LLM([LLMResponse(content="答案")]), tmp_path).run("q")

    assert result["status"] == "success"
    assert result["content"] == "答案"


def test_compaction_degrades_when_the_transcript_cannot_be_written(tmp_path: Path) -> None:
    class _Trace:
        dir_path = tmp_path / "missing" / "dir"
        events: list = []

        def write(self, event):
            self.events.append(event)

    messages = [{"role": "system", "content": "s"}] + [
        {"role": "user", "content": "x" * 50_000} for _ in range(4)
    ]
    before = [dict(m) for m in messages]
    agent = _agent(_LLM([]), tmp_path)
    trace = _Trace()

    agent._auto_compact(messages, tmp_path, trace, iteration=2)

    assert messages == before
    assert agent._stats["compact_failures"] == 1
    assert trace.events[-1]["type"] == "compact_failed"


def test_whitespace_only_reply_is_not_an_answer(tmp_path: Path) -> None:
    """Loop and SessionService agree: blank text is no answer (not ``ok``)."""
    llm = _LLM([LLMResponse(content="  \n\n  ")])

    result = _agent(llm, tmp_path).run("q")

    # The nudge retry ran, then the run failed with the real reason.
    assert llm.calls == 2
    assert result["status"] == "failed"
    assert result["reason"].startswith("empty_model_response")
    assert result["content"] == ""


def test_length_continuation_survives_failing_trace_writes(tmp_path: Path, monkeypatch) -> None:
    class _BrokenTrace(loop_mod.TraceWriter):
        def write_text_entry(self, entry, **kwargs):
            if entry.get("role") == "assistant":
                raise OSError(28, "No space left on device")
            return super().write_text_entry(entry, **kwargs)

        def write(self, entry):
            if entry.get("type") in ("output_truncated", "output_truncated_continue"):
                raise OSError(28, "No space left on device")
            return super().write(entry)

    monkeypatch.setattr(loop_mod, "TraceWriter", _BrokenTrace)
    llm = _LLM([
        LLMResponse(content="第一段", finish_reason="length"),
        LLMResponse(content="第二段"),
    ])

    result = _agent(llm, tmp_path).run("q")

    assert result["status"] == "success"
    assert result["content"] == "第一段第二段"
