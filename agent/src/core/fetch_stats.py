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
    """Thread-safe accumulator for one attempt's data-source activity."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # source -> {"ok": n, "failed": n, "ms": total, "fallback_used": n}
        self._by_source: dict[str, dict[str, int]] = {}
        self._gaps: list[dict[str, Any]] = []

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

    def snapshot(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        with self._lock:
            fetches = [
                {"source": source, **agg}
                for source, agg in sorted(
                    self._by_source.items(), key=lambda kv: -kv[1]["ms"]
                )
            ]
            return fetches, list(self._gaps)


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
