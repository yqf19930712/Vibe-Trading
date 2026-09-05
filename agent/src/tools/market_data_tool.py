"""Local market data tool backed by the shared loader layer."""

from __future__ import annotations

from typing import Any

from src.agent.tools import BaseTool
from src.market_data import DEFAULT_MAX_ROWS, fetch_market_data_json

# Bar sizes accepted by at least one registered loader (union of the loaders'
# interval maps: okx/ccxt 1m…1D, tushare 1m…1H+1D, mootdx 1m…1H+1D/1W/1M,
# futu 1H/4H/1D/1W/1M, yfinance 1H/4H/1D, akshare 1D/1W/1M, tickflow/ifind/
# baostock/tencent 1D only). Every loader supports ``1D``; a loader handed an
# interval it cannot serve either returns nothing (the fallback chain then
# walks on) or coarsens to 1D — see each loader's docstring.
SUPPORTED_INTERVALS = ["1m", "5m", "15m", "30m", "1H", "4H", "1D", "1W", "1M"]


def _source_names() -> list[str]:
    """``auto`` first, then every registered loader name (registry-driven)."""
    try:
        from backtest.loaders.registry import VALID_SOURCES

        names = sorted(n for n in VALID_SOURCES if n != "auto")
    except Exception:  # noqa: BLE001 - registry import must never break tool listing
        names = ["akshare", "baostock", "ccxt", "futu", "ifind", "mootdx",
                 "okx", "tencent", "tickflow", "tushare", "yfinance"]
    return ["auto", *names]


_SOURCES = _source_names()


class MarketDataTool(BaseTool):
    """Fetch normalized OHLCV data through repository loaders."""

    name = "get_market_data"
    description = (
        "Fetch normalized OHLCV market data through the repository loader layer. "
        "Use this for stock, ETF, index, or crypto price bars before writing raw "
        "yfinance/OKX/Tushare scripts. Returns compact JSON per symbol: "
        '{"summary": {start, end, rows, first_close, last_close, high, low, change_pct}, '
        '"columns": [...], "rows": [[...], ...]} (column names listed once; daily dates '
        "as YYYY-MM-DD; numbers rounded to 4 decimals). Default max_rows="
        f"{DEFAULT_MAX_ROWS} per symbol keeps a single-symbol call inside the 10k-char "
        "context budget; a longer series is evenly downsampled (every step-th bar, last "
        "bar pinned) with truncated=true. Results over 10k chars are written to disk "
        "and you get summary + first/last 20 rows plus the file path (read_file pages "
        "through it). Symbols nothing could serve are listed under _unresolved/_gaps."
    )
    parameters = {
        "type": "object",
        "properties": {
            "codes": {
                "type": "array",
                "items": {"type": "string"},
                "description": 'Symbols such as ["AAPL.US"], ["700.HK"], ["000001.SZ"], ["BTC-USDT"].',
            },
            "start_date": {
                "type": "string",
                "description": "Start date in YYYY-MM-DD format.",
            },
            "end_date": {
                "type": "string",
                "description": "End date in YYYY-MM-DD format.",
            },
            "source": {
                "type": "string",
                "enum": _SOURCES,
                "description": (
                    "Data source. 'auto' (default) picks per symbol format and walks the "
                    "market's fallback chain. Registered loaders: "
                    + ", ".join(n for n in _SOURCES if n != "auto")
                    + "."
                ),
                "default": "auto",
            },
            "interval": {
                "type": "string",
                "enum": SUPPORTED_INTERVALS,
                "description": (
                    "Bar size. 1D is supported by every source; intraday (1m/5m/15m/30m/"
                    "1H/4H) by okx/ccxt/tushare/mootdx/futu/yfinance; 1W/1M by "
                    "mootdx/futu/akshare."
                ),
                "default": "1D",
            },
            "max_rows": {
                "type": "integer",
                "description": (
                    f"Per-symbol row cap (default {DEFAULT_MAX_ROWS}). A longer series is "
                    "evenly downsampled to fit; narrow the date range or coarsen the "
                    "interval instead of raising it. Use 0 only when the full series is "
                    "required — the result then exceeds the context budget and is "
                    "offloaded to a file."
                ),
                "default": DEFAULT_MAX_ROWS,
            },
        },
        "required": ["codes", "start_date", "end_date"],
    }

    def execute(self, **kwargs: Any) -> str:
        return fetch_market_data_json(
            codes=kwargs["codes"],
            start_date=kwargs["start_date"],
            end_date=kwargs["end_date"],
            source=kwargs.get("source", "auto"),
            interval=kwargs.get("interval", "1D"),
            max_rows=kwargs.get("max_rows", DEFAULT_MAX_ROWS),
        )
