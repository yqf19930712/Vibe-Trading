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

# Per-symbol row cap. 120 daily bars ≈ half a trading year; together with the
# compact table shape below a default single-symbol call stays under the
# 10k-char trajectory budget (``tool_result_store.TOOL_RESULT_LIMIT``) instead
# of being offloaded to disk on every call (250 indented records ≈ 56–63k).
DEFAULT_MAX_ROWS = 120
# Numeric columns are rounded to this many decimals in the tool payload.
PRICE_DECIMALS = 4
# Key of the per-symbol row table (``{"columns": [...], "rows": [[...]]}``).
TABLE_COLUMNS_KEY = "columns"
TABLE_ROWS_KEY = "rows"

# Total wall-clock budget for one fetch_market_data call INCLUDING the
# fallback chain (further capped by the attempt deadline when one is bound).
FETCH_BUDGET_S = float(os.getenv("VIBE_TRADING_FETCH_BUDGET_S", "120"))

_SOURCE_PATTERNS = [
    (re.compile(r"^\d{6}\.(SZ|SH|BJ)$", re.I), "tushare", "a_share"),
    # US/HK primary = the chain head (tickflow / ifind, China-direct) — the
    # 2026-08-25 reorder must apply to the PRIMARY pass too, not only the
    # fallback walk (attempt f9b0c0cdcded still led with yfinance here).
    (re.compile(r"^[A-Z]+\.US$", re.I), "tickflow", "us_equity"),
    (re.compile(r"^\d{3,5}\.HK$", re.I), "ifind", "hk_equity"),
    (re.compile(r"^[A-Z]+-USDT$", re.I), "okx", "crypto"),
    (re.compile(r"^[A-Z]+/USDT$", re.I), "ccxt", "crypto"),
    # Yahoo-format and bare-US symbols (incident 2026-08-25): these used to
    # fall through to the a_share default and walk a CN chain that can never
    # serve them (runs #7/#8: CRML, GC=F, ^TNX, DX-Y.NYB, SPY, URA → 9 gaps).
    # Yahoo specials (futures/indices/odd tickers) stay yfinance-native; bare
    # plain tickers go to the chain head like .US. Bare-ticker pattern is
    # uppercase-only on purpose — normalized CN codes are digits and crypto
    # is BTC-USDT, so no clash.
    (re.compile(r"^[A-Z0-9]{1,6}=F$"), "yfinance", "us_equity"),  # GC=F CL=F
    (re.compile(r"^\^[A-Z0-9.]{1,10}$"), "yfinance", "us_equity"),  # ^TNX ^GSPC
    (re.compile(r"^[A-Z]{1,4}-[A-Z]\.[A-Z]{2,4}$"), "yfinance", "us_equity"),  # DX-Y.NYB
    (re.compile(r"^[A-Z]{1,5}$"), "tickflow", "us_equity"),  # SPY CRML URA AAPL
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
    """Coerce one cell into a compact, strict-JSON-safe scalar.

    Timestamps become ISO strings (a midnight time part is dropped so daily
    bars carry a bare ``YYYY-MM-DD``); floats are rounded to
    :data:`PRICE_DECIMALS` and integral floats (volume, amount) lose their
    ``.0``; NaN/inf become ``None`` so ``allow_nan=False`` never raises.
    """
    if hasattr(value, "isoformat"):
        text = value.isoformat()
        if text.endswith("T00:00:00"):
            text = text[: -len("T00:00:00")]
        return text
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        value = round(value, PRICE_DECIMALS)
        if value.is_integer() and abs(value) < 1e15:
            return int(value)
        return value
    return value


def _summarize(records: list[dict[str, Any]], columns: list[str]) -> dict[str, Any]:
    """Build the per-symbol ``summary`` block from the (already capped) rows.

    The summary is what survives structured truncation
    (``tool_result_store``) — start/end date, row count, first/last close,
    range high/low and the period return — so the model can still reason
    about the series when the middle rows have been dropped.
    """
    if not records:
        return {"rows": 0}
    date_col = columns[0] if columns else None
    first, last = records[0], records[-1]
    summary: dict[str, Any] = {"rows": len(records)}
    if date_col is not None:
        summary["start"] = first.get(date_col)
        summary["end"] = last.get(date_col)

    def _num(row: dict[str, Any], key: str) -> float | None:
        v = row.get(key)
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    first_close, last_close = _num(first, "close"), _num(last, "close")
    if first_close is not None:
        summary["first_close"] = first_close
    if last_close is not None:
        summary["last_close"] = last_close
    if first_close and last_close is not None:
        summary["change_pct"] = round((last_close / first_close - 1.0) * 100.0, 2)
    highs = [v for v in (_num(r, "high" if "high" in columns else "close") for r in records) if v is not None]
    lows = [v for v in (_num(r, "low" if "low" in columns else "close") for r in records) if v is not None]
    if highs:
        summary["high"] = max(highs)
    if lows:
        summary["low"] = min(lows)
    return summary


def to_table(capped: list | dict[str, object]) -> dict[str, Any]:
    """Turn a (possibly capped) record list into the compact table payload.

    Shape (``summary`` first so a structured truncation keeps it)::

        {"summary": {...}, "columns": [...], "rows": [[...], ...]}

    A capped series (see :func:`cap_rows`) additionally carries
    ``total_rows`` / ``returned`` / ``truncated`` / ``policy`` / ``hint``.
    Column names are emitted once instead of once per row — the single
    biggest saving over ``to_dict(orient="records")``.
    """
    meta: dict[str, Any] = {}
    if isinstance(capped, dict):
        records = list(capped.get("data") or [])
        meta = {
            "total_rows": capped.get("rows"),
            "returned": capped.get("returned"),
            "truncated": True,
            "policy": capped.get("policy"),
            "hint": capped.get("hint"),
        }
    else:
        records = list(capped)
    columns: list[str] = []
    for row in records:
        for key in row:
            if key not in columns:
                columns.append(str(key))
    table: dict[str, Any] = {"summary": _summarize(records, columns)}
    if meta:
        table["summary"]["total_rows"] = meta["total_rows"]
        table.update(meta)
    table[TABLE_COLUMNS_KEY] = columns
    table[TABLE_ROWS_KEY] = [[row.get(col) for col in columns] for row in records]
    return table


def table_rows(value: Any) -> tuple[list[str], list[list[Any]]] | None:
    """Return ``(columns, rows)`` for a per-symbol table payload, else None.

    Shared by the grounding verifier and the structured truncation so the
    table contract lives in one place.
    """
    if not isinstance(value, dict):
        return None
    columns = value.get(TABLE_COLUMNS_KEY)
    rows = value.get(TABLE_ROWS_KEY)
    if not isinstance(columns, list) or not isinstance(rows, list):
        return None
    return [str(c) for c in columns], rows


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

    # Belt-and-braces for callers that bypass the registry's schema coercion
    # (gateway/MCP direct calls): stringified numerics/arrays must not blow up
    # deep in cap_rows (attempt 052d98f52286: max_rows "0" → TypeError).
    if isinstance(max_rows, str):
        try:
            max_rows = int(max_rows.strip(), 10)
        except ValueError:
            max_rows = DEFAULT_MAX_ROWS
    if isinstance(codes, str):
        try:
            parsed = json.loads(codes)
            codes = parsed if isinstance(parsed, list) else [codes]
        except ValueError:
            codes = [c.strip() for c in codes.split(",") if c.strip()]

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
            results[symbol] = to_table(cap_rows(records, max_rows))

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


def dumps_compact(payload: Any) -> str:
    """Serialize a market-data payload as compact strict JSON (no indent)."""
    return json.dumps(
        payload, ensure_ascii=False, indent=None, separators=(",", ":"), allow_nan=False
    )


def fetch_market_data_json(**kwargs: Any) -> str:
    """Fetch market data and return compact strict JSON.

    Per symbol: ``{"summary": {...}, "columns": [...], "rows": [[...]]}``;
    ``_unresolved`` / ``_gaps`` metadata keys as before.
    """
    return dumps_compact(fetch_market_data(**kwargs))
