"""Tushare loader for A-share daily and intraday bars plus optional fundamentals.

Supports ``interval``: 1D (default) / 1m / 5m / 15m / 30m / 1H.
Minute data uses ``pro.stk_mins()`` (Tushare points >= 2000).
"""

import logging
import os
import threading
import time
from typing import Dict, List, Optional

import pandas as pd

from backtest.loaders.base import (
    cached_loader_fetch,
    positive_env_float,
    retry_with_budget,
    validate_date_range,
)
from backtest.loaders.registry import register


logger = logging.getLogger(__name__)

TUSHARE_TOKEN_PLACEHOLDERS = {"", "your-tushare-token"}

# One TUSHARE_TOKEN is shared by every tenant engine, so concurrent attempts can
# trip the per-minute quota together. This in-process throttle spaces calls out;
# the ceiling is configurable to match the account's Pro tier (default leaves
# headroom under the common 500/min tier).
_THROTTLE_MIN_INTERVAL_S = 60.0 / positive_env_float("TUSHARE_MAX_PER_MIN", 300.0)
_throttle_lock = threading.Lock()
_throttle_last = 0.0


def _throttled_call(fn, *args, **kwargs):
    """Serialize Tushare API calls to at most TUSHARE_MAX_PER_MIN per minute."""
    global _throttle_last
    with _throttle_lock:
        wait = _throttle_last + _THROTTLE_MIN_INTERVAL_S - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _throttle_last = time.monotonic()
    return fn(*args, **kwargs)


