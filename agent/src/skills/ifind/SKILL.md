---
name: ifind
description: 同花顺 iFinD MCP financial data — US/HK daily OHLCV backup source plus natural-language queries for global-stock profiles, financials, and corporate events. China-direct, no egress proxy needed. Requires IFIND_MCP_TOKEN.
category: data-source
---
# iFinD (同花顺 MCP)

## Overview

iFinD MCP (https://mcp.51ifind.com) is 同花顺's MCP-protocol financial data
service. **China-direct endpoint** (`api-mcp.51ifind.com:8643`) — reachable
without the egress proxy, which makes it a US/HK backup when yfinance is
rate-limited. Two distinct uses:

1. **Primary US/HK OHLCV source** — the built-in DataLoader
   (`backtest/loaders/ifind_loader.py`, `source: "ifind"`) serves US & HK
   **daily** bars and sits FIRST in the `auto` chains
   (US: `ifind → tickflow → yfinance → akshare`;
   HK: `ifind → tickflow → yfinance → tencent → futu → akshare`).
2. **Beyond-OHLCV lookups** — natural-language tools for 港美股 profiles,
   financials (ROE/ROA/growth), and corporate events (IPO/回购/分红) that no
   other integrated source provides for US/HK.

## OHLCV Quick Start

Preferred tool call:

```json
{
  "codes": ["INTC.US", "03690.HK"],
  "start_date": "2026-06-01",
  "end_date": "2026-08-25",
  "source": "ifind",
  "interval": "1D"
}
```

Script usage — always via the loader (it owns the MCP session, the nested-JSON
markdown-table parsing, and the non-trading-row cleanup):

```python
from backtest.loaders.registry import get_loader_cls_with_fallback

loader = get_loader_cls_with_fallback("ifind")()
data = loader.fetch(["INTC.US", "03690.HK"], "2026-06-01", "2026-08-25")
```

Daily-only: other intervals return `{}` so the chain moves on.

## Natural-Language Tools (global_stock server)

For profiles/financials/events, call the MCP server directly. Protocol:
JSON-RPC over POST, `Authorization` header carries the **raw token** (no
`Bearer` prefix), session via `Mcp-Session-Id` header from `initialize`.

```python
import json, os, urllib.request

URL = "https://api-mcp.51ifind.com:8643/ds-mcp-servers/hexin-ifind-ds-global-stock-mcp"
TOKEN = os.environ["IFIND_MCP_TOKEN"]

def post(payload, session=None):
    req = urllib.request.Request(URL, data=json.dumps(payload).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json, text/event-stream")
    req.add_header("Authorization", TOKEN)
    if session: req.add_header("Mcp-Session-Id", session)
    resp = urllib.request.urlopen(req, timeout=60)
    sid = resp.headers.get("Mcp-Session-Id")
    raw = resp.read().decode()
    if "text/event-stream" in resp.headers.get("Content-Type", ""):
        raw = [l[5:] for l in raw.splitlines() if l.startswith("data:")][-1]
    return (json.loads(raw) if raw.strip() else None), sid

_, sid = post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-03-26", "capabilities": {},
    "clientInfo": {"name": "script", "version": "1.0"}}})
post({"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)

res, _ = post({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
    "name": "global_stock_financial",
    "arguments": {"query": "英特尔(INTC) 最新报告期的ROE、毛利率、营收同比增速"}}}, sid)
text = res["result"]["content"][0]["text"]        # JSON string
outer = json.loads(text)                          # {"code":1,"msg":"success","data":"..."}
answer = json.loads(outer["data"])["answer"]      # markdown table
```

Available tools on this server: `global_stock_profile`（基本资料/股本）,
`global_stock_quotes`（行情, the loader uses this）, `global_stock_financial`
（财务/估值）, `global_stock_events`（公告事件）, `search_global_stocks`（选股）.
Other servers exist at the same base (`hexin-ifind-ds-{stock,fund,edb,news,bond,index,futures}-mcp`)
covering A-share data — but tushare/akshare are preferred there.

## Query Rules

- Queries are natural language; **always give explicit date ranges** like
  `20260601-20260825` — never relative phrases like “过去20个交易日”.
- One security per query keeps the answer table parseable; batch queries
  return merged tables that are harder to consume.
- The `answer` is a markdown table. Expect: a currency summary table BEFORE
  the data table; non-trading rows carrying the previous close with other
  cells as a literal tab; volumes formatted with 万/亿 units; prices in the
  security's native currency.

## Pitfalls

- Availability = `IFIND_MCP_TOKEN` env var set. On 401 or quota errors, the
  subscription may have lapsed — report, don't loop.
- The MCP session can expire; re-run `initialize` on 400/401/404 (the loader
  does this automatically).
- The NL backend is slower (~3–8s per query) and less deterministic than
  structured sources — if a query comes back unparsable, fall through to
  `tickflow`/`yfinance` instead of rephrasing in a loop.
