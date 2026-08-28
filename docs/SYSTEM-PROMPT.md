# SYSTEM-PROMPT — 提示词体系

本文是引擎提示词体系的**现状**技术文档：主 Agent（ReAct 循环）与 swarm worker 两套系统提示词分别由什么拼装、包含哪些块、记忆与缓存如何协作。Skill 体系（提示词的主要「知识供给方」）见 [SKILLS.md](SKILLS.md)；29 个 swarm preset 的清单与触发策略见 [SWARM-PRESETS.md](SWARM-PRESETS.md)。

## 目录

1. [总览：两套提示词](#1-总览两套提示词)
2. [主 Agent 系统提示词](#2-主-agent-系统提示词)
3. [记忆注入与 prompt cache](#3-记忆注入与-prompt-cache)
4. [Swarm worker 系统提示词](#4-swarm-worker-系统提示词)
5. [Preset YAML 中的角色提示词](#5-preset-yaml-中的角色提示词)
6. [与来财主站的边界](#6-与来财主站的边界)

## 1. 总览：两套提示词

| | 主 Agent | Swarm worker |
|---|---|---|
| 拼装代码 | `agent/src/agent/context.py` `ContextBuilder.build_system_prompt()` | `agent/src/swarm/worker.py` `build_worker_prompt()` |
| 模板来源 | 模块级常量 `_SYSTEM_PROMPT` | preset YAML 的 `system_prompt` 字段 + 代码内固定块 |
| Skill 可见范围 | 全部 79 个（一行摘要） | 按角色 `skills` 白名单过滤 |
| 工具可见范围 | 全量 ToolRegistry | 按角色 `tools` 白名单过滤 |
| 记忆 | 持久记忆快照 + auto-recall | 无（靠 Upstream Context / Ground Truth） |

两者都遵循同一原则：**系统提示只放摘要，重知识靠 `load_skill` 按需加载**（渐进式披露，见 [SKILLS.md](SKILLS.md) §2）。

## 2. 主 Agent 系统提示词

模板在 `agent/src/agent/context.py` 顶部的 `_SYSTEM_PROMPT`，`build_system_prompt()` 每会话填充。块结构依次为：

| 块 | 内容 | 来源 |
|---|---|---|
| 身份声明 | 「finance research agent，{N} skills / {N} tools / 11 数据源 / 29 swarm 团队」 | skill/tool 数量动态计数 |
| `## Tools` | 全量工具名 + 描述 + 参数列表 | `_format_tool_descriptions()` 遍历 ToolRegistry |
| `## Skills` | 按 category 分组的一行摘要，提示用 `load_skill` 读全文 | `SkillsLoader.get_descriptions()` |
| `## State` | 工作区内存摘要（run_dir 内已有文件等） | `WorkspaceMemory.to_summary()` |
| `## Task Routing` | 五条工作流路由（见下） | 模板固定文本 |
| `## Guidelines` | 输出与行为守则（见下） | 模板固定文本 |
| `## Persistent Memory` | 跨会话记忆快照，**有记忆才渲染** | `PersistentMemory.snapshot` |
| `## Current Date & Time` | 本地当前时间 | `datetime.now()` |

**Task Routing 五条路由**（模型据此选工作流）：

1. **Backtest** — `load_skill("strategy-generate")` → `write_file("config.json")` → `write_file("code/signal_engine.py")` → 语法检查 → `backtest(run_dir=…)` → 读 `artifacts/metrics.csv`。明确禁止自写 run_backtest.py（引擎内置）。
2. **Swarm** — 仅当用户明确要求团队/委员会/swarm 分析才调 `run_swarm`；点名 preset 就传 `preset_name`，否则由引擎自动选择；「继续/完成报告」类追问**不得**用片段开新 swarm，应复用上次 run 或带原始完整请求重跑；调 `run_swarm` **前**先用 get_market_data / web_search 取关键实时数据并把要点折进 prompt（worker 只能看到 prompt 与自动 grounding 携带的内容，自由体宏观 prompt 根本没有自动 grounding）；swarm run 失败**不得**立即重跑同 preset（系统性上游故障会再杀掉它、白烧几十分钟），应就地打捞已完成 worker 的 `tasks`/`final_report` 产出、自行补缺后作答。
3. **Analysis / research** — 先 load 相关 skill，再用对应工具（factor_analysis / options_pricing / bash 自写脚本）。
4. **Document / web** — PDF 用 `read_document`，网页用 `read_url`。
5. **Trade Journal → Shadow Account** — 交割单分析走 `trade-journal` skill + `analyze_trade_journal`；用户追问「怎么做得更好」切 Shadow Account 流：**必须先 `load_skill("shadow-account")` 才能碰任何 `shadow_*` 工具**（extract → confirm → backtest → render，扫描信号必附 research-only 免责声明）。

**Guidelines 要点**：任务前先 load skill；缺关键信息（标的/日期/策略类型）要问、不许猜；多行数据一律 markdown 管道表格；禁用 `---` 水平线（两端渲染都丑）、用 `##`/`###` 分节；回测后必报 total_return / sharpe / max_drawdown / trade_count；路径相对 run_dir；跟随用户语言；可用 `remember` 存跨会话记忆、`save_skill`/`patch_skill` 沉淀与修复技能。

## 3. 记忆注入与 prompt cache

记忆走两条通道，刻意分开以保 prompt cache（`ContextBuilder.build_messages()`）：

- **系统提示通道（会话内稳定）**：`PersistentMemory.snapshot` 在会话开始时冻结，整个会话不变 → 系统提示逐轮字节一致，provider 的 prompt cache 可命中。
- **user message 通道（逐查询变化）**：每轮对当前 user message 做 `find_relevant(…, max_results=3)`，命中的记忆以 `<recalled-memories>` 块前置拼进 user message。相关性召回不污染系统提示。

召回失败静默降级（debug 日志），不阻塞对话。

## 4. Swarm worker 系统提示词

`build_worker_prompt()`（`agent/src/swarm/worker.py`）按顺序拼接，无模板文件：

1. **`## Role`** — preset 里该 agent 的 `role` 一行。
2. **角色 `system_prompt`** — preset YAML 正文，`{upstream_context}` 占位符替换为上游 agent 摘要块（`## Upstream Context` + 按 context_key 分节）。
3. **`## Available Skills`** — 按该角色 `skills` 白名单过滤后的一行摘要（`_filter_skill_descriptions()`）；无匹配则整块省略。
4. **Ground Truth 块**（可选）— `src/swarm/grounding.py` 在 `user_vars` 给出明确标的时预取真实近期价格渲染成 markdown，放在执行规则**之前**，让 worker 规划第一次工具调用时就在作用域内。自带「优先用这些价格、别用训练数据」的指令。
5. **`## Market Data Tool Policy`**（仅当角色工具含 `get_market_data`）— OHLCV/指标/收益计算先调 `get_market_data`（走仓库 loader 层、符号规范化、坏行清洗、严格 JSON），裸 yfinance 脚本只用于 OHLCV 覆盖外的字段（基本面/持股/期权/公司元数据）。
6. **`## Data Citation Discipline (HARD RULE)`**（无条件注入）— 输出中的每个具体数字（价格/百分比/成交量/资金流/市值排名/板块权重/ETF 代码/推荐标的）必须可溯源到：(a) 本次 run 的工具结果、(b) Ground Truth 块、(c) 上游上下文（且上游自身源于 a/b）。不许引用训练数据——「市场早已变化，你记得的任何具体价格默认是错的」。补不上就要么调工具取，要么删数字并标注「方向性判断、未经实时数据验证」。**对没有数据工具的汇总/编辑角色同样生效**：上游没给的数字不许自己编。
7. **`## Execution Rules`** — 硬上限 20 次工具调用，三阶段：Phase 1 计划（0 次调用，先列 3-5 条 bullet）；Phase 2 执行（≤15 次：先 `load_skill`，`write_file` 写一个聚焦脚本再 `bash python` 跑、禁止在 bash 里写长代码、禁止 curl/requests 取数、脚本失败最多重试 2 次）；Phase 3 总结（**必须** `write_file` 产出 `report.md`，含具体数字/日期/可操作结论，再输出 2-3 句摘要，语言跟随任务 prompt）。
8. **`## Current Date & Time`**。

Ground Truth 与 Data Citation Discipline 是两道防幻觉闸：前者只在 user_vars 有明确标的时渲染，后者兜底所有自由格式 prompt（「看看 A 股短线情绪」这类没有标的的请求，否则 worker 会引用训练数据里的价格和板块权重）。

## 5. Preset YAML 中的角色提示词

29 个 preset（`agent/src/swarm/presets/*.yaml`）共 113 段角色 `system_prompt`。每个 agent 定义：

```yaml
- id: bull_advocate
  role: Bull-side Researcher
  system_prompt: |
    （角色使命 + ## Task + ## 分析维度 + ## Required outputs，
     正文里显式指示 load_skill("technical-basic") 等取方法论）
  tools: [bash, read_file, write_file, load_skill, get_market_data, factor_analysis]
  skills: [technical-basic, fundamental-filter, yfinance, ...]
  max_iterations: 50
  timeout_seconds: 1800
  max_retries: 1
```

角色 prompt 的通用写法：身份/立场 → 任务（`{target}` / `{market}` 等 user_vars 模板变量）→ 分析维度（每个维度点名要 load 的 skill）→ 编号的必交产出清单。`tools`/`skills` 白名单同时约束 ToolRegistry 与提示词里的 skill 摘要，worker 看不到白名单外的任何东西。

`max_iterations` 是 ReAct 循环的代码侧上限；提示词里的「20 次工具调用」是行为约束——前者兜底，后者塑形。

preset 全量清单与结构见 [SWARM-PRESETS.md](SWARM-PRESETS.md)。

## 6. 与来财主站的边界

- 来财 `chat_threads.systemPrompt` 是用户自定义**聊天**提示词在建线程时的快照，属于主站 chat 模型，与引擎无关。
- 主站到引擎的唯一提示词接触面是 `ask_vibe_trading` 透传工具的三档指令（must-call / continuity / on-demand，`app/src/server/chat-handler.ts`）——它决定主站模型**何时调用**引擎，不改变引擎内部任何提示词。
- 引擎侧提示词全部在引擎进程内生成：主 Agent 由 `context.py`，swarm worker 由 `worker.py`。router（cube-router）不注入提示词。
