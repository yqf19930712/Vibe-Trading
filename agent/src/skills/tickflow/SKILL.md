---
name: tickflow
description: TickFlow US-equity daily OHLCV via structured REST (api.tickflow.org) — China-direct, no egress proxy needed, forward-adjusted klines. Requires TICKFLOW_API_KEY. The go-to fallback when yfinance is rate-limited.
category: data-source
---
# TickFlow

## Overview

TickFlow (https://docs.tickflow.org) is a structured REST market-data service.
**This deployment's key covers US equities, daily-K only** (the service itself
also offers A-shares/HK/minute data on paid tiers). Key properties:

- **China-direct endpoint** — `api.tickflow.org` is reachable from the mainland
  without the egress proxy/tunnel. When yfinance returns empty or 429 (Yahoo
  rate-limits the shared exit IP), switch here instead of retrying yfinance.
- **Structured column-oriented JSON** — no parsing heuristics needed.
- **Forward-adjusted** (`adjust=forward`) daily bars with full history.

The project has a built-in DataLoader (`backtest/loaders/tickflow_loader.py`),
registered as `source: "tickflow"` and part of the `auto` US-equity chain
(`ifind → tickflow → yfinance → akshare` — second, right after ifind).

## Quick Start

Preferred OHLCV tool call (routes through the loader layer):

```json
{
  "codes": ["INTC.US", "NVDA.US"],
  "start_date": "2026-01-01",
  "end_date": "2026-08-25",
  "source": "tickflow",
  "interval": "1D"
}
```

Script usage — use the loader, NOT raw HTTP (it handles auth, the Cloudflare
User-Agent quirk, timestamp semantics, and 429 retry):

```python
from backtest.loaders.registry import get_loader_cls_with_fallback

loader = get_loader_cls_with_fallback("tickflow")()
data = loader.fetch(["INTC.US"], "2026-01-01", "2026-08-25", interval="1D")
for symbol, df in data.items():
    print(symbol, df.tail())   # index=trade_date, columns=open/high/low/close/volume
```

## Symbol Format

Identical to the project convention — no conversion needed: `AAPL.US`,
`INTC.US`. Non-`.US` symbols are skipped by the loader (free tier is US-only).

## Raw HTTP Reference (only when the loader can't serve you)

```
GET https://api.tickflow.org/v1/klines?symbol=INTC.US&period=1d&adjust=forward
    &start_time=<ms>&end_time=<ms>&count=10000
Headers: x-api-key: $TICKFLOW_API_KEY
         User-Agent: vibe-trading/1.0     # REQUIRED — see pitfalls
```

Response is column-oriented: `{"data": {"timestamp": [...ms], "open": [...],
"high": [...], "low": [...], "close": [...], "volume": [...], "amount": [...]}}`.
US bars are stamped at 04:00/05:00 UTC (US-midnight ET) — **the UTC calendar
date IS the trade date**, DST-safe. Periods: `1d/1w/1M/1Q/1Y` (minute data is
A-share-only on the service). Errors come as `{"code": "...", "message": "..."}`
with 400/401/403/404/429.

## Pitfalls

- **Cloudflare blocks the default Python-urllib User-Agent** (HTTP 403,
  `error code: 1010`). ANY explicit `User-Agent` header passes. The loader
  already sets one; raw `urllib`/`requests` scripts must too.
- **Rate limit (free tier)**: 10 kline requests/min, 1 symbol per request.
  The loader retries a 429 once after ~6.5s. For >10 symbols expect the fetch
  to be throttled — don't hammer in a loop; `yfinance` picks up the remainder.
- **Daily-K only** here: for intraday/minute US data this source cannot help.
- Check availability with the `TICKFLOW_API_KEY` env var. On persistent
  401/403 the key may have expired — report it instead of retrying.
