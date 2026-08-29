# Changelog

All notable changes to Vibe-Trading are documented in this file.
This project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- **Shared context-compression policy (`agent/context_policy.py`).** Layers 1–3
  now read one rule source. Layer 2 used to fold the middle out of exactly the
  grounding results Layer 1 refuses to prune, and out of the Layer 3 handoff
  summary. Rules are graded rather than boolean — the first user message folds
  under a looser budget instead of being exempted outright, and a pathological
  protected result still has a hard-cap escape valve.
- **Off-disk tool results with an explicit truncation envelope
  (`agent/tool_result_store.py`).** Oversized results are written under
  `run_dir/tool-results/` and the model receives a marked head+tail preview
  plus the path and read instructions, instead of a silent `[:10_000]` cut it
  could not detect. File names are deterministic so the preview text — and
  therefore the prompt cache — stays byte-stable across replays.
- **Cross-attempt handoff summaries (`session/handoff.py`).** The Layer 3
  structured summary is persisted the moment it is produced and resumed by the
  next attempt, so decisions compressed away earlier are inherited instead of
  lost. Session history replay is now summary + token-budgeted raw turns, with
  an explicit placeholder for turns that did not fit.
- **Per-(tool, args) circuit breaker.** After
  `VIBE_TOOL_CIRCUIT_FAILURE_LIMIT` (default 3) consecutive identical failures
  the call is refused with an actionable structured error. The duplicate guard
  only ever registered successes, so a dead upstream could repeat until the
  iteration cap.
- **Chinese prompt-injection rules and an `<external-content>` declaration.**
  All five scanner rules gained a Chinese variant, and `read_url` /
  `web_search` / `read_document` bodies are now wrapped in an untrusted-data
  declaration mirroring `<recalled-memories>`, with high-severity findings
  promoted to a banner above the content.

### Changed
- **microcompact hysteresis.** Crossing the trigger arms the layer and cuts
  once, deeper (keep ratio 0.25 → 0.15), staying armed until the estimate
  falls back below 0.35. Replaces a shallow rewrite of the trajectory middle on
  every single turn past the trigger, which rebuilt the provider prompt cache
  each time.
- **`run_swarm` returns conclusions plus pointers.** Per-task summaries are
  previews with a `report_path`, and `final_report` receives the measured
  leftover of the payload budget, so the whole return is valid JSON inside the
  tool-result limit rather than being cut mid-document on the way in.
- **Session history budget is now tokens, not characters.**
  `MAX_HISTORY_CHARS = 12000` ("roughly 3000 tokens") was an English-only
  assumption worth ~7.2k tokens of Chinese. Replaced with
  `MAX_HISTORY_TOKENS = 6000` using the repo's CJK-weighted estimator — this
  unifies the unit and deliberately does NOT also cut the budget.
- **Swarm workers run tools through the main loop's watchdog.** Worker tool
  calls now get the same timeout, heartbeat and budget clamp as the main loop;
  previously a hung tool blocked a worker until the layer deadline expired.
- **Long-term memory hygiene.** Overwriting an entry folds the superseded body
  into the new file under a merge marker instead of destroying it; the index is
  auto-consolidated at run end once it reaches 180 lines.

### Fixed
- **Compaction failure no longer fails the run.** The Layer 3 summary call is
  guarded: on failure the run degrades to the zero-LLM layers and continues. A
  correction mechanism must not be what kills a healthy attempt.
- **`empty_model_response` gets one in-place retry.** A degraded provider turn
  (stream succeeds, no content and no tool calls) used to write off a
  potentially hour-long attempt without a single retry.
- **Swarm worker `tool_result` events report real status.** The status was
  hardcoded `"ok"`, so the swarm panel showed a 0% worker tool error rate
  regardless of what happened.
- **Worker `incomplete` results are retried.** A missing deliverable is the
  failure class most likely to succeed on a second attempt, and leaving it
  unretried blocked every downstream task.
- **Upstream reports injected into downstream workers are budgeted.** A
  multi-upstream role concatenated whole `report.md` documents into its system
  prompt with no limit.
- **A full tenant volume no longer kills the attempt.** `PersistentMemory.add`
  raises `MemoryWriteError`, which `remember` turns into a structured tool
  error; tool-result offload failures degrade to a marked, disk-free preview.
- **Removed the tracked `agent/logs/engine.jsonl`** (164KB, first line leaked
  an internal LLM gateway host) and gitignored `agent/logs/`.

## [0.1.9] — 2026-06-01

