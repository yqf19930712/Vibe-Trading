"""TickFlow loader: US equity daily OHLCV via api.tickflow.org REST.

Structured column-oriented kline API (https://docs.tickflow.org) — unlike the
iFinD NL interface there is nothing to parse heuristically. Reachable from
the mainland without the server-B egress tunnel (Aliyun direct, ~2s incl.
TLS; verified live 2026-08-25 with INTC, values byte-identical to iFinD).
Leads the us_equity chain (tickflow → ifind → yfinance → akshare); in
hk_equity it sits second after ifind because the current plan is US-only —
for .HK it no-ops and the chain moves on.

Free-plan scope (this deployment's key): US equities only, klines rate-limited
to 10 req/min at 1 symbol/req — a 429 gets one retry after the rate window
breathes. Symbol format matches the project convention verbatim (``AAPL.US``).

Auth: ``TICKFLOW_API_KEY`` env, sent as the ``x-api-key`` header. Forwarded
into tenant sandboxes via the router's FORWARD_ENV.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, List, Optional

import pandas as pd

from backtest.loaders.base import cached_loader_fetch, validate_date_range
from backtest.loaders.registry import register

logger = logging.getLogger(__name__)

_BASE_URL = os.environ.get("TICKFLOW_BASE_URL", "https://api.tickflow.org")
_TIMEOUT_S = 30
# Free plan: 10 kline requests/min. On 429 wait for the window and retry once.
_RATE_RETRY_SLEEP_S = 6.5

_PERIOD_MAP = {"1D": "1d", "D": "1d", "1DAY": "1d", "DAY": "1d"}


def _api_key() -> str:
    return os.environ.get("TICKFLOW_API_KEY", "").strip()


def _to_ms(date_str: str) -> int:
    return int(pd.Timestamp(date_str, tz="UTC").timestamp() * 1000)


def _get_klines(symbol: str, start_date: str, end_date: str) -> Optional[dict]:
    params = {
        "symbol": symbol,
        "period": "1d",
        "adjust": "forward",
        "count": "10000",
        "start_time": str(_to_ms(start_date)),
        # US daily bars are stamped 04:00/05:00 UTC of the trade date, so the
        # end-of-day cutoff needs the full end date included.
        "end_time": str(_to_ms(end_date) + 86_400_000 - 1),
    }
    url = f"{_BASE_URL}/v1/klines?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={
        "x-api-key": _api_key(),
        # Cloudflare blocks the default Python-urllib UA signature (403,
        # error code 1010) — any explicit UA passes.
        "User-Agent": "vibe-trading/1.0",
    })
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt == 1:
                logger.info("tickflow rate-limited for %s; retrying once", symbol)
                time.sleep(_RATE_RETRY_SLEEP_S)
                continue
            body = ""
            try:
                body = exc.read().decode("utf-8", "replace")[:200]
            except Exception:  # noqa: BLE001
                pass
            raise RuntimeError(f"tickflow HTTP {exc.code}: {body}") from exc
    return None


def _to_frame(payload: dict) -> Optional[pd.DataFrame]:
    data = (payload or {}).get("data") or {}
    ts = data.get("timestamp") or []
    if not ts:
        return None
    cols = {}
    for key in ("open", "high", "low", "close", "volume"):
        vals = data.get(key)
        if not isinstance(vals, list) or len(vals) != len(ts):
            return None
        cols[key] = vals
    df = pd.DataFrame(cols)
    # Bars are stamped at US-midnight in UTC (04:00/05:00Z) — the UTC calendar
    # date IS the trade date, so truncating to date is safe year-round.
    df["trade_date"] = pd.to_datetime(pd.Series(ts), unit="ms", utc=True).dt.tz_localize(None).dt.normalize()
    df = df.set_index("trade_date").sort_index()
    return df[["open", "high", "low", "close", "volume"]].dropna(
        subset=["open", "high", "low", "close"]
    )


@register
class DataLoader:
    """TickFlow US-equity daily OHLCV loader (structured REST, token auth)."""

    name = "tickflow"
    markets = {"us_equity"}
    requires_auth = True

    def __init__(self) -> None:
        pass

    def is_available(self) -> bool:
        return bool(_api_key())

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
        if str(interval or "1D").strip().upper() not in _PERIOD_MAP:
            return {}  # US minute data not offered; let the chain move on

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
                logger.warning("tickflow failed for %s: %s", code, exc)
        return result

    def _fetch_one(
        self, code: str, start_date: str, end_date: str,
    ) -> Optional[pd.DataFrame]:
        upper = code.strip().upper()
        if not upper.endswith(".US"):
            return None
        payload = _get_klines(upper, start_date, end_date)
        if payload is None:
            return None
        return _to_frame(payload)
