"""A swarm worker ends with its deliverable instead of hitting the context wall.

* Past ``_WRAP_UP_TOKEN_ESTIMATE`` the worker gets one wrap-up nudge and only
  ``write_file`` / ``edit_file`` still run; other calls get a structured
  ``context_budget_reached`` error.
* At the hard ``_MAX_TOKEN_ESTIMATE`` a worker whose ``report.md`` already
  meets the output contract completes; without it the status stays
  ``token_limit``.
* Under the hosted (tenant-safe) profile the Execution Rules stop sending
  roles to yfinance / OKX download scripts the sandbox cannot run — no tool is
  added to any role.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

import src.swarm.worker as worker_mod
from src.providers.chat import LLMResponse, ToolCallRequest
from src.swarm.models import SwarmAgentSpec, SwarmTask
from src.swarm.worker import build_worker_prompt, run_worker

REPORT = (
    "# 报告\n\n收盘价 1712.5 元（来自 get_market_data），20 日均线 1690.2，"
    "结论：维持持有，止损位 1650。风险：成交量萎缩。"
)


class _Tools:
    def __init__(self) -> None:
        self.executed: list[str] = []

    def get_definitions(self):
        return []

    def get(self, name):
        return None

    def execute(self, name, args):
        self.executed.append(name)
        if name == "write_file":
            Path(args["run_dir"], args["path"]).write_text(args["content"], encoding="utf-8")
        return json.dumps({"status": "ok"})


class _LLM:
    def __init__(self, script: list[LLMResponse]) -> None:
        self.script = script
        self.seen: list[list[dict]] = []

    def stream_chat(self, messages, tools=None, on_text_chunk=None, timeout=None,
                    should_cancel=None, tool_choice=None) -> LLMResponse:
        self.seen.append([dict(m) for m in messages])
        return self.script[min(len(self.seen), len(self.script)) - 1]


def _run(tmp_path: Path, llm: _LLM, tools: _Tools, events: list) -> Any:
    spec = SwarmAgentSpec(id="a", role="Analyst", system_prompt="Analyse {target}.",
                          tools=["get_market_data", "write_file"], skills=[],
                          max_iterations=6, timeout_seconds=600)
    task = SwarmTask(id="t", agent_id="a", prompt_template="Analyse 600519.SH.")
    with (
        patch.object(worker_mod, "build_swarm_registry", lambda *a, **k: tools),
        patch.object(worker_mod, "ChatLLM", lambda *a, **k: llm),
    ):
        return run_worker(agent_spec=spec, task=task, upstream_summaries={}, user_vars={},
                          run_dir=tmp_path, event_callback=events.append)


def _call(name: str, **args: Any) -> LLMResponse:
    return LLMResponse(content="", tool_calls=[ToolCallRequest(id=f"id-{name}", name=name, arguments=args)])


def test_wrap_up_keeps_only_report_tools(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(worker_mod, "_WRAP_UP_TOKEN_ESTIMATE", 1)
    tools = _Tools()
    llm = _LLM([
        _call("get_market_data", codes="600519.SH"),
        _call("write_file", path="report.md", content=REPORT),
        LLMResponse(content="报告已写入：维持持有，止损 1650。"),
    ])
    events: list = []

    result = _run(tmp_path, llm, tools, events)

    assert tools.executed == ["write_file"]
    assert result.status == "completed"
    assert (tmp_path / "artifacts" / "a" / "report.md").read_text(encoding="utf-8") == REPORT
    nudges = [m for m in llm.seen[0] if m.get("content") == worker_mod._CONTEXT_WRAP_UP_NUDGE]
    assert len(nudges) == 1
    refused = [m for m in llm.seen[1] if m.get("role") == "tool"]
    assert json.loads(refused[-1]["content"])["error_code"] == "context_budget_reached"
    assert sum(e.type == "worker_context_wrap_up" for e in events) == 1


def test_hard_limit_after_a_valid_report_completes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(worker_mod, "_MAX_TOKEN_ESTIMATE", 1)
    artifact_dir = tmp_path / "artifacts" / "a"
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "report.md").write_text(REPORT, encoding="utf-8")
    events: list = []

    result = _run(tmp_path, _LLM([LLMResponse(content="x")]), _Tools(), events)

    assert result.status == "completed"
    assert result.summary == REPORT
    assert any(e.type == "worker_token_limit" for e in events)


def test_hard_limit_without_a_report_stays_token_limit(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(worker_mod, "_MAX_TOKEN_ESTIMATE", 1)
    result = _run(tmp_path, _LLM([LLMResponse(content="x")]), _Tools(), [])
    assert result.status == "token_limit"


@pytest.mark.parametrize("hosted", [True, False])
def test_hosted_profile_rewrites_the_data_fetch_rule(monkeypatch, hosted: bool) -> None:
    if hosted:
        monkeypatch.setenv("VIBE_TRADING_TENANT_SAFE", "1")
    else:
        monkeypatch.delenv("VIBE_TRADING_TENANT_SAFE", raising=False)
    spec = SwarmAgentSpec(id="a", role="r", system_prompt="s", tools=["bash"], skills=[])

    prompt = build_worker_prompt(spec, {}, "")

    assert ("do NOT write yfinance / OKX / tushare download scripts" in prompt) is hosted
    assert ("Use the patterns from load_skill (yfinance, OKX API via Python)" in prompt) is not hosted
