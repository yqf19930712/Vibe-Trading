"""Zero-LLM structural verification at run finalization (harness "Verify" step).

Two cheap, deterministic checks run when a run is judged successful:

1. ``metrics.csv`` sanity — numbers must parse and key metrics must sit inside
   generous plausibility bounds (a sharpe of 400 or a total_return of -730
   almost always means a broken engine, not a great/terrible strategy).
2. Final-answer price grounding — price-like numbers mentioned next to a
   symbol in the final text are compared against the reference prices this
   run actually fetched via ``get_market_data`` / ``get_realtime_quotes``.
   A >20% divergence is flagged.

Design contract: verification NEVER changes the run's success status and
NEVER raises — it only produces a ``verify_warnings`` list that the loop
records into attempt_stats / trace and emits as an event for the
observability panel. No LLM calls, stdlib only.
"""

from __future__ import annotations

import csv
import json
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Plausibility bounds for metrics.csv fields (see backtest/metrics.py for the
# producer: total_return / max_drawdown are fractions, sharpe is annualized).
# Bounds are deliberately loose — they exist to catch engine blow-ups
# (NaN propagation, unit confusion, cash-scale bugs), not to grade strategies.
METRIC_BOUNDS: dict[str, tuple[float, float]] = {
    "total_return": (-1.0, 100.0),   # -100% .. +10000%
    "annual_return": (-1.0, 100.0),
    "max_drawdown": (-1.0, 0.0),     # fraction, non-positive
    "sharpe": (-20.0, 20.0),
    "win_rate": (0.0, 1.0),
    "trade_count": (0.0, 10_000_000.0),
}

# Final-text price check tuning. A candidate number "belongs" to a symbol when
# it appears within this many characters after a symbol mention; it is treated
# as price-like only inside the plausible band around the fetched reference
# price (so volumes / market caps / percentages don't false-positive), and it
# is flagged when no candidate lands within the relative tolerance.
_PRICE_WINDOW_CHARS = 80
_PRICE_BAND_LOW = 1 / 3
_PRICE_BAND_HIGH = 3.0
_PRICE_REL_TOLERANCE = 0.20

_NUMBER_RE = re.compile(r"\d+(?:,\d{3})*(?:\.\d+)?")


def verify_run(
    run_dir: Path,
    final_text: str,
    grounding_results: list[tuple[str, str]],
) -> list[dict[str, Any]]:
    """Run all finalization checks and return the warning list.

    Args:
        run_dir: The run directory (``artifacts/metrics.csv`` is checked
            when present).
        final_text: The final answer text produced by the model.
        grounding_results: ``(tool_name, raw_json_result)`` pairs collected
            from successful ``get_market_data`` / ``get_realtime_quotes``
            calls during this run.

    Returns:
        List of warning dicts (possibly empty). Never raises.
    """
    warnings: list[dict[str, Any]] = []
    try:
        warnings.extend(verify_metrics_csv(run_dir))
    except Exception:  # noqa: BLE001 - verification must never break the run
        logger.debug("metrics.csv verification failed", exc_info=True)
    try:
        refs = extract_reference_prices(grounding_results)
        if refs and final_text:
            warnings.extend(verify_final_text_prices(final_text, refs))
    except Exception:  # noqa: BLE001 - verification must never break the run
        logger.debug("final-text price verification failed", exc_info=True)
    return warnings


def verify_metrics_csv(run_dir: Path) -> list[dict[str, Any]]:
    """Check ``artifacts/metrics.csv`` for parseability and plausible ranges.

    Args:
        run_dir: Run directory containing ``artifacts/metrics.csv``.

    Returns:
        Warning dicts for unparseable or out-of-bounds metric values. Empty
        when the file does not exist.
    """
    path = Path(run_dir) / "artifacts" / "metrics.csv"
    if not path.exists():
        return []

    warnings: list[dict[str, Any]] = []
    try:
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    except (OSError, csv.Error) as exc:
        return [{
            "check": "metrics_csv",
            "code": "unreadable",
            "message": f"metrics.csv could not be parsed: {exc}",
        }]

    if not rows:
        return [{
            "check": "metrics_csv",
            "code": "empty",
            "message": "metrics.csv exists but contains no data rows",
        }]

    row = rows[0]
    for field, (lo, hi) in METRIC_BOUNDS.items():
        raw = row.get(field)
        if raw is None or str(raw).strip() == "":
            continue  # optional field — engines write different supersets
        try:
            value = float(raw)
        except (TypeError, ValueError):
            warnings.append({
                "check": "metrics_csv",
                "code": "unparseable",
                "field": field,
                "value": str(raw)[:50],
                "message": f"metrics.csv field '{field}' is not numeric: {str(raw)[:50]}",
            })
            continue
        if value != value:  # NaN
            warnings.append({
                "check": "metrics_csv",
                "code": "nan",
                "field": field,
                "message": f"metrics.csv field '{field}' is NaN",
            })
        elif not (lo <= value <= hi):
            warnings.append({
                "check": "metrics_csv",
                "code": "out_of_range",
                "field": field,
                "value": value,
                "bounds": [lo, hi],
                "message": (
                    f"metrics.csv field '{field}'={value} outside plausible "
                    f"range [{lo}, {hi}]"
                ),
            })
    return warnings


def extract_reference_prices(
    grounding_results: list[tuple[str, str]],
) -> dict[str, float]:
    """Build a symbol → latest reference price map from grounding tool results.

    Understands the two grounding tool payload shapes:
      * ``get_realtime_quotes``: ``{"quotes": [{"symbol", "last_price", ...}]}``
      * ``get_market_data``: per symbol the compact table
        ``{"summary": {...}, "columns": [..., "close", ...], "rows": [[...], ...]}``
        (``src.market_data.to_table``); the legacy record list
        ``[{..., "close": x}, ...]`` is still understood. Underscore-prefixed
        keys like ``_gaps`` / ``_unresolved`` are metadata.

    Later results win (they are newer within the run).

    Args:
        grounding_results: ``(tool_name, raw_json_result)`` pairs.

    Returns:
        Mapping of symbol (as reported by the tool) to reference price.
    """
    refs: dict[str, float] = {}
    for tool_name, raw in grounding_results:
        try:
            payload = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue

        if tool_name == "get_realtime_quotes":
            for quote in payload.get("quotes") or []:
                if not isinstance(quote, dict):
                    continue
                symbol = quote.get("symbol")
                price = quote.get("last_price")
                if isinstance(symbol, str) and isinstance(price, (int, float)) and price > 0:
                    refs[symbol] = float(price)
        else:  # get_market_data
            for symbol, rows in payload.items():
                if not isinstance(symbol, str) or symbol.startswith("_"):
                    continue
                price = _last_close(rows)
                if isinstance(price, (int, float)) and price > 0:
                    refs[symbol] = float(price)
    return refs


def _last_close(rows: Any) -> Any:
    """Return the last row's ``close`` from either market-data payload shape."""
    if isinstance(rows, dict):
        columns = rows.get("columns")
        table = rows.get("rows")
        if isinstance(columns, list) and isinstance(table, list) and table and "close" in columns:
            last = table[-1]
            idx = columns.index("close")
            if isinstance(last, list) and idx < len(last):
                return last[idx]
        return None
    if isinstance(rows, list) and rows and isinstance(rows[-1], dict):
        return rows[-1].get("close")
    return None


def _symbol_variants(symbol: str) -> list[str]:
    """Return text-mention variants for a tool symbol (``NVDA.US`` → ``NVDA``)."""
    variants = [symbol]
    for suffix in (".US", ".HK", ".SH", ".SZ"):
        if symbol.upper().endswith(suffix):
            bare = symbol[: -len(suffix)]
            if len(bare) >= 2:
                variants.append(bare)
            break
    return variants


def verify_final_text_prices(
    final_text: str,
    reference_prices: dict[str, float],
) -> list[dict[str, Any]]:
    """Flag symbol-adjacent price-like numbers diverging >20% from references.

    For every symbol with a fetched reference price, each mention in the final
    text is scanned ``_PRICE_WINDOW_CHARS`` chars ahead for numbers. Numbers
    inside the plausible price band (1/3× .. 3× the reference) are treated as
    price claims; a mention where at least one price-like candidate exists but
    NONE lands within ±20% of the reference produces one warning per symbol.

    Args:
        final_text: The model's final answer text.
        reference_prices: Symbol → reference price from this run's tools.

    Returns:
        Warning dicts (at most one per symbol).
    """
    warnings: list[dict[str, Any]] = []
    for symbol, ref in reference_prices.items():
        if ref <= 0:
            continue
        flagged_value: float | None = None
        matched_ok = False
        for variant in _symbol_variants(symbol):
            for m in re.finditer(re.escape(variant), final_text, re.IGNORECASE):
                window = final_text[m.end(): m.end() + _PRICE_WINDOW_CHARS]
                candidates = [
                    float(n.replace(",", ""))
                    for n in _NUMBER_RE.findall(window)
                ]
                price_like = [
                    c for c in candidates
                    if ref * _PRICE_BAND_LOW <= c <= ref * _PRICE_BAND_HIGH
                ]
                if not price_like:
                    continue
                if any(abs(c - ref) / ref <= _PRICE_REL_TOLERANCE for c in price_like):
                    matched_ok = True
                else:
                    flagged_value = price_like[0]
            if matched_ok:
                break
        if flagged_value is not None and not matched_ok:
            warnings.append({
                "check": "final_text_price",
                "code": "price_divergence",
                "symbol": symbol,
                "reference_price": ref,
                "text_value": flagged_value,
                "message": (
                    f"Final answer mentions {flagged_value} near '{symbol}' but the "
                    f"run's fetched reference price is {ref} (>20% divergence)"
                ),
            })
    return warnings
