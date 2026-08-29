# SWARM-PRESETS — 多智能体团队预设清单与触发策略

引擎的 swarm 子系统（`agent/src/swarm/`，工具入口 `run_swarm`）按 YAML preset 编排多智能体 DAG：层内并行、层间串行，每个 worker 是独立的 ReAct 循环。本文列出当前全部 29 个 preset 及来财AI 的触发/选择策略。preset 源文件在 `agent/src/swarm/presets/`；调用记录落 `attempt_stats.swarm_runs`（见 [OBSERVABILITY.md](OBSERVABILITY.md) §3.2）。

## 触发策略（来财AI）

- **引擎侧白名单**（`context.py` 系统提示）：仅当 query 明确要求团队/委员会/swarm 分析时才调用 `run_swarm`；用户点名 preset 就传 `preset_name`，否则由关键词打分自动选择（`_resolve_preset`，兜底 `equity_research_team`）；追问「继续/完成报告」不得开新 run。
- **preset 名单的唯一真源是 `agent/src/swarm/presets/*.yaml` 的目录清单**（`_discover_preset_names`），不是关键词表。关键词表只决定「没点名时选谁」，缺一行不影响该 preset 能被点名。
- **关键词打分平分时**优先精确短语命中数更多者（多词英文短语或 3 字以上中文词，如 "funding rate" / "资金费率"；单个泛词如 "crypto"、"macro" 不计），仍平分则按表内顺序。路由置信度随结果返回（`preset_score`：99 = 点名、正数 = 关键词命中、0 = 兜底到 `equity_research_team`）。
- **变量抽取**（`_build_variables`）逐 preset 从 prompt 里读 commodity / crypto 标的 / 方向观点 / 因子族 / 事件类型 / 基金类型等，抽不到才回落到默认值；`commodity_research_team` 与 `crypto_research_lab` 的模板另带 `{goal}`（用户原话），确保回落时 worker 看到的是真实诉求而不是一个自信的错主题。
- **laicai 侧意图声明**（`app/src/server/chat-tools.ts`）：chat 模型通过 `ask_vibe_trading` 的 **`depth="deep_team"` 参数**声明团队研判意图（可选 `swarmPreset` 点名团队），而**不是**把这个意图写成散文塞进 `query`。适用场景仍是两类：① 用户明确要求多视角/团队/委员会式研判；② 重大资金决策（具体金额的加仓/清仓/大类配置）且用户要求全面论证。laicai 服务端据此拼出固定措辞的 swarm 指令追加到 query（引擎侧协议不变），并把 `intent=deep_team` 下发给 router。
- **预算档位的唯一真源是 router 的 `BUDGET_BY_INTENT`**（`standard`=900s / `deep_team`=7200s），租户档 `SWARM_TIMEOUT` env 由同一常量派生。此前这个 7200 被写在四个地方（laicai chat / laicai 作战室 / router env / 引擎默认值）各自漂移。laicai 灰度期仍显式发 `timeoutS`，而 router 让显式值优先——因此只回滚 laicai 就能立刻回到旧预算行为。
- **散文正则只是兜底**：laicai 保留了 swarm 意图正则用于历史线程与用户自己写的散文，声明与嗅探不一致时打一条 `swarm_intent_diverged` 运营事件；分歧率达标后即可删除正则。
- **两层等待的嵌套关系**（决定上一条是否真的可达）：工具自己的等待是 `cap_timeout(SWARM_TIMEOUT, reserve 90s, floor 60s)`；循环侧的写工具看门狗按 `run_swarm` 声明的 `timeout_seconds`（=`SWARM_TIMEOUT + 120s`）计算并自留 60s。**swarm 自留得更多，所以工具必然先于看门狗自收口。** 看门狗曾经按租户档工具超时（300s）计算，于是每次 swarm 在第 600 秒被丢弃、两小时档实际不可达——见 [HISTORY.md](HISTORY.md) 2026-08-29 条目。
- 等待预算耗尽时 run **不取消、后台继续**（`wait_budget_exhausted`），工具返回 run_id 与部分结果；模型可据此如实汇报，或用 `run_swarm(run_id=…)` 继续等**同一个** run（不起新 run、不产生额外 worker token）。
- **失败冷却（F3）**：同一 SwarmTool 实例内某 preset 失败后 **30 分钟**（`_FAILURE_COOLDOWN_SECONDS`）内再调同 preset 直接拒绝（`swarm_preset_cooldown`），并附上次失败 run 已完成 worker 的产物供打捞——systemic 上游故障重跑还是会死，代价是几十分钟。冷却只在工具真的返回过失败时装填，所以它同样依赖上面的嵌套关系成立。

## Preset 清单（29 个，真源 = `agent/src/swarm/presets/*.yaml`）

