"""V2: the ``run_swarm`` return must be valid JSON inside the tool-result limit.

Pre-V2 the payload was ``final_report`` plus EVERY task's whole report.md. A
multi-worker preset produced tens of KB, and the loop's flat ``[:10_000]``
then cut it mid-document — the model received unparseable JSON with the later
tasks silently absent, on the very tool whose results are on the never-prune
list because re-running it costs tens of minutes.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from src.agent.tool_result_store import TOOL_RESULT_LIMIT
from src.swarm.models import RunStatus, SwarmAgentSpec, SwarmRun, SwarmTask, TaskStatus
from src.swarm.serialization import SUMMARY_PREVIEW_CHARS, serialize_task
from src.tools.swarm_tool import (
    MIN_SUMMARY_PREVIEW_CHARS,
    PAYLOAD_TARGET_CHARS,
    _format_result,
)


def _run(n_tasks: int = 6, report_chars: int = 30_000) -> SwarmRun:
    agents, tasks = [], []
    for i in range(n_tasks):
        agents.append(
            SwarmAgentSpec(id=f"agent{i}", role="Analyst", system_prompt="x")
        )
        task = SwarmTask(id=f"t{i}", agent_id=f"agent{i}", prompt_template="do x")
        task.status = TaskStatus.completed
        task.summary = f"REPORT{i} " + "详细分析内容。" * (report_chars // 7)
        tasks.append(task)
    run = SwarmRun(
        id="r1",
        preset_name="demo",
        created_at=datetime.now(timezone.utc).isoformat(),
        agents=agents,
        tasks=tasks,
    )
    run.status = RunStatus.completed
    run.final_report = "FINAL " + "综合结论。" * 20_000
    return run


class TestSerializeTask:
    def test_full_text_by_default(self) -> None:
        """MCP read paths must keep returning the whole report."""
        task = _run(1).tasks[0]
        payload = serialize_task(task)
        assert payload["summary"] == task.summary
        assert "summary_truncated" not in payload

    def test_preview_flags_the_clip_and_reports_the_real_size(self) -> None:
        task = _run(1).tasks[0]
        payload = serialize_task(
            task,
            summary_preview_chars=SUMMARY_PREVIEW_CHARS,
            report_path="/runs/r1/artifacts/agent0/report.md",
        )
        assert len(payload["summary"]) == SUMMARY_PREVIEW_CHARS
        assert payload["summary_truncated"] is True
        assert payload["summary_chars"] == len(task.summary)
        assert payload["report_path"] == "/runs/r1/artifacts/agent0/report.md"

    def test_short_summary_is_not_flagged(self) -> None:
        task = _run(1).tasks[0]
        task.summary = "brief"
        payload = serialize_task(task, summary_preview_chars=SUMMARY_PREVIEW_CHARS)
        assert payload["summary"] == "brief"
        assert "summary_truncated" not in payload


class TestFormatResultBudget:
    def test_payload_is_valid_json_under_the_tool_result_limit(self) -> None:
        """The core regression: previously ~200KB, cut mid-JSON at 10k."""
        out = _format_result(_run(), "demo", {}, run_dir=Path("/runs/r1"))

        assert len(out) < TOOL_RESULT_LIMIT, len(out)
        # Parses cleanly — the model is not handed a truncated document.
        parsed = json.loads(out)
        assert len(parsed["tasks"]) == 6

    def test_every_task_is_still_present(self) -> None:
        """Truncation used to make later tasks vanish without a trace."""
        parsed = json.loads(_format_result(_run(), "demo", {}, run_dir=Path("/runs/r1")))
        ids = [t["id"] for t in parsed["tasks"]]
        assert ids == [f"t{i}" for i in range(6)]
        for i, task in enumerate(parsed["tasks"]):
            assert task["summary"].startswith(f"REPORT{i}")

    def test_each_clipped_summary_carries_its_artifact_pointer(self) -> None:
        parsed = json.loads(_format_result(_run(), "demo", {}, run_dir=Path("/runs/r1")))
        for i, task in enumerate(parsed["tasks"]):
            assert task["summary_truncated"] is True
            assert task["report_path"] == f"/runs/r1/artifacts/agent{i}/report.md"

    def test_final_report_gets_the_measured_leftover_and_is_flagged(self) -> None:
        """A flat cap cannot hold the payload under the limit; the leftover can."""
        parsed = json.loads(_format_result(_run(), "demo", {}, run_dir=Path("/runs/r1")))
        assert 0 < len(parsed["final_report"]) < PAYLOAD_TARGET_CHARS
        assert parsed["final_report_truncated"] is True
        assert parsed["final_report_path"] == "/runs/r1/artifacts/final_report.md"

    def test_a_large_team_shrinks_previews_to_stay_inside_the_limit(self) -> None:
        """technical_analysis_panel is the widest shipped preset (12 tasks)."""
        out = _format_result(_run(n_tasks=12), "demo", {}, run_dir=Path("/runs/r1"))

        assert len(out) < TOOL_RESULT_LIMIT, len(out)
        parsed = json.loads(out)
        assert len(parsed["tasks"]) == 12
        assert len(parsed["tasks"][0]["summary"]) >= MIN_SUMMARY_PREVIEW_CHARS
        # Every task still carries its pointer, however small the preview.
        assert all(t.get("report_path") for t in parsed["tasks"])

    def test_reading_note_explains_the_previews(self) -> None:
        parsed = json.loads(_format_result(_run(), "demo", {}, run_dir=Path("/runs/r1")))
        assert "PREVIEW" in parsed["reading_note"]
        assert "read_file" in parsed["reading_note"] or "report_path" in parsed["reading_note"]

    def test_short_run_gets_no_reading_note(self) -> None:
        """No noise when nothing was clipped."""
        run = _run(n_tasks=2)
        for task in run.tasks:
            task.summary = "short summary"
        run.final_report = "short final"

        parsed = json.loads(_format_result(run, "demo", {}, run_dir=Path("/runs/r1")))

        assert "reading_note" not in parsed
        assert parsed["final_report_truncated"] is False
        assert parsed["tasks"][0]["summary"] == "short summary"

    def test_missing_run_dir_still_produces_valid_bounded_json(self) -> None:
        out = _format_result(_run(), "demo", {})
        parsed = json.loads(out)
        assert len(out) < TOOL_RESULT_LIMIT
        assert "final_report_path" not in parsed
        assert "report_path" not in parsed["tasks"][0]


class TestUpstreamSummaryBudget:
    def test_long_upstream_report_is_clipped_with_a_pointer(self) -> None:
        """P2-7: a multi-upstream role concatenated whole reports unbudgeted."""
        from src.swarm.runtime import UPSTREAM_SUMMARY_MAX_CHARS, _clip_upstream_summary

        class _TaskStore:
            @staticmethod
            def load_task(_tid: str):
                return type("T", (), {"agent_id": "researcher"})()

        long_report = "HEAD" + "x" * (UPSTREAM_SUMMARY_MAX_CHARS * 6) + "TAIL"
        out = _clip_upstream_summary(
            long_report,
            run_dir=Path("/runs/r1"),
            source_task_id="t0",
            task_store=_TaskStore(),
        )

        assert len(out) < len(long_report)
        assert out.startswith("HEAD")
        assert out.endswith("TAIL")
        assert "/runs/r1/artifacts/researcher/report.md" in out
        assert "PREVIEW" in out

    def test_short_upstream_report_is_untouched(self) -> None:
        from src.swarm.runtime import _clip_upstream_summary

        short = "a concise upstream conclusion"
        assert (
            _clip_upstream_summary(
                short, run_dir=Path("/runs/r1"), source_task_id="t0", task_store=None
            )
            == short
        )

    def test_unresolvable_agent_id_degrades_without_raising(self) -> None:
        from src.swarm.runtime import _clip_upstream_summary

        class _Broken:
            @staticmethod
            def load_task(_tid: str):
                raise FileNotFoundError

        out = _clip_upstream_summary(
            "y" * 50_000,
            run_dir=Path("/runs/r1"),
            source_task_id="t0",
            task_store=_Broken(),
        )
        assert "report path unavailable" in out


class TestIncompleteIsRetryable:
    def test_incomplete_joins_failed_in_the_retry_set(self) -> None:
        """P2-6: a missing deliverable is the failure most worth one more try,
        and it blocks every downstream task when it is not retried."""
        from src.swarm.runtime import _RETRYABLE_WORKER_STATUSES

        assert "incomplete" in _RETRYABLE_WORKER_STATUSES
        assert "failed" in _RETRYABLE_WORKER_STATUSES
        # timeout / token_limit stay excluded — a re-run hits the same wall.
        assert "timeout" not in _RETRYABLE_WORKER_STATUSES
        assert "token_limit" not in _RETRYABLE_WORKER_STATUSES
        assert "completed" not in _RETRYABLE_WORKER_STATUSES
