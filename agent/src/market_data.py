"""Shared market data helpers for MCP and local agent tools."""

from __future__ import annotations

import json
import logging
import math
import os
import re
import time
from collections.abc import Callable
from typing import Any

from src.core import budget as _budget
from src.core import fetch_stats as _fetch_stats

logger = logging.getLogger(__name__)

DEFAULT_MAX_ROWS = 250

# Total wall-clock budget for one fetch_market_data call INCLUDING the
# fallback chain (further capped by the attempt deadline when one is bound).
FETCH_BUDGET_S = float(os.getenv("VIBE_TRADING_FETCH_BUDGET_S", "120"))

_SOURCE_PATTERNS = [
    (re.compile(r"^\d{6}\.(SZ|SH|BJ)$", re.I), "tushare", "a_share"),
    (re.compile(r"^[A-Z]+\.US$", re.I), "yfinance", "us_equity"),
    (re.compile(r"^\d{3,5}\.HK$", re.I), "yfinance", "hk_equity"),
    (re.compile(r"^[A-Z]+-USDT$", re.I), "okx", "crypto"),
    (re.compile(r"^[A-Z]+/USDT$", re.I), "ccxt", "crypto"),
    # Yahoo-format and bare-US symbols (incident 2026-08-25): these used to
    # fall through to the a_share default and walk a CN chain that can never
    # serve them (runs #7/#8: CRML, GC=F, ^TNX, DX-Y.NYB, SPY, URA → 9 gaps).
    # All are Yahoo-native, so route them to the us_equity chain (yfinance
    # first). Bare-ticker pattern is uppercase-only on purpose — normalized CN
    # codes are digits and crypto is BTC-USDT, so no clash.
    (re.compile(r"^[A-Z0-9]{1,6}=F$"), "yfinance", "us_equity"),  # GC=F CL=F
    (re.compile(r"^\^[A-Z0-9.]{1,10}$"), "yfinance", "us_equity"),  # ^TNX ^GSPC
    (re.compile(r"^[A-Z]{1,4}-[A-Z]\.[A-Z]{2,4}$"), "yfinance", "us_equity"),  # DX-Y.NYB
    (re.compile(r"^[A-Z]{1,5}$"), "yfinance", "us_equity"),  # SPY CRML URA AAPL
]


def detect_source(code: str) -> str:
    """Infer the best loader source for a normalized symbol."""
    for pattern, source, _market in _SOURCE_PATTERNS:
        if pattern.match(code):
            return source
    return "tushare"


def detect_market(code: str) -> str:
    """Infer the fallback-chain market key for a normalized symbol."""
    for pattern, _source, market in _SOURCE_PATTERNS:
        if pattern.match(code):
            return market
    return "a_share"


def get_loader(source: str):
    """Get loader class via registry with fallback support."""
    from backtest.loaders.registry import get_loader_cls_with_fallback

    return get_loader_cls_with_fallback(source)


def cap_rows(records: list, max_rows: int) -> list | dict[str, object]:
    """Bound a per-symbol row list to keep tool payloads within budget."""
    n = len(records)
    if max_rows < 0:
        max_rows = DEFAULT_MAX_ROWS
    if max_rows == 0 or n <= max_rows:
        return records
    step = math.ceil(n / max_rows)
    sampled = records[::step]
    if sampled[-1] is not records[-1]:
        sampled = sampled + [records[-1]]
    return {
        "rows": n,
        "returned": len(sampled),
        "truncated": True,
        "policy": f"every-{step}th-row (even stride; last bar pinned)",
        "hint": "narrow the date range, coarsen interval, or set max_rows=0 for all rows",
        "data": sampled,
    }


