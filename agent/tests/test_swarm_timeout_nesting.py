"""V1 regression: the loop's write-tool watchdog must never pre-empt run_swarm.

Batch F gave write tools a hard timeout (warn at 1x, abandon at 2x the
per-call timeout). Its base was pinned to the tenant-wide
``TOOL_TIMEOUT_SECONDS``, so ``run_swarm`` — whose NORMAL runtime is tens of
minutes and whose own wait budget is ``SWARM_TIMEOUT`` — was abandoned at 600s
under the production tenant tier (300s tool timeout). Consequences: the two
hour swarm tier was unreachable, the ``wait_budget_exhausted`` salvage return
(the only thing that carries ``run_id`` back to the model) never executed, the
attempt was marked ``degraded``, and the orphaned run kept burning tokens
nobody billed.

The fix is a per-tool ``timeout_seconds`` declaration; these tests pin the
resulting nesting invariant:

    swarm's own wait  <  loop's watchdog  <=  attempt deadline

``test_long_swarm_returns_wait_budget_exhausted_not_write_tool_timeout`` is the
end-to-end guard: it is the only test in the suite that fails on the original
bug.

NOTE on scaling. The production constants are reproduced at 1:300
(TOOL_TIMEOUT 300 -> 1, SWARM_TIMEOUT 7200 -> 24, reserves 60/90 -> 0.2/0.3,
margin 120 -> 0.4). What is deliberately NOT scaled down is the attempt
deadline: in production the two expiries are separated by only the reserve
difference (90s - 60s = 30s out of ~7000s, i.e. 0.4%), and at 1:300 that
becomes a 0.1s race that would make this test flaky on a loaded CI box. The
end-to-end test therefore runs with a deadline too generous to bind — proving
the tool's own budget wins — and the deadline-bound half of the invariant is
asserted separately, at full production numbers, by the pure-function
``test_tool_timeout_nesting_invariant`` below (which needs no threads and no
wall-clock at all).
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import src.tools.swarm_tool as swarm_tool
from src.agent import loop as loop_mod
from src.agent.loop import AgentLoop
from src.core import budget
from src.swarm.models import RunStatus, SwarmAgentSpec, SwarmRun, SwarmTask

# Production values, divided by 300. Keep this table in sync with the
# constants it mirrors — it is the whole point of the test.
SCALE = 300.0
SCALED_TOOL_TIMEOUT_S = 300.0 / SCALE       # 1.0
SCALED_SWARM_TIMEOUT_S = 7200.0 / SCALE     # 24.0
SCALED_LOOP_RESERVE_S = 60.0 / SCALE        # 0.2
SCALED_WAIT_RESERVE_S = 90.0 / SCALE        # 0.3
SCALED_WAIT_FLOOR_S = 60.0 / SCALE          # 0.2
SCALED_WATCHDOG_MARGIN_S = 120.0 / SCALE    # 0.4
SCALED_POLL_INTERVAL_S = 5.0 / SCALE        # ~0.0167


def _running_run(run_id: str = "swarm-nesting-test") -> SwarmRun:
    """A run that never leaves ``running`` — the "legitimately slow" case."""
    return SwarmRun(
        id=run_id,
        preset_name="risk_committee",
        status=RunStatus.running,
        created_at=datetime.now(timezone.utc).isoformat(),
        agents=[
            SwarmAgentSpec(id="risk_officer", role="Risk Officer", system_prompt="x")
        ],
        tasks=[SwarmTask(id="t1", agent_id="risk_officer", prompt_template="do x")],
    )


@pytest.fixture
def _unbound_deadline():
    """Bind/restore the attempt deadline contextvar around a test."""
    previous = budget.get_deadline()
    yield
    budget.bind_deadline(previous)


def _install_scaled_swarm(monkeypatch, run: SwarmRun) -> None:
    """Patch the swarm constants to 1:300 and stub the store/runtime."""
    monkeypatch.setattr(swarm_tool, "_MAX_WAIT_SECONDS", SCALED_SWARM_TIMEOUT_S)
    monkeypatch.setattr(swarm_tool, "_WAIT_RESERVE_S", SCALED_WAIT_RESERVE_S)
    monkeypatch.setattr(swarm_tool, "_WAIT_FLOOR_S", SCALED_WAIT_FLOOR_S)
    monkeypatch.setattr(
        swarm_tool, "_LOOP_WATCHDOG_MARGIN_S", SCALED_WATCHDOG_MARGIN_S
    )
    monkeypatch.setattr(swarm_tool, "_POLL_INTERVAL_SECONDS", SCALED_POLL_INTERVAL_S)

    class _Store:
        def __init__(self, base_dir):
            self.base_dir = base_dir

        def load_run(self, run_id):
            return run if run_id == run.id else None

        def reconcile_run(self, loaded, write=False):
            return loaded

        def run_dir(self, run_id):
            # V2: _format_result asks the store for the run directory so the
            # task previews can point at artifacts/<agent>/report.md.
            return Path(self.base_dir) / run_id

    class _Runtime:
        def __init__(self, store, max_workers=4, agent_config=None):
            self._store = store

        def start_run(self, preset, variables, live_callback=None, include_shell_tools=False):
            return run

    monkeypatch.setattr("src.config.load_swarm_agent_config", lambda: None)
    monkeypatch.setattr("src.swarm.store.SwarmStore", _Store)
    monkeypatch.setattr("src.swarm.runtime.SwarmRuntime", _Runtime)


class _SwarmRegistry:
    """Registry exposing the real SwarmTool, as the loop would see it."""

    def __init__(self, tool: swarm_tool.SwarmTool) -> None:
        self._tool = tool

    def get(self, name: str) -> Any:
        return self._tool if name == "run_swarm" else None

    def execute(self, name: str, args: dict[str, Any]) -> str:
        return self._tool.execute(**args)


def test_long_swarm_returns_wait_budget_exhausted_not_write_tool_timeout(
    monkeypatch, _unbound_deadline
) -> None:
    """A legitimately long swarm must self-close, not be abandoned by the loop.

    Before the fix this returned ``write_tool_timeout`` at 2x the (tiny) tool
    timeout with no run_id and ``degraded`` set. After it, the tool's own wait
    expires first and returns the salvage envelope.
    """
    run = _running_run()
    _install_scaled_swarm(monkeypatch, run)
    # The tenant-tier tool timeout, scaled. Far below the swarm's own budget —
    # exactly the production mismatch.
    monkeypatch.setattr(loop_mod, "TOOL_TIMEOUT_SECONDS", SCALED_TOOL_TIMEOUT_S)
    monkeypatch.setattr(loop_mod, "_TOOL_CAP_RESERVE_S", SCALED_LOOP_RESERVE_S)
    monkeypatch.setattr(loop_mod, "HEARTBEAT_INTERVAL_S", 0.5)
    # Generous, non-binding deadline (see the module docstring).
    budget.bind_deadline(time.monotonic() + 600.0)

    events: list[tuple[str, dict[str, Any]]] = []
    agent = AgentLoop(
        registry=_SwarmRegistry(swarm_tool.SwarmTool()),  # type: ignore[arg-type]
        llm=SimpleNamespace(),
        event_callback=lambda event_type, data: events.append((event_type, data)),
        max_iterations=1,
    )

    t0 = time.perf_counter()
    result, _ = agent._invoke_tool(
        "run_swarm", {"prompt": "评估 A 股当前尾部风险", "preset_name": "risk_committee"}
    )
    elapsed = time.perf_counter() - t0

    payload = json.loads(result)
    assert payload.get("run_id") == run.id, "run_id lost — the salvage path did not run"
    assert payload.get("wait_budget_exhausted") is True
    assert payload.get("error_code") != "write_tool_timeout"
    assert payload.get("status") == "running"
    assert agent._stats.get("degraded") is not True
    # The model must be told it can keep waiting on the same run.
    assert run.id in payload.get("next_step", "")
    # It waited for its OWN budget, not 2x the tool timeout.
    assert elapsed > 2 * SCALED_TOOL_TIMEOUT_S
    assert not any(
        data.get("stage") in ("timeout", "timeout_warning") for _, data in events
    )


def test_swarm_declares_a_timeout_above_the_tenant_tool_timeout(monkeypatch) -> None:
    """``run_swarm`` must declare a watchdog bound outside its own wait budget."""
    monkeypatch.setattr(swarm_tool, "_MAX_WAIT_SECONDS", 7200)
    tool = swarm_tool.SwarmTool()
    assert tool.timeout_seconds == 7200 + swarm_tool._LOOP_WATCHDOG_MARGIN_S

    # Read at call time, so SWARM_TIMEOUT changes take effect without a restart.
    monkeypatch.setattr(swarm_tool, "_MAX_WAIT_SECONDS", 60)
    assert tool.timeout_seconds == 60 + swarm_tool._LOOP_WATCHDOG_MARGIN_S


def test_tool_timeout_nesting_invariant(_unbound_deadline) -> None:
    """At any remaining budget, the swarm's wait is strictly shorter than the loop's.

    This is the deadline-bound half of the invariant, asserted at real
    production numbers (no scaling, no threads). ``remaining=150`` is the
    interesting one: both floors engage there (swarm max(60, 60)=60 vs loop
    max(10, 90)=90), so the assertion pins the floor values too.
    """
    declared = 7200.0 + 120.0  # SwarmTool.timeout_seconds at production values
    for remaining in (150, 300, 900, 3600, 7200, 20000):
        budget.bind_deadline(time.monotonic() + remaining)
        loop_wait = budget.cap_timeout(declared, reserve_s=60.0, floor_s=10.0)
        swarm_wait = budget.cap_timeout(7200.0, reserve_s=90.0, floor_s=60.0)
        assert swarm_wait < loop_wait, (
            f"loop watchdog would fire before the swarm self-closes at "
            f"remaining={remaining}s (swarm={swarm_wait}, loop={loop_wait})"
        )


# --- V1: run_id resume -----------------------------------------------------


def test_run_swarm_accepts_a_run_id_to_resume_an_existing_run(monkeypatch) -> None:
    """``wait_budget_exhausted`` told the model to re-invoke with the run_id...

    ...but ``parameters`` had no such field, so the only executable option was
    starting a whole new run. Resuming must reuse the run and start nothing.
    """
    run = _running_run("swarm-resume-me")
    started: list[str] = []

    class _Store:
        def __init__(self, base_dir):
            self.base_dir = base_dir

        def load_run(self, run_id):
            return run if run_id == run.id else None

        def reconcile_run(self, loaded, write=False):
            return loaded

        def run_dir(self, run_id):
            # V2: _format_result asks the store for the run directory so the
            # task previews can point at artifacts/<agent>/report.md.
            return Path(self.base_dir) / run_id

    class _Runtime:
        def __init__(self, store, max_workers=4, agent_config=None):
            pass

        def start_run(self, preset, variables, live_callback=None, include_shell_tools=False):
            started.append(preset)
            return run

    monkeypatch.setattr(swarm_tool, "_MAX_WAIT_SECONDS", 0)
    monkeypatch.setattr(swarm_tool, "_POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr("src.config.load_swarm_agent_config", lambda: None)
    monkeypatch.setattr("src.swarm.store.SwarmStore", _Store)
    monkeypatch.setattr("src.swarm.runtime.SwarmRuntime", _Runtime)

    payload = json.loads(swarm_tool.SwarmTool().execute(run_id=run.id))

    assert started == [], "resume must not start a second swarm run"
    assert payload["run_id"] == run.id
    assert payload["resumed"] is True
    assert payload["preset"] == "risk_committee"


def test_resuming_an_unknown_run_id_is_a_structured_error(monkeypatch) -> None:
    """An unknown run_id must not silently start a fresh run."""

    class _Store:
        def __init__(self, base_dir):
            self.base_dir = base_dir

        def load_run(self, run_id):
            return None

    monkeypatch.setattr("src.swarm.store.SwarmStore", _Store)

    payload = json.loads(swarm_tool.SwarmTool().execute(run_id="swarm-nope"))

    assert payload["status"] == "error"
    assert payload["error_code"] == "swarm_run_not_found"


def test_run_swarm_without_prompt_or_run_id_explains_both_options() -> None:
    payload = json.loads(swarm_tool.SwarmTool().execute())

    assert payload["status"] == "error"
    assert "run_id" in payload["error"]