### Added
- **Connector-first broker profiles (IBKR + Robinhood).** Trading access now
  starts from a selectable connector profile rather than separate broker/live
  entry points; `vibe-trading connector list/use/check/account/positions/orders/quote/history`
  and the MCP `trading_*` tools share the selected profile, with paper/live as
  a property under the connector. IBKR is usable immediately as a local
  read-only TWS / IB Gateway profile; the official IBKR remote MCP path is
  seeded as an OAuth `mcp.read` probe until stable read tool names ship.
  Robinhood Agentic Trading is a bounded connector behind OAuth, a committed
  mandate, an order guard, an audit ledger, and an instant halt switch.
- **Research Goal runtime.** Long-running, research-only goals with auditable
  checklist criteria, budgets, and a `/goal` CLI command, plus REST + MCP
  endpoints (`start_research_goal`, `get_research_goal`, `add_goal_evidence`,
  `update_research_goal_status`) and a Web `GoalDrawer`.
- **Swarm `retry_run`.** Re-launch a failed/stale/cancelled run with the
  original preset + variables; exposed as both `POST /swarm/runs/{id}/retry`
  and an MCP `retry_run` tool (the `list_runs → retry` loop). 36 MCP tools now.
- **Operator-configured external MCP tools in swarm workers** (#142) and
  **remote MCP transports** for the built-in agent.
- **`mootdx` A-share OHLCV loader** — native 通达信 TCP, no token, sits between
  tushare and akshare in the fallback chain. CCXT loader now reads proxy env
  for restricted networks (#126).
- **Hypothesis Registry CLI** — `list / show / invalidate`.
- **Strict alpha-bench mode** with a mandatory random control (#143).

### Changed
- **CLI split into the `agent/cli/` package** (from a 3216-LOC single file),
  with a refreshed interactive terminal UI (figlet banner + activity rail) and
  a single `cli/_version.py` version source.
- Swarm status reconciles from live task files on every read; `run_swarm`
  sends MCP progress heartbeats, and the stale-run reaper uses per-run
  thresholds (#132).
- Refreshed provider default model ids; bumped `langgraph` for CVE-2026-28277.

### Fixed
- **`--version` no longer drifts (#156).** The version derives from package
  metadata, falling back to reading `pyproject.toml` directly — no hardcoded
  constant left to forget on release.
- **Session running-status indicator** now survives reconnect / page reload /
  sidebar navigation; **swarm DAG** blocks downstream tasks when an upstream
  task fails (#145).
- **Robustness pass:** pre-flight validation for LLM-generated signal engines
  with clean JSON errors (#149), graceful agent-loop exit at the iteration
  budget instead of an output-less `failed` (#148), `flush + fsync` session
  message writes that skip corrupted JSONL lines on read (#147), and IME Enter
  handling in the Web composer (#146).
- **Full Report** link now always renders when a `runId` exists, even cross-browser
  (#150); SSE idle timeout is configurable via `VIBE_TRADING_SSE_TIMEOUT` (#157);
  cross-market correlation normalizes timestamps so crypto-vs-equity pairs align (#158).

## [0.1.8] — 2026-05-17

### Added — Alpha Zoo (450+ pre-built quant alphas)
- `agent/src/factors/` — base operators (`rank`, `scale`, `ts_*`, `delta`,
  `decay_linear`, `signed_power`, `safe_div`, market-aware `vwap`) and a
  registry that AST-extracts metadata from each alpha module without
  importing it. Lookahead is enforced at the operator level
  (`delta(d>=1)`), and registry sanity checks reject `+/-inf` and
  outputs that are more than 95 % NaN.
- 4 zoos shipping 452 alphas total:
  - **qlib158** (154 alphas) — port of Microsoft Qlib's `Alpha158`
    feature handler under Apache-2.0, with pinned commit SHA per file.
  - **alpha101** (101 alphas) — implementation of Kakushadze (2015)
    *"101 Formulaic Alphas"* (arXiv:1601.00991), written from the paper
    appendix; the relevant trademarked string is intentionally absent.
  - **gtja191** (191 alphas) — implementation of Guotai Junan's 2014
    *"191 Short-period Trading Alpha Factors"* research report.
  - **academic** (6 factors) — Fama-French 5 + Carhart momentum, shipped
    as honest price-based proxies (not the canonical FF series).
- `vibe-trading alpha {list,show,bench,compare,export-manifest}` CLI
  subcommand. `show` and `export-manifest` enforce path-traversal guards.
- New agent tools: `AlphaZooTool` (browse) and `AlphaBenchTool`
  (orchestrator with Jinja2 autoescape + strict CSP HTML report).
- `ZooSignalEngine.from_zoo(...)` — composite multi-factor signal engine
  with cross-sectional standardisation, weighting, and optional top-N /
  bottom-N long-short conversion.
- `wiki/scripts/build_alpha_library.py` — Alpha Library renderer.
  Reads `manifest.json` produced by `vibe-trading alpha export-manifest`
  and emits 452 per-alpha HTML pages plus 4 per-zoo overviews, each with
  `script-src 'none'` CSP. The landing page hydrates per-zoo counts
  from `content/index.json`.
- New blog post: *"Which of the 191 GTJA alphas still work in 2026?"*
  with aggregate IC statistics, theme breakdown, and the top alphas
  that survive eight years of out-of-sample data.

### Added — Web UI for Alpha Zoo
- New page at `/alpha-zoo` in the Vite + React frontend with three
  views: browse (4 zoo cards + filter bar + paginated table), detail
  (formula, metadata, collapsible source code), and bench-runner
  (form → SSE-streamed progress + Alive/Reversed/Dead stat cards +
  Top-5-by-IR table + by-theme breakdown chart). "Alpha Zoo" nav
  entry added to the layout.
- Four new REST routes in the FastAPI server:
  - `GET /alpha/list` — filterable alpha catalogue
  - `GET /alpha/{alpha_id}` — meta + source code
  - `POST /alpha/bench` — kicks off a background bench job and
    returns a `job_id`
  - `GET /alpha/bench/{job_id}/stream` — Server-Sent Events with
    `progress`, `result`, `done`, and `error` event types. In-memory
    job state with a 1-hour TTL; no Redis/Celery dependency.
- Bench math is refactored into `agent/src/factors/bench_runner.py`
  so the CLI driver (`agent/scripts/w4a_run_benches.py`) and the new
  API worker share a single implementation.

### Added — Safety floor
- `agent/tests/factors/test_alpha_purity.py` — AST allowlist scan over
  every `zoo/**/*.py` module (whitelist: pandas, numpy, scipy.\*,
  `src.factors.base`, `__future__`, `typing`, `math`, `dataclasses`;
  banned: `os`, `sys`, `subprocess`, `socket`, `urllib`, `requests`,
  `httpx`, `pathlib`, `Path`, `open`, `eval`, `exec`, `compile`,
  `__import__`, and `getattr(_, "__*")`).
- `agent/tests/factors/test_lookahead.py` — sentinel future-row
  injection on a 300-row synthetic panel; corrupting rows after the
  probe must leave the probe value unchanged within 1e-9.
- `tools/ci_grep_gates.sh` — CI gate that rejects `yaml.load(` without
  `safe_load`, any trademarked-name leak in shipped artifacts, and any
  per-stock-code data leak in `wiki/**/*.{json,csv,html}`.
- `agent/tests/factors/conftest.py` — opt-in `pytest-socket` integration
  that hard-fails any test attempting outbound network during the
  factors test suite.

### Added — Community governance
- `CONTRIBUTING.md` — Developer Certificate of Origin sign-off
  requirement and a contributor checklist for new alpha PRs (purity,
  lookahead, `__alpha_meta__` shape, LaTeX-matches-code, per-zoo
  LICENSE.md, DCO).
- `NOTICE` (repo root) — Apache-2.0 attribution for Qlib and a
  declaration that the bundled formulas from Kakushadze, GTJA, and the
  academic baselines are mathematical content (paper prose, tables, and
  figures are not reproduced here).
- Per-zoo `LICENSE.md` for each of `qlib158/`, `alpha101/`, `gtja191/`,
  and `academic/`, plus an upstream `NOTICE` for `qlib158/`.

### Changed
- `agent/src/tools/factor_analysis_tool.py` extracted its IC/IR and
  layered-backtest helpers to `agent/src/factors/factor_analysis_core.py`
  so the new `alpha_bench_tool` reuses the same maths. Public tool
  signature is unchanged; `_compute_ic_series` and `_compute_group_equity`
  remain importable as backward-compatible aliases.
- `agent/cli.py` grew by 7 lines to register the `alpha` subcommand;
  all handler logic lives in `agent/src/factors/cli_handlers.py`.
- Packaging: `pyproject.toml` now ships `zoo/**/*.yaml`, `zoo/**/*.md`,
  and `zoo/**/NOTICE` as package data; `MANIFEST.in` recursively
  includes `agent/src/factors`.

### Known limitations
- The `btc-usdt` universe is single-asset; cross-sectional IC requires
  ≥2 instruments, so the bundled `alpha101_btc` bench run returns
  alive/reversed/dead = 0/0/0 by construction. Use a multi-symbol crypto
  basket (e.g. BTC + ETH + SOL + the top-N perpetuals) for meaningful
  cross-sectional results; a curated `crypto-majors` universe is planned
  for 0.2.

### Internal
- `wiki/alpha-library/manifest.json` and `wiki/alpha-library/content/`
  are generated artifacts and gitignored. Run
  `vibe-trading alpha export-manifest --out wiki/alpha-library/manifest.json
  --force` followed by `python wiki/scripts/build_alpha_library.py` to
  regenerate the static site.