def _json_safe(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _fetch_via(
    loader_cls: type,
    src: str,
    src_codes: list[str],
    start_date: str,
    end_date: str,
    interval: str,
    *,
    fallback: bool,
) -> tuple[dict[str, Any], str | None]:
    """Run one loader fetch with timing + fetch-stats accounting.

    Returns ``(data_map, error)`` — error is the exception summary when the
    whole call raised, ``None`` otherwise (per-symbol misses are just absent
    keys in ``data_map``).
    """
    t0 = time.monotonic()
    error: str | None = None
    data_map: dict[str, Any] = {}
    try:
        loader = loader_cls()
        data_map = loader.fetch(src_codes, start_date, end_date, interval=interval) or {}
    except Exception as exc:  # noqa: BLE001 - a failed source falls through the chain
        error = f"{type(exc).__name__}: {exc}"[:200]
        # Primary-pass blow-up is ERROR (the request's chosen source is down);
        # a failed fallback attempt is only WARNING (the chain keeps walking).
        logger.log(
            logging.WARNING if fallback else logging.ERROR,
            "market-data loader failed",
            exc_info=not fallback,
            extra={"source": src, "symbols": src_codes, "error": error},
        )
    ms = int((time.monotonic() - t0) * 1000)
    got = sum(1 for code in src_codes if code in data_map)
    _fetch_stats.record_fetch(
        src, ok=got, failed=len(src_codes) - got, ms=ms, fallback=fallback
    )
    return data_map, error


def fetch_market_data(
    *,
    codes: list[str],
    start_date: str,
    end_date: str,
    source: str = "auto",
    interval: str = "1D",
    max_rows: int = DEFAULT_MAX_ROWS,
    loader_resolver: Callable[[str], type] = get_loader,
) -> dict[str, Any]:
    """Fetch normalized OHLCV data through the repository loader layer.

    Reliability semantics (batch 2): a primary-source failure OR an empty
    per-symbol result walks the market's ``FALLBACK_CHAINS`` until a source
    delivers or the chain/budget is exhausted; every attempt is accounted in
    the per-attempt fetch stats, and symbols nothing could serve are reported
    both in ``_unresolved`` (legacy shape) and ``_gaps`` (per-symbol reason +
    sources tried) so the model can state exactly what is missing.
    """
    from backtest.loaders.registry import FALLBACK_CHAINS, LOADER_REGISTRY, _ensure_registered

    results: dict[str, Any] = {}
    tried: dict[str, list[str]] = {code: [] for code in codes}
    last_error: dict[str, str] = {}
    deadline = time.monotonic() + min(
        FETCH_BUDGET_S, _budget.remaining_s(default=FETCH_BUDGET_S) or FETCH_BUDGET_S
    )

    def _ingest(data_map: dict[str, Any]) -> None:
        for symbol, df in data_map.items():
            if symbol in results:
                continue
            records = df.reset_index().to_dict(orient="records")
            for row in records:
                for key, value in row.items():
                    row[key] = _json_safe(value)
            results[symbol] = cap_rows(records, max_rows)

    # ── Primary pass (requested/detected source per code) ────────────────────
    if source == "auto":
        groups: dict[str, list[str]] = {}
        for code in codes:
            src = detect_source(code)
            groups.setdefault(src, []).append(code)
    else:
        groups = {source: list(codes)}

    for src, src_codes in groups.items():
        for code in src_codes:
            tried[code].append(src)
        try:
            loader_cls = loader_resolver(src)
        except Exception as exc:  # noqa: BLE001 - unavailable source → chain pass
            for code in src_codes:
                last_error[code] = f"{type(exc).__name__}: {exc}"[:200]
            _fetch_stats.record_fetch(src, failed=len(src_codes), fallback=False)
            continue
        # loader_resolver may itself have fallen back to another source class.
        actual = getattr(loader_cls, "name", src)
        data_map, error = _fetch_via(
            loader_cls, actual, src_codes, start_date, end_date, interval, fallback=False
        )
        if actual != src:
            for code in src_codes:
                tried[code].append(actual)
        _ingest(data_map)
        if error:
            for code in src_codes:
                last_error.setdefault(code, error)

    # ── Fallback pass: unresolved symbols walk their market chain ────────────
    unresolved = [code for code in codes if code not in results]
    if unresolved:
        _ensure_registered()
    for code in unresolved:
        if code in results:
            continue
        if time.monotonic() > deadline:
            last_error.setdefault(code, "fetch budget exhausted before fallback")
            break
        for name in FALLBACK_CHAINS.get(detect_market(code), []):
            if name in tried[code] or name not in LOADER_REGISTRY:
                continue
            if time.monotonic() > deadline:
                last_error.setdefault(code, "fetch budget exhausted mid-chain")
                break
            tried[code].append(name)
            loader_cls = LOADER_REGISTRY[name]
            try:
                if not loader_cls().is_available():
                    continue
            except Exception:  # noqa: BLE001 - constructor failure = unavailable
                continue
            data_map, error = _fetch_via(
                loader_cls, name, [code], start_date, end_date, interval, fallback=True
            )
            if error:
                last_error[code] = error
            _ingest(data_map)
            if code in results:
                logger.info(
                    "market-data fallback served symbol",
                    extra={"source": name, "symbol": code},
                )
                break

    unresolved = [code for code in codes if code not in results]
    if unresolved:
        results["_unresolved"] = unresolved
        gaps = []
        for code in unresolved:
            reason = last_error.get(code, "no source returned data")
            if "每分钟" in reason or "rate" in reason.lower():
                reason = f"rate_limited: {reason}"
            _fetch_stats.record_gap(code, reason, tried[code])
            gaps.append(
                {"symbol": code, "reason": reason, "sources_tried": tried[code]}
            )
        results["_gaps"] = gaps

    return results


def fetch_market_data_json(**kwargs: Any) -> str:
    """Fetch market data and return strict JSON."""
    return json.dumps(fetch_market_data(**kwargs), ensure_ascii=False, indent=2, allow_nan=False)