| preset | 用途 | 结构 |
|---|---|---|
| `investment_committee` | 买卖决策：多空辩手对辩 → 风控审查 → 基金经理终裁 | 辩论型，2 并行 + 2 串行 |
| `equity_research_team` | 个股深度研究报告：宏观 → 行业 → 个股 → 编辑整合 | 4 层串行 |
| `fundamental_research_team` | 基本面三维（财务/估值/质量）并行 → 买方深度报告 | 3 并行 + 汇总 |
| `technical_analysis_panel` | 多流派技术面（经典 TA/一目均衡/谐波/波浪/SMC）并行 → 信号共振评分 | 5 并行 + 聚合，最大编制 |
| `earnings_research_desk` | 财报季：基本面 + 一致预期修正 + 期权事件 + 财报策略 | 并行 + 汇总 |
| `macro_strategy_forum` | 宏观研判：全球 + 国内 + 政策并行 → 首席整合大类配置 | 3 并行 + 汇总 |
| `macro_rates_fx_desk` | 跨资产宏观：全球利率 + 外汇 + 商品通胀 → 宏观组合经理 | 并行 + 汇总 |
| `global_allocation_committee` | 跨市场配置：A股 + 加密 + 港美股并行 → 配置器定权重/再平衡 | 3 并行 + 汇总 |
| `global_equities_desk` | 跨市场选股：A股 + 港美 + 加密分析师 + 全球策略师 | 并行 + 汇总 |
| `sector_rotation_team` | 板块轮动：经济周期 + 景气度 + 资金流 → 轮动策略并回测 | 3 并行 + 汇总 |
| `etf_allocation_desk` | ETF 组合：筛选 + 宏观配置 + 风险预算 → 组合优化并回测 | 3 并行 + 汇总 |
| `fund_selection_panel` | 基金优选（FOF）：量化筛选 → Brinson 归因/风格分析 → 权重优化 | 3 层串行 |
| `portfolio_review_board` | 组合体检：业绩归因 + 风险审查 + 执行质量 → CIO 再平衡决定 | 3 并行 + 汇总 |
| `risk_committee` | 风险审查：回撤 + 尾部风险 + 市场状态并行 → 风控负责人签署 | 3 并行 + 汇总 |
| `event_driven_task_force` | 事件驱动：事件扫描 → 影响深挖 → 策略构建 | 3 层串行 |
| `geopolitical_war_room` | 地缘危机：地缘 + 能源冲击 + 供应链并行 → 应急配置预案 | 3 并行 + 汇总 |
| `sentiment_intelligence_team` | 情绪情报：新闻 + 社媒 + 资金流并行 → 复合情绪分/反转信号 | 3 并行 + 汇总 |
| `social_alpha_team` | 社媒 Alpha：Twitter/Telegram/Reddit 并行 → 可交易情绪因子 | 3 并行 + 汇总 |
| `commodity_research_team` | 商品研究：供需两侧深挖 → 周期策略师投资论点 | 并行 + 汇总 |
| `convertible_bond_team` | 可转债：债底 + 股性 + 期权价值三维并行 → 转债策略 | 3 并行 + 汇总 |
| `credit_research_team` | 信用债：信用资质 + 利率环境 + 行业信用并行 → 固收策略 | 3 并行 + 汇总 |
| `derivatives_strategy_desk` | 期权策略：波动率分析 → 策略设计 → Greeks 风控 | 3 层串行 |
| `factor_research_committee` | 因子研究：挖掘 + 验证并行 → 组合构建 → 回测评审 | 混合 DAG |
| `quant_strategy_desk` | 量化策略：选股 + 因子并行 → 策略回测 → 风控审计 | 混合 DAG |
| `ml_quant_lab` | 机器学习量化：特征工程 + 模型设计并行 → 严格样本外验证 | 并行 + 汇总 |
| `pairs_research_lab` | 配对交易：相关性扫描 + 协整检验并行 → 策略设计 → 微观结构审查 | 混合 DAG |
| `statistical_arbitrage_desk` | 统计套利：配对扫描 + 微观结构并行 → 套利策略 → 风控审查 | 混合 DAG |
| `crypto_research_lab` | 加密研究：链上 + DeFi + 情绪三维并行 → Alpha 汇总 | 3 并行 + 汇总 |
| `crypto_trading_desk` | 加密执行台：资金费率/基差 + 清算/微观结构 + 链上资金流 + 风控（含仓位与执行时机） | 并行 + 汇总 |

工程注意：preset 的 `agents:` 支持逐 agent `model` 覆盖（`ChatLLM(model_name=agent_spec.model_name)`），未配置时继承引擎 env 的 `LANGCHAIN_MODEL_NAME`——需要提速时可给中间层 worker 配轻量模型、仅终裁层保留旗舰模型。
