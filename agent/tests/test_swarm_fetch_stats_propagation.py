"""Regression: swarm threads must inherit the attempt's FetchStatsCollector.

Incident 2026-08-24: a run_swarm attempt's ``attempt_stats.skills`` came back
empty even though workers called ``load_skill`` — ``SwarmRuntime`` spawned the
run thread with a plain ``threading.Thread`` and dispatched workers through a
``ThreadPoolExecutor``, neither of which inherits contextvars, so
``record_skill`` resolved to a no-op collector inside every worker.

The fix wraps both hops in ``contextvars.copy_context()``:
  1. ``start_run`` → run thread (spawn-time copy)
  2. ``_execute_layer`` → executor workers (per-submit copy)

These tests pin each hop independently.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import src.swarm.runtime as rt
from src.core import fetch_stats
from src.swarm.models import SwarmAgentSpec, SwarmRun, SwarmTask, WorkerResult
from src.swarm.store import SwarmStore


def _make_run(tmp_path: Path) -> tuple[SwarmStore, rt.SwarmRuntime, SwarmRun]:
    store = SwarmStore(base_dir=tmp_path)
    runtime = rt.SwarmRuntime(store=store)
    agents = [SwarmAgentSpec(id="a", role="a", system_prompt="x", max_retries=0)]
    tasks = [SwarmTask(id="task-a", agent_id="a", prompt_template="do a")]
    run = SwarmRun(
        id="r-ctx",
        preset_name="demo",
        created_at="2026-08-24T00:00:00+00:00",
        agents=agents,
        tasks=tasks,
    )
    store.create_run(run)
    return store, runtime, run


def test_executor_workers_inherit_collector(tmp_path, monkeypatch):
    """Hop 2: a worker running inside the layer executor must see the
    collector bound in the thread that called _execute_run."""

    def fake_worker(agent_spec, task, **kwargs):
        fetch_stats.record_skill("from-worker", ms=5, ok=True)
        return WorkerResult(status="completed", summary="ok")

    monkeypatch.setattr(rt, "run_worker", fake_worker)

    collector = fetch_stats.start_collect()
    _, runtime, run = _make_run(tmp_path)
    runtime._execute_run(run, threading.Event())

    skills = collector.snapshot_skills()
    assert [s["name"] for s in skills] == ["from-worker"], (
        f"worker record_skill must land in the caller's collector, got {skills}"
    )


def test_start_run_thread_inherits_collector(tmp_path, monkeypatch):
    """Hop 1: the background run thread spawned by start_run must carry the
    caller's context (collector included) across the Thread boundary."""

    done = threading.Event()

    def fake_execute_run(self, run, cancel_event, include_shell_tools=False):
        fetch_stats.record_skill("from-run-thread", ms=1, ok=True)
        done.set()

    monkeypatch.setattr(rt.SwarmRuntime, "_execute_run", fake_execute_run)

    store = SwarmStore(base_dir=tmp_path)
    runtime = rt.SwarmRuntime(store=store)
    agents = [SwarmAgentSpec(id="a", role="a", system_prompt="x", max_retries=0)]
    tasks = [SwarmTask(id="task-a", agent_id="a", prompt_template="do a")]
    run = SwarmRun(
        id="r-thread",
        preset_name="demo",
        created_at="2026-08-24T00:00:00+00:00",
        agents=agents,
        tasks=tasks,
    )
    monkeypatch.setattr(rt, "build_run_from_preset", lambda preset_name, user_vars: run)

    collector = fetch_stats.start_collect()
    runtime.start_run("demo", {})
    assert done.wait(10), "run thread did not execute"
    # record happens right before done.set(); give the locked append a beat
    for _ in range(50):
        if collector.snapshot_skills():
            break
        time.sleep(0.02)

    skills = collector.snapshot_skills()
    assert [s["name"] for s in skills] == ["from-run-thread"], (
        f"run-thread record_skill must land in the caller's collector, got {skills}"
    )
