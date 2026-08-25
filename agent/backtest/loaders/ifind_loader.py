"""iFinD (同花顺) MCP loader: US & HK daily OHLCV via the 51ifind MCP service.

Domestic endpoint (``api-mcp.51ifind.com:8643``) — reachable from mainland
without the server-B egress tunnel, which makes it the natural fallback when
yfinance gets rate-limited on the shared exit IP (US equities have no other
domestic source; verified live 2026-08-25 with INTC).

Protocol: MCP streamable-HTTP JSON-RPC against the ``global_stock`` server
(``initialize`` -> ``Mcp-Session-Id`` header -> ``tools/call``). The only
quotes tool is natural-language (``global_stock_quotes`` with a ``query``
string); the response nests JSON twice and carries a markdown table in
``answer``, so the parser is header-driven and tolerant:

- tables other than the one with a 日期 column are ignored (a currency
  summary table precedes the price table);
- non-trading rows carry the previous close with the other columns empty
  (a literal tab) and are dropped — we require full OHLC;
- volumes come formatted with Chinese units ("9690.34万", "1.17亿").

Auth: ``IFIND_MCP_TOKEN`` env (raw token in the ``Authorization`` header, no
Bearer prefix). Forwarded into tenant sandboxes via the router's FORWARD_ENV.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import urllib.error
import urllib.request
from typing import Dict, List, Optional

import pandas as pd

from backtest.loaders.base import cached_loader_fetch, validate_date_range
from backtest.loaders.registry import register

logger = logging.getLogger(__name__)

_SERVER_URL = os.environ.get(
    "IFIND_MCP_GLOBAL_STOCK_URL",
    "https://api-mcp.51ifind.com:8643/ds-mcp-servers/hexin-ifind-ds-global-stock-mcp",
)
_PROTOCOL_VERSION = "2025-03-26"
_TIMEOUT_S = 60

# One MCP session per process; re-initialized once on auth/session errors.
_session_lock = threading.Lock()
_session = {"id": None, "req_id": 0}


def _token() -> str:
    return os.environ.get("IFIND_MCP_TOKEN", "").strip()


def _post(payload: dict, timeout: int = _TIMEOUT_S) -> tuple[int, dict | str, dict]:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(_SERVER_URL, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json, text/event-stream")
    req.add_header("Authorization", _token())
    if _session["id"]:
        req.add_header("Mcp-Session-Id", _session["id"])
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        headers = dict(resp.headers)
        raw = resp.read().decode("utf-8", "replace")
        if "text/event-stream" in resp.headers.get("Content-Type", ""):
            datas = [ln[5:].strip() for ln in raw.splitlines() if ln.startswith("data:")]
            raw = datas[-1] if datas else raw
        try:
            return resp.status, json.loads(raw), headers
        except (ValueError, TypeError):
            return resp.status, raw, headers


def _rpc(method: str, params: Optional[dict] = None) -> dict | str:
    _session["req_id"] += 1
    payload: dict = {"jsonrpc": "2.0", "id": _session["req_id"], "method": method}
    if params is not None:
        payload["params"] = params
    _, data, _ = _post(payload)
    return data


def _init_session() -> None:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": _PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "vibe-trading-loader", "version": "1.0.0"},
        },
    }
    _, _, headers = _post(payload, timeout=30)
    sid = headers.get("Mcp-Session-Id") or headers.get("mcp-session-id")
    if not sid:
        raise RuntimeError("iFinD MCP initialize returned no Mcp-Session-Id")
    _session["id"] = sid
    try:
        _post({"jsonrpc": "2.0", "method": "notifications/initialized"}, timeout=10)
    except Exception:  # noqa: BLE001 - notification failure is non-fatal
        pass


def _call_quotes(query: str) -> str:
    """tools/call global_stock_quotes -> the inner ``answer`` markdown text."""
    with _session_lock:
        if not _session["id"]:
            _init_session()
        try:
            data = _rpc(
                "tools/call",
                {"name": "global_stock_quotes", "arguments": {"query": query}},
            )
        except urllib.error.HTTPError as exc:
            # Session likely expired/invalid — one re-init retry, then give up.
            if exc.code not in (400, 401, 404):
                raise
            _session["id"] = None
            _init_session()
            data = _rpc(
                "tools/call",
                {"name": "global_stock_quotes", "arguments": {"query": query}},
            )
    if not isinstance(data, dict):
        raise RuntimeError(f"iFinD MCP non-JSON response: {str(data)[:200]}")
    if "error" in data:
        raise RuntimeError(f"iFinD MCP error: {json.dumps(data['error'], ensure_ascii=False)[:300]}")
    content = (data.get("result") or {}).get("content") or []
    text = next(
        (c.get("text") for c in content if isinstance(c, dict) and c.get("type") == "text"),
        None,
    )
    if not text:
        raise RuntimeError("iFinD MCP response has no text content")
    outer = json.loads(text)
    if outer.get("code") != 1:
        raise RuntimeError(
            f"iFinD MCP business error: code={outer.get('code')} msg={str(outer.get('msg'))[:200]}"
        )
    inner_raw = outer.get("data")
    inner = json.loads(inner_raw) if isinstance(inner_raw, str) else (inner_raw or {})
    answer = inner.get("answer")
    if not isinstance(answer, str) or not answer.strip():
        raise RuntimeError("iFinD MCP answer is empty")
    return answer


_CN_NUM_UNITS = {"万": 1e4, "亿": 1e8}


def _parse_number(cell: str) -> Optional[float]:
    s = (cell or "").strip().replace(",", "")
    if not s or s in {"-", "--"}:
        return None
    unit = 1.0
    if s and s[-1] in _CN_NUM_UNITS:
        unit = _CN_NUM_UNITS[s[-1]]
        s = s[:-1]
    try:
        return float(s) * unit
    except ValueError:
        return None


# Header keyword -> canonical column. Headers may carry suffixes like
# 「收盘价（单位：元）」, so match by substring.
_COLUMN_KEYS = [
    ("日期", "trade_date"),
    ("开盘价", "open"),
    ("最高价", "high"),
    ("最低价", "low"),
    ("收盘价", "close"),
    ("成交量", "volume"),
]


def _parse_answer_table(answer: str) -> Optional[pd.DataFrame]:
    """Extract the daily-price markdown table (the one with a 日期 column)."""
    tables: List[List[List[str]]] = []
    current: List[List[str]] = []
    for line in answer.splitlines():
        line = line.strip()
        if line.startswith("|") and line.endswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                continue  # separator row
            current.append(cells)
        elif current:
            tables.append(current)
            current = []
    if current:
        tables.append(current)

    for table in tables:
        if len(table) < 2:
            continue
        header = table[0]
        col_idx: Dict[str, int] = {}
        for i, cell in enumerate(header):
            for key, canonical in _COLUMN_KEYS:
                if key in cell and canonical not in col_idx:
                    col_idx[canonical] = i
        if "trade_date" not in col_idx or "close" not in col_idx:
            continue
        rows = []
        for cells in table[1:]:
            def cell(name: str) -> str:
                i = col_idx.get(name, -1)
                return cells[i] if 0 <= i < len(cells) else ""

            date_s = re.sub(r"[^0-9]", "", cell("trade_date"))
            if len(date_s) != 8:
                continue
            row = {
                "trade_date": f"{date_s[:4]}-{date_s[4:6]}-{date_s[6:]}",
                "open": _parse_number(cell("open")),
                "high": _parse_number(cell("high")),
                "low": _parse_number(cell("low")),
                "close": _parse_number(cell("close")),
                "volume": _parse_number(cell("volume")),
            }
            # Non-trading rows carry forward the close with empty OHLC — drop.
            if None in (row["open"], row["high"], row["low"], row["close"]):
                continue
            rows.append(row)
        if not rows:
            continue
        df = pd.DataFrame(rows)
        df["trade_date"] = pd.to_datetime(df["trade_date"])
        df = df.set_index("trade_date").sort_index()
        return df[["open", "high", "low", "close", "volume"]]
    return None


def _build_query(code: str, start_date: str, end_date: str) -> Optional[str]:
    upper = code.strip().upper()
    start = start_date.replace("-", "")
    end = end_date.replace("-", "")
    if upper.endswith(".US"):
        market, symbol = "美股", upper[:-3]
    elif upper.endswith(".HK"):
        market, symbol = "港股", upper[:-3].lstrip("0") or "0"
    else:
        return None
    return (
        f"{market}{symbol} {start}-{end} 每日的开盘价、最高价、最低价、收盘价、成交量"
    )


@register
class DataLoader:
    """iFinD MCP US/HK daily OHLCV loader (domestic endpoint, token auth)."""

    name = "ifind"
    markets = {"us_equity", "hk_equity"}
    requires_auth = True

    def __init__(self) -> None:
        pass

    def is_available(self) -> bool:
        return bool(_token())

    def fetch(
        self,
        codes: List[str],
        start_date: str,
        end_date: str,
        *,
        interval: str = "1D",
        fields: Optional[List[str]] = None,
    ) -> Dict[str, pd.DataFrame]:
        validate_date_range(start_date, end_date)
        if str(interval or "1D").strip().upper() not in {"1D", "D", "1DAY", "DAY"}:
            return {}  # NL quotes tool is daily-only; let the chain move on

        result: Dict[str, pd.DataFrame] = {}
        for code in codes:
            try:
                df = cached_loader_fetch(
                    source=self.name,
                    symbol=code,
                    timeframe="1D",
                    start_date=start_date,
                    end_date=end_date,
                    fields=None,
                    fetch=lambda code=code: self._fetch_one(code, start_date, end_date),
                )
                if df is not None and not df.empty:
                    result[code] = df
            except Exception as exc:
                logger.warning("ifind failed for %s: %s", code, exc)
        return result

    def _fetch_one(
        self, code: str, start_date: str, end_date: str,
    ) -> Optional[pd.DataFrame]:
        query = _build_query(code, start_date, end_date)
        if query is None:
            return None
        answer = _call_quotes(query)
        df = _parse_answer_table(answer)
        if df is None or df.empty:
            logger.warning(
                "ifind returned no parsable table for %s (answer head: %s)",
                code,
                answer[:160].replace("\n", " "),
            )
            return None
        # Clamp to the requested window — the NL backend occasionally pads.
        return df.loc[(df.index >= start_date) & (df.index <= end_date)]