@register
class DataLoader:
    """Tushare-backed OHLCV loader."""

    name = "tushare"
    markets = {"a_share", "futures", "fund"}
    requires_auth = True

    def is_available(self) -> bool:
        """Available when TUSHARE_TOKEN is set."""
        return os.getenv("TUSHARE_TOKEN", "").strip() not in TUSHARE_TOKEN_PLACEHOLDERS

    def __init__(self) -> None:
        """Initialize Tushare pro API."""
        import tushare as ts

        token = os.getenv("TUSHARE_TOKEN", "")
        self.api = ts.pro_api(token)

    def fetch(
        self,
        codes: List[str],
        start_date: str,
        end_date: str,
        fields: Optional[List[str]] = None,
        interval: str = "1D",
    ) -> Dict[str, pd.DataFrame]:
        """Fetch A-share bars via Tushare API.

        Args:
            codes: Stock codes (e.g. ``000001.SZ``).
            start_date: Start date (YYYY-MM-DD).
            end_date: End date (YYYY-MM-DD).
            fields: Extra fundamental columns (daily only).
            interval: Bar size (1D/1m/5m/15m/30m/1H), default ``1D``.

        Returns:
            Mapping code -> OHLCV DataFrame.
        """
        validate_date_range(start_date, end_date)

        if interval != "1D":
            return self._fetch_minutes(codes, start_date, end_date, interval)

        sd = start_date.replace("-", "")
        ed = end_date.replace("-", "")
        cache_fields = list(fields or [])
        result: Dict[str, pd.DataFrame] = {}

        # Every code goes through the opt-in cache helper, which is a direct
        # passthrough when the cache is disabled. Fundamentals are merged inside
        # the cached unit so a cached entry already carries its extra columns.
        for code in codes:
            def _fetch_one(code: str = code) -> Optional[pd.DataFrame]:
                try:
                    df = self._fetch_daily_frame(code, sd, ed)
                    if df is None:
                        return None
                    merged = self._merge_basic_fields(
                        {code: df}, [code], start_date, end_date, cache_fields
                    )
                    return merged.get(code)
                except Exception as exc:
                    logger.warning(
                        "tushare daily fetch failed",
                        extra={"source": "tushare", "symbol": code, "error": str(exc)},
                    )
                    return None

            df = cached_loader_fetch(
                source=self.name,
                symbol=code,
                timeframe="1D",
                start_date=start_date,
                end_date=end_date,
                fields=cache_fields,
                fetch=_fetch_one,
            )
            if df is not None and not df.empty:
                result[code] = df

        return result

    def _fetch_daily_frame(
        self,
        code: str,
        start_date: str,
        end_date: str,
    ) -> Optional[pd.DataFrame]:
        """Fetch and normalize one daily OHLCV frame (throttled + bounded retry)."""
        df = retry_with_budget(
            lambda: _throttled_call(
                self.api.daily, ts_code=code, start_date=start_date, end_date=end_date
            ),
            transient=Exception,
            deadline=time.monotonic() + 30,
            label=f"tushare daily {code}",
            max_retries=2,
            backoff=(1.0, 3.0),
        )
        if df is None or df.empty:
            return None
        df = df.sort_values("trade_date")
        df["trade_date"] = pd.to_datetime(df["trade_date"])
        df = df.set_index("trade_date")
        df = df.rename(columns={"vol": "volume"})
        for col in ["open", "high", "low", "close", "volume"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        ohlcv = df[["open", "high", "low", "close", "volume"]].dropna(
            subset=["open", "high", "low", "close"]
        )
        return ohlcv

    def _merge_basic_fields(
        self,
        result: Dict[str, pd.DataFrame],
        codes: List[str],
        start_date: str,
        end_date: str,
        fields: Optional[List[str]],
    ) -> Dict[str, pd.DataFrame]:
        """Merge fundamental columns from daily_basic API.

        Args:
            result: Existing OHLCV frames.
            codes: All requested codes.
            start_date: Start date.
            end_date: End date.
            fields: Extra column names from daily_basic.

        Returns:
            Updated result map.
        """
        if not fields:
            return result

        sd = start_date.replace("-", "")
        ed = end_date.replace("-", "")
        active_codes = [c for c in codes if c in result]

        for code in active_codes:
            try:
                basic = _throttled_call(
                    self.api.daily_basic,
                    ts_code=code,
                    start_date=sd,
                    end_date=ed,
                    fields="ts_code,trade_date," + ",".join(fields),
                )
                if basic is not None and not basic.empty:
                    basic["trade_date"] = pd.to_datetime(basic["trade_date"])
                    basic = basic.set_index("trade_date").sort_index()
                    for f in fields:
                        if f in basic.columns:
                            result[code][f] = basic[f]
            except Exception as exc:
                logger.warning(
                    "tushare daily_basic fetch failed",
                    extra={"source": "tushare", "symbol": code, "error": str(exc)},
                )

        return result

    def _fetch_minutes(
        self,
        codes: List[str],
        start_date: str,
        end_date: str,
        interval: str,
    ) -> Dict[str, pd.DataFrame]:
        """Intraday bars via stk_mins.

        Args:
            codes: Stock codes.
            start_date: Start date (YYYY-MM-DD).
            end_date: End date (YYYY-MM-DD).
            interval: Minute bar (1m/5m/15m/30m/1H).

        Returns:
            Mapping code -> DataFrame.
        """
        freq_map = {"1m": "1min", "5m": "5min", "15m": "15min", "30m": "30min", "1H": "60min"}
        freq = freq_map.get(interval)
        if not freq:
            logger.error("unsupported Tushare interval: %s", interval)
            return {}

        sd = start_date.replace("-", "")
        ed = end_date.replace("-", "")
        result: Dict[str, pd.DataFrame] = {}

        for code in codes:
            try:
                df = _throttled_call(
                    self.api.stk_mins, ts_code=code, freq=freq, start_date=sd, end_date=ed
                )
                if df is None or df.empty:
                    logger.warning(
                        "empty Tushare minute data (points >= 2000 required)",
                        extra={"source": "tushare", "symbol": code},
                    )
                    continue
                df = df.sort_values("trade_time")
                df["trade_date"] = pd.to_datetime(df["trade_time"])
                df = df.set_index("trade_date")
                df = df.rename(columns={"vol": "volume"})
                for col in ["open", "high", "low", "close", "volume"]:
                    if col in df.columns:
                        df[col] = pd.to_numeric(df[col], errors="coerce")
                ohlcv = df[["open", "high", "low", "close", "volume"]].dropna(
                    subset=["open", "high", "low", "close"]
                )
                result[code] = ohlcv
            except Exception as exc:
                logger.warning(
                    "tushare minute fetch failed",
                    extra={"source": "tushare", "symbol": code, "error": str(exc)},
                )
        return result
