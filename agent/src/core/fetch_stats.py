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
