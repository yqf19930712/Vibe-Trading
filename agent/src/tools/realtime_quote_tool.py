"""Realtime quote tool backed by TickFlow (US equities).

Before this tool the model had NO structured realtime channel and resorted to
bash-curling Tencent's qt.gtimg.cn (which lacks recent IPOs — CBRS returned
``pv_none_match`` in attempt dea1222743ef). TickFlow's ``/v1/quotes`` is
China-direct, structured, and covers the whole US board.

Free-tier limits: 10 requests/min, 5 symbols per request — the tool batches
accordingly and retries a 429 once. Requires ``TICKFLOW_API_KEY``.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from src.agent.tools import BaseTool

_BASE_URL = os.environ.get("TICKFLOW_BASE_URL", "https://api.tickflow.org")
_TIMEOUT_S = 30
_BATCH = 5  # free tier: 5 symbols per quotes request
_RATE_RETRY_SLEEP_S = 6.5

_BARE_US_RE = re.compile(r"^[A-Z]{1,5}$")


def _to_us_symbol(code: str) -> str | None:
    upper = str(code).strip().upper()
    if upper.endswith(".US"):
        return upper
    if _BARE_US_RE.match(upper):
        return f"{upper}.US"
    return None


def _http_get_quotes(symbols: list[str], api_key: str) -> list[dict[str, Any]]:
    url = f"{_BASE_URL}/v1/quotes?{urllib.parse.urlencode({'symbols': ','.join(symbols)})}"
    req = urllib.request.Request(url, headers={
        "x-api-key": api_key,
        # Cloudflare rejects the default Python UA signature (403 / 1010).
        "User-Agent": "vibe-trading/1.0",
    })
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
                data = payload.get("data")
                return data if isinstance(data, list) else []
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt == 1:
                time.sleep(_RATE_RETRY_SLEEP_S)
                continue
            body = ""
            try:
                body = exc.read().decode("utf-8", "replace")[:200]
            except Exception:  # noqa: BLE001
                pass
            raise RuntimeError(f"tickflow quotes HTTP {exc.code}: {body}") from exc
    return []


class RealtimeQuoteTool(BaseTool):
    """US-equity realtime snapshot quotes via TickFlow."""

    name = "get_realtime_quotes"
    description = (
        "Get realtime snapshot quotes for US stocks/ETFs (last price, change vs "
        "prev close, OHLC, volume, session) via the structured TickFlow API. "
        "Use this instead of bash-curling quote websites. US symbols only "
        '(e.g. ["INTC.US", "NVDA"]); for A-shares/HK realtime use other means.'
    )
    parameters = {
        "type": "object",
        "properties": {
            "codes": {
                "type": "array",
                "items": {"type": "string"},
                "description": 'US symbols, with or without the .US suffix, e.g. ["INTC.US", "AVGO"].',
            },
        },
        "required": ["codes"],
    }
    repeatable = True  # quotes are volatile by nature — re-calls are legitimate
    is_readonly = True

    def check_available(self) -> bool:
        return bool(os.environ.get("TICKFLOW_API_KEY", "").strip())

    def execute(self, **kwargs: Any) -> str:
        api_key = os.environ.get("TICKFLOW_API_KEY", "").strip()
        if not api_key:
            return json.dumps({"status": "error", "error": "TICKFLOW_API_KEY not configured"})

        codes = kwargs.get("codes") or []
        if not isinstance(codes, list) or not codes:
            return json.dumps({"status": "error", "error": "codes must be a non-empty array"})

        symbol_map: dict[str, str] = {}  # api symbol -> requested code
        unsupported: list[str] = []
        for code in codes:
            sym = _to_us_symbol(code)
            if sym is None:
                unsupported.append(str(code))
            else:
                symbol_map.setdefault(sym, str(code))

        quotes: list[dict[str, Any]] = []
        errors: list[str] = []
        symbols = list(symbol_map)
        t0 = time.monotonic()
        ok_count = 0
        for i in range(0, len(symbols), _BATCH):
            batch = symbols[i : i + _BATCH]
            try:
                for q in _http_get_quotes(batch, api_key):
                    ext = q.get("ext") or {}
                    last = q.get("last_price")
                    prev = q.get("prev_close")
                    change_pct = ext.get("change_pct")
                    if change_pct is None and last is not None and prev:
                        change_pct = (last - prev) / prev
                    quotes.append({
                        "symbol": q.get("symbol"),
                        "name": ext.get("name"),
                        "last_price": last,
                        "prev_close": prev,
                        "change_pct": round(change_pct, 6) if isinstance(change_pct, (int, float)) else None,
                        "open": q.get("open"),
                        "high": q.get("high"),
                        "low": q.get("low"),
                        "volume": q.get("volume"),
                        "amount": q.get("amount"),
                        "session": q.get("session"),
                        "timestamp_ms": q.get("timestamp"),
                    })
                    ok_count += 1
            except Exception as exc:  # noqa: BLE001 - per-batch degradation
                errors.append(f"{','.join(batch)}: {str(exc)[:200]}")

        elapsed_ms = int((time.monotonic() - t0) * 1000)
        try:  # attempt-level data-source accounting (best-effort)
            from src.core.fetch_stats import record_fetch

            record_fetch(
                "tickflow",
                ok=ok_count,
                failed=len(symbols) - ok_count,
                ms=elapsed_ms,
            )
        except Exception:  # noqa: BLE001 - stats must never break the tool
            pass

        returned = {q["symbol"] for q in quotes}
        missing = [symbol_map[s] for s in symbols if s not in returned]
        out: dict[str, Any] = {"status": "ok" if quotes else "error", "quotes": quotes}
        if missing:
            out["missing"] = missing
        if unsupported:
            out["unsupported_non_us"] = unsupported
        if errors:
            out["errors"] = errors
        return json.dumps(out, ensure_ascii=False)
