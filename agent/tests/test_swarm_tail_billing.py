"""A swarm run outlives its wait: its tokens are billed exactly once.

``wait_budget_exhausted`` deliberately leaves the run working (a later
attempt may resume it). The tokens it spent so far are billed then; the
remainder used to be either lost (nobody resumed) or double-billed (the
resume emitted the full total again).
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

import src.tools.swarm_tool as swarm_tool
from src.swarm.models import RunStatus, SwarmAgentSpec, SwarmRun, SwarmTask
from src.swarm.store import SwarmStore


def _run(run_id: str) -> SwarmRun:
    return SwarmRun(
        id=run_id,
        preset_name="risk_committee",
        status=RunStatus.running,
        created_at=datetime.now(timezone.utc).isoformat(),
        agents=[SwarmAgentSpec(id="a", role="A", system_prompt="x")],
        tasks=[SwarmTask(id="t1", agent_id="a", prompt_template="do x")],
    )


def _tool(events: list) -> swarm_tool.SwarmTool:
    return swarm_tool.SwarmTool(
        event_callback=lambda et, data: events.append((et, dict(data))), session_id="sess-1"
    )


def _usage(events: list, source: str | None = None) -> list[dict]:
    return [d for et, d in events if et == "llm_usage" and (source is None or d.get("source") == source)]


def _wait(tool, store, run_id, **kw):
    return tool._wait_for_run(
        store=store, run_id=run_id, preset="risk_committee", variables={},
        run_agents=1, run_tasks=1, record=lambda *a, **k: None, **kw,
    )


@pytest.fixture()
def store(tmp_path: Path) -> SwarmStore:
    return SwarmStore(tmp_path / "runs")


def test_exhausted_wait_then_resume_bills_each_token_once(store, monkeypatch) -> None:
    monkeypatch.setattr(swarm_tool, "_POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(swarm_tool, "_TAIL_POLL_SECONDS", 3600)  # keep the meter idle
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    run = _run(run_id)
    run.total_input_tokens, run.total_output_tokens = 1000, 100
    store.create_run(run)
    events: list = []
    tool = _tool(events)

    monkeypatch.setattr(swarm_tool, "_MAX_WAIT_SECONDS", 0)
    _wait(tool, store, run_id)
    assert [(u["input_tokens"], u["output_tokens"]) for u in _usage(events)] == [(1000, 100)]

    run.total_input_tokens, run.total_output_tokens = 5000, 700
    run.status = RunStatus.completed
    store.update_run(run)
    monkeypatch.setattr(swarm_tool, "_MAX_WAIT_SECONDS", 5)
    _wait(tool, store, run_id, resumed=True)

    usage = _usage(events)
    assert [(u["input_tokens"], u["output_tokens"]) for u in usage] == [(1000, 100), (4000, 600)]
    assert sum(u["total_tokens"] for u in usage) == 5700


def test_billing_ledger_survives_the_process(store, monkeypatch) -> None:
    """A resume after an engine restart must not re-bill what was billed."""
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    run = _run(run_id)
    run.total_input_tokens, run.total_output_tokens = 300, 30
    store.create_run(run)
    events: list = []
    _tool(events)._emit_swarm_usage(run_id, run, store=store)
    swarm_tool._BILLED.pop(run_id, None)  # "restart": in-process ledger gone

    run.total_input_tokens = 500
    _tool(events)._emit_swarm_usage(run_id, run, store=store)

    assert [(u["input_tokens"], u["output_tokens"]) for u in _usage(events)] == [(300, 30), (200, 0)]


def test_tail_meter_reports_the_remainder_when_the_run_ends(store, monkeypatch) -> None:
    monkeypatch.setattr(swarm_tool, "_POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(swarm_tool, "_TAIL_POLL_SECONDS", 0.02)
    monkeypatch.setattr(swarm_tool, "_MAX_WAIT_SECONDS", 0)
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    run = _run(run_id)
    run.total_input_tokens, run.total_output_tokens = 1000, 100
    store.create_run(run)
    events: list = []

    _wait(_tool(events), store, run_id)
    run.total_input_tokens, run.total_output_tokens = 9000, 900
    run.status = RunStatus.completed
    store.update_run(run)

    deadline = time.monotonic() + 3
    while not _usage(events, "swarm_tail") and time.monotonic() < deadline:
        time.sleep(0.02)

    tail = _usage(events, "swarm_tail")
    assert [(u["input_tokens"], u["output_tokens"]) for u in tail] == [(8000, 800)]
    assert tail[0]["run_id"] == run_id
    assert tail[0]["tail_key"] == f"{run_id}:9000:900"


def test_tail_meter_stops_when_the_run_is_deleted(store, monkeypatch) -> None:
    import shutil

    monkeypatch.setattr(swarm_tool, "_POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(swarm_tool, "_TAIL_POLL_SECONDS", 0.02)
    monkeypatch.setattr(swarm_tool, "_MAX_WAIT_SECONDS", 0)
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    store.create_run(_run(run_id))
    events: list = []

    _wait(_tool(events), store, run_id)
    assert run_id in swarm_tool._TAIL_METERS
    shutil.rmtree(store.run_dir(run_id))

    deadline = time.monotonic() + 3
    while run_id in swarm_tool._TAIL_METERS and time.monotonic() < deadline:
        time.sleep(0.02)
    assert run_id not in swarm_tool._TAIL_METERS
    assert _usage(events, "swarm_tail") == []
