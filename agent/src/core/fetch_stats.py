"""Per-attempt market-data fetch accounting (feeds ``attempt_stats``).

The AgentLoop starts a collector at the top of each run and binds it in a
contextvar; tool worker threads inherit the SAME collector object through
``contextvars.copy_context()``, so appends from any thread land in one place
(guarded by a lock). ``market_data.fetch_market_data`` records one entry per
loader call plus one gap entry per symbol that no source could serve — the
"silent data loss" the observability plan exists to surface.

Module-level helpers are no-ops when no collector is bound (CLI, tests,
backtests outside an attempt), so loaders never need to care about context.
"""

from __future__ import annotations

import contextvars
import threading
from typing import Any, Optional


class FetchStatsCollector:
    """Thread-safe accumulator for one attempt's data-source / skill / swarm activity."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # source -> {"ok": n, "failed": n, "ms": total, "fallback_used": n}
        self._by_source: dict[str, dict[str, int]] = {}
        self._gaps: list[dict[str, Any]] = []
        # skill name -> {"calls": n, "ms": total, "errors": n}
        self._skills: dict[str, dict[str, int]] = {}
        self._swarm_runs: list[dict[str, Any]] = []
        # background_run launches: work happens on detached threads, outside
        # tool_ms and the budget clamp — record starts so attempt_stats can at
        # least surface count + final status (resolved at snapshot time).
        self._background: list[dict[str, Any]] = []
        # LLM stream health: in-place retry counts (main loop / swarm workers)
        # plus swarm stream attempts, so a retry RATE is computable —
        # main-loop attempts are already counted by AgentLoop._stats.
        self._stream: dict[str, int] = {
            "main": 0,
            "swarm": 0,
            "swarm_llm_calls": 0,
        }

    def record_fetch(
        self,
        source: str,
        *,
        ok: int = 0,
        failed: int = 0,
        ms: int = 0,
        fallback: bool = False,
    ) -> None:
        with self._lock:
            agg = self._by_source.setdefault(
                source, {"ok": 0, "failed": 0, "ms": 0, "fallback_used": 0}
            )
            agg["ok"] += max(0, int(ok))
            agg["failed"] += max(0, int(failed))
            agg["ms"] += max(0, int(ms))
            if fallback:
                agg["fallback_used"] += 1

    def record_gap(self, symbol: str, reason: str, sources_tried: list[str]) -> None:
        with self._lock:
            if len(self._gaps) < 100:  # bound the payload for pathological runs
                self._gaps.append(
                    {
                        "symbol": str(symbol)[:40],
                        "reason": str(reason)[:120],
                        "sources_tried": [str(s)[:20] for s in sources_tried[:8]],
                    }
                )

    def record_skill(self, name: str, *, ms: int = 0, ok: bool = True) -> None:
        with self._lock:
            agg = self._skills.setdefault(
                str(name)[:60], {"calls": 0, "ms": 0, "errors": 0}
            )
            agg["calls"] += 1
            agg["ms"] += max(0, int(ms))
            if not ok:
                agg["errors"] += 1

    def record_swarm(
        self,
        preset: str,
        run_id: Optional[str],
        status: str,
        ms: int,
        agents: Optional[int] = None,
        tasks: Optional[int] = None,
        llm_ms: Optional[int] = None,
        tool_ms: Optional[int] = None,
        input_tokens: Optional[int] = None,
        output_tokens: Optional[int] = None,
        resumed: bool = False,
    ) -> None:
        with self._lock:
            if len(self._swarm_runs) < 20:  # bound for pathological loops
                entry: dict[str, Any] = {
                    "preset": str(preset)[:60],
                    "run_id": (str(run_id)[:40] if run_id else None),
                    "status": str(status)[:40],
                    "ms": max(0, int(ms)),
                }
                if agents is not None:
                    entry["agents"] = int(agents)
                if tasks is not None:
                    entry["tasks"] = int(tasks)
                # Cumulative worker effort split (parallel layers can make
                # these exceed the run's wall-clock ms above).
                if llm_ms is not None:
                    entry["llm_ms"] = max(0, int(llm_ms))
                if tool_ms is not None:
                    entry["tool_ms"] = max(0, int(tool_ms))
                if input_tokens is not None:
                    entry["input_tokens"] = max(0, int(input_tokens))
                if output_tokens is not None:
                    entry["output_tokens"] = max(0, int(output_tokens))
                if resumed:
                    # V1: a run_id resume waits on an ALREADY-RUNNING run —
                    # without this flag the ops tab would read two entries for
                    # one run as two separate swarms.
                    entry["resumed"] = True
                self._swarm_runs.append(entry)

    def snapshot(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        with self._lock:
            fetches = [
                {"source": source, **agg}
                for source, agg in sorted(
                    self._by_source.items(), key=lambda kv: -kv[1]["ms"]
                )
            ]
            return fetches, list(self._gaps)

    def snapshot_skills(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {"name": name, **agg}
                for name, agg in sorted(
                    self._skills.items(), key=lambda kv: -kv[1]["ms"]
                )
            ]

    def snapshot_swarm(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._swarm_runs)

    def record_stream_retry(self, source: str) -> None:
        with self._lock:
            if source in ("main", "swarm"):
                self._stream[source] += 1

    def record_swarm_llm_call(self) -> None:
        with self._lock:
            self._stream["swarm_llm_calls"] += 1

    def snapshot_stream(self) -> Optional[dict[str, int]]:
        """Return stream-retry counters, or None when nothing to report."""
        with self._lock:
            if not any(self._stream.values()):
                return None
            return dict(self._stream)

    def record_background(self, task_id: str, command: str) -> None:
        with self._lock:
            if len(self._background) < 20:
                self._background.append(
                    {"task_id": str(task_id)[:20], "command": str(command)[:120]}
                )

    def snapshot_background(self) -> list[dict[str, Any]]:
        """Return recorded background launches with their CURRENT status,
        resolved from the BackgroundManager singleton at snapshot time — a
        task still running when the attempt ends shows as "running"."""
        with self._lock:
            entries = [dict(e) for e in self._background]
        if not entries:
            return entries
        try:
            from src.tools.background_tools import get_background_manager

            tasks = get_background_manager().tasks
            for e in entries:
                t = tasks.get(e["task_id"])
                e["status"] = (t or {}).get("status", "unknown")
        except Exception:  # noqa: BLE001 - stats must never break the run
            pass
        return entries


_COLLECTOR: contextvars.ContextVar[Optional[FetchStatsCollector]] = contextvars.ContextVar(
    "vibe_fetch_stats", default=None
)


def start_collect() -> FetchStatsCollector:
    """Create and bind a fresh collector for the current attempt context."""
    collector = FetchStatsCollector()
    _COLLECTOR.set(collector)
    return collector


def current() -> Optional[FetchStatsCollector]:
    return _COLLECTOR.get()


def record_fetch(source: str, **kwargs: Any) -> None:
    collector = _COLLECTOR.get()
    if collector is not None:
        collector.record_fetch(source, **kwargs)


def record_gap(symbol: str, reason: str, sources_tried: list[str]) -> None:
    collector = _COLLECTOR.get()
    if collector is not None:
        collector.record_gap(symbol, reason, sources_tried)


def record_skill(name: str, **kwargs: Any) -> None:
    collector = _COLLECTOR.get()
    if collector is not None:
        collector.record_skill(name, **kwargs)


def record_swarm(preset: str, run_id: Optional[str], status: str, ms: int, **kwargs: Any) -> None:
    collector = _COLLECTOR.get()
    if collector is not None:
        collector.record_swarm(preset, run_id, status, ms, **kwargs)


def record_background(task_id: str, command: str) -> None:
    collector = _COLLECTOR.get()
    if collector is not None:
        collector.record_background(task_id, command)


def record_stream_retry(source: str) -> None:
    collector = _COLLECTOR.get()
    if collector is not None:
        collector.record_stream_retry(source)


def record_swarm_llm_call() -> None:
    collector = _COLLECTOR.get()
    if collector is not None:
        collector.record_swarm_llm_call()
