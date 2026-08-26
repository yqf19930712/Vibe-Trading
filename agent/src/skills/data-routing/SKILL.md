---
name: data-routing
category: data-source
description: Data source selection decision tree. Load this skill BEFORE any backtest or data-fetching task to choose the best available data source.
---

## Data Source Overview

| Source | Markets | Auth Required | Network | Skill |
|--------|---------|---------------|---------|-------|
| tushare | A-shares, funds, futures, macro | Yes (`TUSHARE_TOKEN`) | China network | tushare |
| akshare | A-shares, US, HK, futures, macro, forex | No | Unrestricted | akshare |
| yfinance | US stocks, HK stocks, ETFs | No | Needs Yahoo Finance access | yfinance |
| tickflow | US stocks (daily K only) | Yes (`TICKFLOW_API_KEY`) | China-direct, no proxy needed | tickflow |
| ifind | US & HK daily K + global-stock financials/events | Yes (`IFIND_MCP_TOKEN`) | China-direct, no proxy needed | ifind |
| tencent | A-shares, HK stocks | No | China-direct | — (runner-integrated) |
| okx | Crypto (OKX exchange) | No | Needs okx.com access | okx-market |
| ccxt | Crypto (100+ exchanges) | No | Needs exchange access | ccxt |

## Decision Tree

### Backtest Scenario (writing config.json)

Use `source: "auto"` — the runner automatically routes by symbol pattern and falls back to alternative sources if the primary one is unavailable.

You do NOT need to specify a concrete data source in config.json unless the user explicitly asks for one.

### Analysis / Research Scenario (writing Python scripts)

1. Identify the market type from the user's request
2. Pick the source by priority:

**A-shares**: tushare (if TUSHARE_TOKEN is set) > akshare (free fallback)
**US stocks**: tickflow > ifind > yfinance > akshare — tickflow and ifind are
China-direct (no egress proxy) and go first (tickflow leads: structured and
fast; ifind is the NL backup); yfinance rides the egress proxy and Yahoo
rate-limits the shared exit IP, so it is a fallback only — when it returns
empty or 429, do NOT retry it in a loop.
**HK stocks**: ifind > tickflow > yfinance > tencent > akshare (tickflow's
current plan is US-only, so ifind leads for HK; tickflow no-ops for .HK)
**Crypto**: okx (single exchange) > ccxt (multi-exchange)
**Futures**: tushare > akshare
**Macro / economics**: akshare > tushare
**Forex**: akshare > yfinance

3. Load the corresponding skill for API details: `load_skill("akshare")`

### Availability Check

- **tushare**: check if `TUSHARE_TOKEN` environment variable exists
- **tickflow**: check `TICKFLOW_API_KEY`; free tier is rate-limited to 10 req/min
  (1 symbol/req) and covers US daily K only — the loader retries a 429 once
- **ifind**: check `IFIND_MCP_TOKEN`; US/HK daily K only, natural-language
  backend so date ranges must be explicit
- **yfinance / okx / ccxt / akshare**: free but may have network restrictions
- If the user reports "connection timeout" or "cannot access", switch to the same-market fallback

### US realtime quotes

For US realtime snapshots use the **`get_realtime_quotes` tool** (TickFlow
`/v1/quotes`: last price, change, OHLC, volume; includes recent IPOs that
Tencent's endpoint lacks). Do NOT bash-curl quote websites for US symbols.

### Using tickflow / ifind in analysis scripts

Both are wired into `get_market_data` and the backtest runner (`source: "auto"`
walks the chain; `source: "tickflow"` / `source: "ifind"` pin them). For
scripts, load the dedicated skill for API details and quirks:
`load_skill("tickflow")` / `load_skill("ifind")` — prefer the loader classes
over raw HTTP (they handle auth, sessions, parsing, Cloudflare UA, retries).

## Symbol Format Reference

| Market | Format | Examples |
|--------|--------|---------|
| A-shares | `NNNNNN.SZ/SH/BJ` | 000001.SZ, 600000.SH |
| US stocks | `TICKER.US` | AAPL.US, MSFT.US |
| HK stocks | `NNN(N).HK` | 700.HK, 9988.HK |
| Crypto | `SYMBOL-USDT` | BTC-USDT, ETH-USDT |
| Futures | `XXNNNN.EXCHANGE` | CU2406.SHFE |
| Forex | `XXX/YYY` | USD/CNY, EUR/USD |

## Fallback Chain (Runner Layer)

The backtest runner implements automatic fallback at the market level:

```
User requests INTC.US (US stock)
  -> detect market: us_equity
  -> try tickflow: TICKFLOW_API_KEY set -> use tickflow
  -> (if tickflow empty/429) try ifind -> yfinance -> akshare
  -> success (zero config required)
```

Current chains: us_equity = tickflow → ifind → yfinance → akshare;
hk_equity = ifind → tickflow → yfinance → tencent → futu → akshare;
a_share = tushare → mootdx → baostock → tencent → akshare.

This is transparent to the user — they just see results.
