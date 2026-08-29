"""V1: the other tools that declare ``timeout_seconds``, and their own budgets.

A declaration on its own only moves an unbounded wait from the loop into the
tool. Every tool that declares one must therefore also enforce a budget of its
own — these tests pin both halves for ``alpha_bench`` and ``MCPRemoteTool``,
plus the schema bound that keeps the MCP declaration finite.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import src.tools.alpha_bench_tool as bench_mod
from src.config.schema import MCPServerConfig


# --- alpha_bench -----------------------------------------------------------


def _row(alpha_id: str) -> dict:
    """A bench result row with every field the HTML report renders."""
    return {
        "id": alpha_id,
        "ic_mean": 0.1,
        "ic_std": 0.2,
        "ir": 0.5,
        "ic_positive_ratio": 0.55,
        "ic_count": 250,
        "formula_latex": "",
        "category": "test",
    }


def test_alpha_bench_declares_a_timeout_covering_its_own_budget() -> None:
    tool = bench_mod.AlphaBenchTool()
    assert tool.timeout_seconds == (
        bench_mod._BENCH_BUDGET_S + bench_mod._BENCH_LOOP_WATCHDOG_MARGIN_S
    )
    # Strictly outside its own budget, so the loop's watchdog only ever fires
    # on a real hang rather than on a legitimately slow cold-cache bench.
    assert tool.timeout_seconds > bench_mod._BENCH_BUDGET_S


def test_alpha_bench_stops_starting_alphas_once_its_budget_is_spent(
    monkeypatch, tmp_path
) -> None:
    """The declaration is only safe because the bench self-limits (V1).

    With a zero budget it must still write a report and return the partial
    table, flagged, rather than running the whole zoo to completion.
    """
    monkeypatch.setattr(bench_mod, "_BENCH_BUDGET_S", 0.0)
    monkeypatch.setattr(bench_mod, "_BENCH_RESERVE_S", 0.0)
    monkeypatch.setattr(bench_mod, "_BENCH_FLOOR_S", 0.0)

    benched: list[str] = []

    def _fake_bench_one(registry, aid, panel, return_df):
        benched.append(aid)
        return _row(aid)

    monkeypatch.setattr(bench_mod, "_bench_one_alpha", _fake_bench_one)
    monkeypatch.setattr(bench_mod, "_load_universe_panel", lambda u, p: {"close": None})
    monkeypatch.setattr(bench_mod, "_compute_forward_returns", lambda panel: None)
    monkeypatch.setattr(
        bench_mod, "_select_alpha_ids", lambda registry, alpha_id, zoo: ["a1", "a2", "a3"]
    )
    monkeypatch.setattr(
        "src.factors.registry.get_default_registry", lambda: SimpleNamespace()
    )

    envelope = bench_mod.run_alpha_bench(
        universe="csi300", period="2020-2021", output_dir=str(tmp_path)
    )

    assert benched == [], "budget was spent — no alpha should have been started"
    assert envelope["status"] == "ok"
    assert envelope["budget_exhausted"] is True
    assert envelope["n_not_run"] == 3
    assert "PARTIAL" in envelope["message"]


def test_alpha_bench_reports_nothing_extra_when_it_finishes_in_budget(
    monkeypatch, tmp_path
) -> None:
    """A bench that completes must not carry the partial-result flags."""
    monkeypatch.setattr(bench_mod, "_BENCH_BUDGET_S", 600.0)
    monkeypatch.setattr(
        bench_mod,
        "_bench_one_alpha",
        lambda registry, aid, panel, return_df: _row(aid),
    )
    monkeypatch.setattr(bench_mod, "_load_universe_panel", lambda u, p: {"close": None})
    monkeypatch.setattr(bench_mod, "_compute_forward_returns", lambda panel: None)
    monkeypatch.setattr(
        bench_mod, "_select_alpha_ids", lambda registry, alpha_id, zoo: ["a1", "a2"]
    )
    monkeypatch.setattr(
        "src.factors.registry.get_default_registry", lambda: SimpleNamespace()
    )

    envelope = bench_mod.run_alpha_bench(
        universe="csi300", period="2020-2021", output_dir=str(tmp_path)
    )

    assert envelope["status"] == "ok"
    assert envelope["n_alphas_tested"] == 2
    assert "budget_exhausted" not in envelope


# --- MCP -------------------------------------------------------------------


def test_mcp_tool_timeout_is_bounded_above() -> None:
    """``tool_timeout`` had ``ge=0.1`` and no ceiling, so the declaration it
    feeds could be arbitrarily large (V1)."""
    assert MCPServerConfig(command="x", tool_timeout=1800.0).tool_timeout == 1800.0
    with pytest.raises(Exception):
        MCPServerConfig(command="x", tool_timeout=1801.0)
    with pytest.raises(Exception):
        MCPServerConfig(command="x", tool_timeout=0.0)


def test_mcp_remote_tool_declares_connect_plus_call_allowance() -> None:
    from src.tools.mcp import MCPRemoteTool

    adapter = SimpleNamespace(
        server_config=MCPServerConfig(command="x", tool_timeout=120.0)
    )
    spec = SimpleNamespace(
        local_name="remote_thing", description="d", parameters={}, remote_name="thing"
    )
    tool = MCPRemoteTool(adapter, spec)  # type: ignore[arg-type]

    # tool_timeout + init_timeout(max(tool_timeout, 30)) + margin
    assert tool.timeout_seconds == 120.0 + 120.0 + 30.0
    # A default-configured server stays modest.
    adapter.server_config = MCPServerConfig(command="x")
    assert MCPRemoteTool(adapter, spec).timeout_seconds == 30.0 + 30.0 + 30.0  # type: ignore[arg-type]


def test_registry_not_found_error_lists_available_tools() -> None:
    """A bare "not found" left the model guessing from the same wrong memory."""
    from src.agent.tools import BaseTool, ToolRegistry

    class _Echo(BaseTool):
        name = "echo"
        description = "d"

        def execute(self, **kwargs):
            return "{}"

    registry = ToolRegistry()
    registry.register(_Echo())

    payload = json.loads(registry.execute("ecko", {}))

    assert payload["status"] == "error"
    assert "echo" in payload["error"]
    assert payload["available_tools"] == ["echo"]
