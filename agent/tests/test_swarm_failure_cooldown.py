"""Tests for the swarm preset failure cooldown (batch F, F3)."""

from __future__ import annotations

import json
from types import SimpleNamespace

from src.tools import swarm_tool as swarm_mod
from src.tools.swarm_tool import SwarmTool


def _failed_run() -> SimpleNamespace:
    return SimpleNamespace(
        id="run-abc123",
        final_report="partial report from completed workers",
        tasks=[
            SimpleNamespace(
                id="t1",
                agent_id="macro_analyst",
                status=SimpleNamespace(value="completed"),
                summary="Macro backdrop: rates likely on hold; USD softening.",
            ),
            SimpleNamespace(
                id="t2",
                agent_id="risk_officer",
                status=SimpleNamespace(value="failed"),
                summary="",
            ),
        ],
    )


def test_rerun_within_cooldown_is_rejected_with_salvage() -> None:
    tool = SwarmTool()
    tool._record_preset_failure("risk_committee", _failed_run())

    result = json.loads(tool.execute(prompt="风险审计", preset_name="risk_committee"))

    assert result["status"] == "rejected"
    assert result["error_code"] == "swarm_preset_cooldown"
    assert result["failed_run_id"] == "run-abc123"
    assert result["retry_after_s"] > 0
    # Only COMPLETED workers' products are offered for salvage.
    tasks = result["salvage"]["completed_tasks"]
    assert [t["agent_id"] for t in tasks] == ["macro_analyst"]
    assert "Macro backdrop" in tasks[0]["summary"]
    assert result["salvage"]["final_report"].startswith("partial report")


def test_different_preset_not_affected() -> None:
    tool = SwarmTool()
    tool._record_preset_failure("risk_committee", _failed_run())

    # A different preset passes the cooldown gate (returns None = no rejection).
    assert tool._cooldown_rejection("macro_strategy_forum") is None


def test_cooldown_expires(monkeypatch) -> None:
    tool = SwarmTool()
    tool._record_preset_failure("risk_committee", _failed_run())
    # Age the failure record past the cooldown window.
    tool._recent_failures["risk_committee"]["ts"] -= (
        swarm_mod._FAILURE_COOLDOWN_SECONDS + 1
    )

    assert tool._cooldown_rejection("risk_committee") is None
    # Expired record is cleaned up.
    assert "risk_committee" not in tool._recent_failures


def test_salvage_payload_is_capped() -> None:
    run = SimpleNamespace(
        id="run-big",
        final_report="R" * 100_000,
        tasks=[
            SimpleNamespace(
                id=f"t{i}",
                agent_id=f"agent{i}",
                status=SimpleNamespace(value="completed"),
                summary="S" * 50_000,
            )
            for i in range(30)
        ],
    )
    tool = SwarmTool()
    tool._record_preset_failure("quant_strategy_desk", run)
    record = tool._recent_failures["quant_strategy_desk"]

    assert len(record["salvage"]["final_report"]) <= swarm_mod._SALVAGE_REPORT_MAX_CHARS
    assert len(record["salvage"]["completed_tasks"]) <= swarm_mod._SALVAGE_MAX_TASKS
    for task in record["salvage"]["completed_tasks"]:
        assert len(task["summary"]) <= swarm_mod._SALVAGE_TASK_MAX_CHARS
