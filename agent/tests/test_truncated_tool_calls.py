"""Tool calls cut by the output-token ceiling are never executed.

LangChain's ``parse_partial_json`` completes a tool call whose arguments were
still streaming when ``finish_reason == "length"`` hit, so the call looks
valid. Executed, a half-written ``report.md`` or script lands on disk and the
tool answers ``ok``. The main loop and the swarm worker instead answer every
such call with a structured ``tool_call_truncated`` error, keep the pairing
intact, and count the turn as a length continuation.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

import src.swarm.worker as worker_mod
from src.agent.loop import (
    TRUNCATED_TOOL_CALL_ERROR,
    AgentLoop,
    truncated_tool_call_messages,
)
from src.agent.tools import BaseTool, ToolRegistry
from src.providers.chat import LLMResponse, ToolCallRequest
from src.swarm.models import SwarmAgentSpec, SwarmTask
from src.swarm.worker import run_worker

CUT_REPORT = "# 报告\n第一段（后面被 max_tokens 截断" + "x" * 5000


class _WriteTool(BaseTool):
    name = "write_file"
    description = "Write a file."
    is_readonly = False
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
    }

    def __init__(self) -> None:
        self.written: dict[str, str] = {}

    def execute(self, **kwargs: Any) -> str:
        self.written[kwargs["path"]] = kwargs["content"]
        target = Path(kwargs.get("run_dir") or ".") / kwargs["path"]
        if kwargs.get("run_dir"):
            target.write_text(kwargs["content"], encoding="utf-8")
        return json.dumps({"status": "ok", "path": kwargs["path"]})


class _ScriptLLM:
    model_name = "stub"
    sends_reasoning_content = False

    def __init__(self, script: list[LLMResponse]) -> None:
        self.script = script
        self.seen: list[list[dict]] = []

    def stream_chat(self, messages, tools=None, **_: Any) -> LLMResponse:
        self.seen.append([dict(m) for m in messages])
        return self.script[min(len(self.seen), len(self.script)) - 1]


def _cut_call() -> LLMResponse:
    return LLMResponse(
        content="",
        tool_calls=[ToolCallRequest(id="c1", name="write_file",
                                    arguments={"path": "report.md", "content": CUT_REPORT})],
        finish_reason="length",
    )


def test_helper_pairs_every_call_with_a_structured_error() -> None:
    calls = [
        ToolCallRequest(id="a", name="write_file", arguments={"path": "p", "content": CUT_REPORT}),
        ToolCallRequest(id="b", name="bash", arguments={"command": "echo hi"}),
    ]
    assistant, *results = truncated_tool_call_messages(calls, content="", reasoning_content=None)

    assert [tc["id"] for tc in assistant["tool_calls"]] == ["a", "b"]
    kept = json.loads(assistant["tool_calls"][0]["function"]["arguments"])["content"]
    assert len(kept) < 2500 and "not executed" in kept
    assert json.loads(assistant["tool_calls"][1]["function"]["arguments"]) == {"command": "echo hi"}
    assert [r["tool_call_id"] for r in results] == ["a", "b"]
    for result in results:
        payload = json.loads(result["content"])
        assert payload["status"] == "error"
        assert payload["error_code"] == TRUNCATED_TOOL_CALL_ERROR


def test_loop_refuses_truncated_call_and_continues(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("VIBE_DATA_DIR", str(tmp_path / "tenant"))
    tool = _WriteTool()
    registry = ToolRegistry()
    registry.register(tool)
    llm = _ScriptLLM([_cut_call(), LLMResponse(content="已改为分段写入。", finish_reason="stop")])
    events: list[tuple[str, dict]] = []
    agent = AgentLoop(registry=registry, llm=llm, max_iterations=4,
                      event_callback=lambda ev, data: events.append((ev, data)))
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    agent.memory.run_dir = str(run_dir)

    result = agent.run("写一份报告")

    assert tool.written == {}
    assert result["status"] == "success"
    second_call = llm.seen[1]
    tool_results = [m for m in second_call if m.get("role") == "tool"]
    assert json.loads(tool_results[-1]["content"])["error_code"] == TRUNCATED_TOOL_CALL_ERROR
    trace = [json.loads(line) for line in (run_dir / "trace.jsonl").read_text().splitlines()]
    refused = [e for e in trace if e.get("type") == "tool_calls_truncated"]
    assert refused and refused[0]["tools"] == ["write_file"]
    stats = [d for ev, d in events if ev == "attempt_stats"][-1]
    assert stats["truncated_tool_calls"] == 1
    assert stats["output_truncations"] == 1


def test_refusals_share_the_length_continuation_budget(tmp_path: Path, monkeypatch) -> None:
    import src.agent.loop as loop_mod

    monkeypatch.setenv("VIBE_DATA_DIR", str(tmp_path / "tenant"))
    monkeypatch.setattr(loop_mod, "LENGTH_CONTINUATIONS", 1)
    registry = ToolRegistry()
    registry.register(_WriteTool())
    llm = _ScriptLLM([
        _cut_call(),
        LLMResponse(content="partial answer", finish_reason="length"),
    ])
    agent = AgentLoop(registry=registry, llm=llm, max_iterations=4)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    agent.memory.run_dir = str(run_dir)

    result = agent.run("写")

    # The refusal used the only continuation: the cut text reply is marked,
    # not continued.
    assert result["content"].startswith("partial answer")
    assert "（输出被截断）" in result["content"]
    assert len(llm.seen) == 2


def test_worker_never_writes_a_truncated_report(tmp_path: Path) -> None:
    tool = _WriteTool()

    class _Reg:
        def get_definitions(self):
            return []

        def get(self, name):
            return tool if name == "write_file" else None

        def execute(self, name, args):
            return tool.execute(**args)

    llm = _ScriptLLM([
        _cut_call(),
        LLMResponse(content="Summary: the report is split into smaller writes next time; "
                    "no numbers were produced in this run.", finish_reason="stop"),
    ])
    events: list = []
    spec = SwarmAgentSpec(id="a", role="r", system_prompt="s", tools=["write_file"], skills=[],
                          max_iterations=4, timeout_seconds=600)
    task = SwarmTask(id="t", agent_id="a", prompt_template="Write the report.")
    with (
        patch.object(worker_mod, "build_swarm_registry", lambda *a, **k: _Reg()),
        patch.object(worker_mod, "ChatLLM", lambda *a, **k: llm),
    ):
        run_worker(agent_spec=spec, task=task, upstream_summaries={}, user_vars={},
                   run_dir=tmp_path, event_callback=events.append)

    assert tool.written == {}
    assert not (tmp_path / "artifacts" / "a" / "report.md").exists()
    truncated = [e for e in events if e.type == "worker_output_truncated"]
    assert truncated and truncated[0].data["tool_calls_refused"] == ["write_file"]
    second = llm.seen[1]
    tool_results = [m for m in second if m.get("role") == "tool"]
    assert json.loads(tool_results[-1]["content"])["error_code"] == TRUNCATED_TOOL_CALL_ERROR


@pytest.mark.parametrize("finish_reason", ["stop", "tool_calls"])
def test_complete_tool_calls_still_execute(tmp_path: Path, monkeypatch, finish_reason) -> None:
    monkeypatch.setenv("VIBE_DATA_DIR", str(tmp_path / "tenant"))
    tool = _WriteTool()
    registry = ToolRegistry()
    registry.register(tool)
    whole = LLMResponse(
        content="",
        tool_calls=[ToolCallRequest(id="c1", name="write_file",
                                    arguments={"path": "a.md", "content": "complete"})],
        finish_reason=finish_reason,
    )
    llm = _ScriptLLM([whole, LLMResponse(content="done")])
    agent = AgentLoop(registry=registry, llm=llm, max_iterations=3)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    agent.memory.run_dir = str(run_dir)

    agent.run("w")

    assert tool.written == {"a.md": "complete"}
