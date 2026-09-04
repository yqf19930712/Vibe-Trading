# SKILLS — 技能体系与全量清单

本文是引擎 skill 体系的**现状**技术文档：加载机制、渐进式披露、自进化工具（save/patch/delete/skill_file），以及全部 79 个内置 skill 的分类清单。skill 如何进入提示词见 [SYSTEM-PROMPT.md](SYSTEM-PROMPT.md)；swarm 角色如何按白名单引用 skill 见 [SWARM-PRESETS.md](SWARM-PRESETS.md)。

## 目录

1. [目录结构与加载](#1-目录结构与加载)
2. [渐进式披露](#2-渐进式披露)
3. [自进化：skill 的增删改](#3-自进化skill-的增删改)
4. [Swarm 中的 skill 白名单](#4-swarm-中的-skill-白名单)
5. [全量清单（79 个）](#5-全量清单79-个)

## 1. 目录结构与加载

加载器：`agent/src/agent/skills.py` `SkillsLoader`。两个来源，**用户目录优先、同名覆盖内置**：

| 来源 | 路径 | 说明 |
|---|---|---|
| 用户 skill | `~/.vibe-trading/skills/user/<name>/` | `save_skill`/`patch_skill` 的产物，先加载 |
| 内置 skill | `agent/src/skills/<name>/`（79 个） | 随包分发 |

每个 skill 是一个目录，`SKILL.md` 必须存在，frontmatter 携带 `name` / `description` / `category`；正文是完整方法论文档。可选子目录：`references/`、`templates/`、`examples/`、`assets/`，通过 `Skill.load_support_file()` 按需读取。

> 注意：`agent/skills/`（`src` 外层）不在默认加载路径——`SkillsLoader` 的默认 `skills_dir` 解析为 `agent/src/skills/`。当前 `agent/skills/ashare-mootdx` 只有 references、无 SKILL.md，属于游离文件，引擎不会加载它。

## 2. 渐进式披露

系统提示只承载**一行摘要**，全文按需加载，这是 79 个 skill 不炸上下文的关键：

- **摘要注入**：`get_descriptions()` 按 category 分组渲染（顺序 `data-source → strategy → analysis → asset-class → crypto → flow → tool → 其余按字母`），每个 skill 一行 `- name: description`。
- **全文加载**：模型调 `load_skill(name)` 工具（`agent/src/tools/load_skill_tool.py`）→ `SkillsLoader.get_body()` 取 SKILL.md 正文，工具返回 **Markdown 原文**，首行 `# skill: <name>`（不是 JSON，也没有 `<skill>` 包裹——单行 JSON 落盘后 `read_file` 按行翻页永远翻不到被裁掉的部分）。未命中时回退到用户目录磁盘查找（覆盖会话中途新建的 skill），仍未命中则返回 `{"status":"error","error":"Error: Unknown skill '…'. Available: …"}` JSON 信封（主循环与 swarm worker 的错误分类器都按该信封判失败）。`get_content()`（`<skill name="…">` XML 包裹）只剩 `mcp_server.py` 的 MCP 工具在用。
- **长 skill 的截断规则**（`agent/src/agent/tool_result_store.py`）：`load_skill` 豁免通用的 10k 字符上限，预算 `SKILL_RESULT_LIMIT=60000`（79 个内置 skill 里 27 个超 10k，`tushare` 约 100k）。超预算时**按 `##` 小节裁**、绝不裁在句中：保留能装下的连续前缀小节，信封列出每个被省略小节的标题与起始行号、全文落盘路径（`run_dir/tool-results/<iter>-load_skill-<callid8>.md`）与 `read_file(offset, limit)` 续读提示；围栏代码块里的 `##` 不算分节点。落盘失败时改指向内置的 `<name>/SKILL.md`（行号偏移 frontmatter）。
- **主 Agent 的行为约束**：Guidelines 要求「任务开始前先 load 相关 skill」；Shadow Account 流更是硬规则——不先 `load_skill("shadow-account")` 不许碰 `shadow_*` 工具。

## 3. 自进化：skill 的增删改

四个工具（`agent/src/tools/skill_writer_tool.py`），全部只写用户目录、不碰内置文件：

| 工具 | 行为 | 关键语义 |
|---|---|---|
| `save_skill` | 新建/整体覆盖用户 skill | name 清洗为 `[a-z0-9-]` slug；content 缺 frontmatter 时自动补（category 默认 `user`）；description 要求新 skill 正文含 Related 段、链接 ≥2 个相关已有 skill |
| `patch_skill` | 对现有 skill 做精确查找替换（1 次） | **copy-on-write**：目标是内置 skill 时先整份复制到用户目录再打补丁——此后用户版永久覆盖内置版 |
| `delete_skill` | 整目录删除 | 仅限用户 skill，内置不可删；删除被 patch 出来的用户副本即回退到内置版 |
| `skill_file` | 辅助文件管理（write / remove / list） | 仅限 `references/` `templates/` `examples/` `assets/` 四个子目录，skill 须已存在 |

典型闭环：某数据源 API 变更 → 回测脚本报错 → 模型 `patch_skill` 修正 skill 里的示例代码 → 后续会话（含 swarm worker）加载的即是修好的版本。

## 4. Swarm 中的 skill 白名单

swarm worker 不看全量 skill：preset 里每个 agent 声明 `skills: [...]` 白名单，`_filter_skill_descriptions()`（`agent/src/swarm/worker.py`）只把白名单内的摘要注入该 worker 的系统提示；无匹配则整块省略。角色 prompt 正文通常显式指示「用 `load_skill("technical-basic")` 取方法论」，skill 体系与角色提示词在此交汇。

## 5. 全量清单（79 个）

按 frontmatter `category` 分组。描述为意译摘要，权威定义以各 `SKILL.md` frontmatter 为准。

### data-source（9）— 数据源接入与路由

| skill | 说明 |
|---|---|
| `data-routing` | 数据源选择决策树；任何回测/取数任务**先读本 skill** |
| `tushare` | A股/基金/期货/数字货币行情与基本面主源（需 `TUSHARE_TOKEN`） |
| `akshare` | 免费聚合源（A股/美股/港股/期货/宏观/外汇），tushare 与 yfinance 的首选回退 |
| `yfinance` | Yahoo Finance 全球行情 + 财务 + 内部人/机构持股（美股/港股/ETF/指数），免 key |
| `mootdx` | 通达信 TCP 直连 A 股行情，免 key 无 IP 限频；akshare 东财抓取被限流时的稳定回退 |
| `okx-market` | OKX V5 REST 加密行情：现货/衍生品/指数、K线、资金费率、持仓量，免认证 |
| `ccxt` | 100+ 交易所统一加密行情库；OKX 不可用时回退 |
| `tickflow` | TickFlow 结构化 REST 美股日线（api.tickflow.org，前复权 K 线），国内直连免隧道，需 `TICKFLOW_API_KEY`；yfinance 被限频时的首选回退 |
| `ifind` | 同花顺 iFinD MCP 金融数据：美/港股日线备源 + 全球个股概况/财务/公司事件自然语言查询，国内直连免隧道，需 `IFIND_MCP_TOKEN` |

### strategy（17）— 信号引擎与策略写法

| skill | 说明 |
|---|---|
| `strategy-generate` | 策略创建/修改/优化 + SignalEngine 合约，回测工作流第一步 |
| `technical-basic` | 核心技术指标（EMA/ADX + BB/RSI + OBV/量比）三维投票复合信号 |
| `candlestick` | 15 种经典 K 线形态识别（单/双/三根 + 趋势确认），纯 pandas 向量化 |
| `chanlun` | 缠论形态引擎（czsc 库）：分型/笔/中枢 + 一二三类买卖点，多周期 |
| `smc` | Smart Money Concepts（ICT）：BOS/ChoCH/FVG/订单块 |
| `elliott-wave` | 艾略特波浪：Zigzag 摆动点 + 5浪/3浪结构 + 斐波那契验证 |
| `harmonic` | 谐波形态：Gartley/Bat/Butterfly/Crab XABCD 结构 + PRZ 信号 |
| `ichimoku` | 一目均衡表五线体系：转换/基准交叉、云层位置、迟行确认 |
| `multi-factor` | 多标的横截面多因子打分（标准化 + 等权/IC 加权 + TopN 组合） |
| `pair-trading` | 配对交易：价差/比率 Z-score 均值回归，≥2 标的 |
| `volatility` | 波动率策略：历史波动率百分位均值回归 |
| `seasonal` | 季节性/日历效应策略（月份效应、星期效应等） |
| `ml-strategy` | sklearn walk-forward 机器学习预测策略（特征工程 + 信号生成） |
| `event-driven` | 事件驱动策略：新闻/公告/宏观事件情绪打分，LLM 当 NLP 引擎，CSV 事件 schema |
| `minute-analysis` | 分钟级数据分析与回测（OKX/Tushare/yfinance 分钟 K） |
| `cross-market-strategy` | 跨市场组合（A股+加密、股票+外汇等）的 signal_engine.py 写法 |
| `execution-model` | 执行建模（仅回测）：滑点公式（线性/平方根冲击）、VWAP/TWAP、冲击成本 |

### analysis（17）— 分析方法论

| skill | 说明 |
|---|---|
| `factor-research` | 因子研究框架：IC/IR、分位数回测、因子合成 |
| `risk-analysis` | 风险度量与压力测试：VaR/CVaR/最大回撤、蒙特卡洛、极值尾部、历史情景 |
| `quant-statistics` | 量化统计：ADF/协整检验、GARCH、回归诊断、Bootstrap、假设检验 |
| `valuation-model` | 估值方法论：DCF/DDM/SOTP 绝对估值 + PE-Band/PB-ROE/EV-EBITDA 相对估值 + 估值陷阱 |
| `performance-attribution` | 业绩归因：Brinson 行业/选股归因、因子 alpha/beta 分解、择时评估 |
| `macro-analysis` | 宏观周期定位与央行政策解读（GDP/CPI/PMI/利率/汇率 → 大类资产倾斜） |
| `global-macro` | 全球宏观框架：央行政策传导、汇率预测、地缘风险、资本流动 → 跨资产宏观因子 |
| `market-microstructure` | 市场微观结构：买卖价差、订单流毒性（VPIN/Kyle λ）、流动性度量、A股集合竞价/大宗机制 |
| `behavioral-finance` | 行为金融：过度/反应不足、动量反转的行为解释、情绪周期、认知偏差清单 |
| `sentiment-analysis` | 市场情绪：恐贪指数/Put-Call Ratio/两融/北向信号、社媒舆情量化框架 |
| `correlation-analysis` | 相关性与协整：联动挖掘、板块聚类、EG/Johansen、半衰期、Kalman 动态对冲比 |
| `credit-analysis` | 固收与信用：信用债评级、利差、违约风险、城投债、转债定价 |
| `commodity-analysis` | 商品分析：原油供需、黄金定价、铜周期、库存周期、期货升贴水、季节性 |
| `dividend-analysis` | 红利股分析：股息质量、派息可持续性、除息机制、股息陷阱检查 |
| `earnings-forecast` | 盈利预测与一致预期（自上而下/自下而上、SUE/PEAD、预期修正） |
| `earnings-revision` | 美/港股盈利预期修正、指引分析与 PEAD 追踪 |
| `shadow-account` | Shadow Account 全流程：交割单提炼盈利模式 → 多市场回测 → 差值归因 → 8 节 PDF 报告；`shadow_*` 工具的**必读前置** |

### asset-class（9）— 资产类别专题

| skill | 说明 |
|---|---|
| `options-strategy` | 期权策略框架：Black-Scholes 定价、Greeks、多腿回测（加密/股票期权） |
| `options-advanced` | 高级期权：波动率曲面（SABR/Local Vol）、动态 Greeks 再平衡、日历价差、波动率套利 |
| `options-payoff` | 期权损益分析：payoff 图、盈亏平衡、多腿可视化、Greeks 情景 |
| `asset-allocation` | 资产配置：MPT/Black-Litterman/风险预算/全天候 + 4 个优化器使用指南 |
| `hedging-strategy` | 对冲设计：beta 对冲/期权保护/尾部风险/跨资产，含对冲比与成本评估 |
| `convertible-bond` | A股可转债：转股/纯债/期权三维估值、下修强赎回售博弈、双低与轮动 |
| `etf-analysis` | ETF：筛选、费率、跟踪误差、流动性、中国市场 ETF 量化配置框架 |
| `fund-analysis` | 基金筛选：晨星评级/夏普/信息比率、风格箱与漂移检测、FOF 构建 |
| `sector-rotation` | 行业轮动：申万景气度评分、动量排名、产业链传导、多维比较框架 |

### crypto（7）— 加密专题

| skill | 说明 |
|---|---|
| `crypto-derivatives` | 加密衍生品：永续资金费率套利、期限结构 contango/backwardation、期权波动率微笑 |
| `onchain-analysis` | 链上分析：活跃地址/巨鲸/TVL/DEX 流动性，MVRV/NVT/SOPR 估值信号 |
| `perp-funding-basis` | 资金费率与现货-期货基差：费率 regime、年化基差信号、跨所费率套利 |
| `liquidation-heatmap` | 清算热图：杠杆集中区、清算瀑布、猎杀止损区、清算位当支撑阻力 |
| `stablecoin-flow` | 稳定币供给与流向：USDT/USDC 铸销、交易所储备、链上流速、资金轮动择时 |
| `token-unlock-treasury` | 代币解锁与金库：vesting cliff/线性解锁、团队/投资人释放、抛压预测 |
| `defi-yield` | DeFi 收益：借贷/LP/质押收益、风险调整比较、协议可持续性评估 |

### flow（8）— 资金流与信息披露

| skill | 说明 |
|---|---|
| `hk-connect-flow` | 陆股通/港股通资金流：北向南向、行业配置追踪、跨境套利信号 |
| `us-etf-flow` | 美股 ETF 资金流：申赎追踪机构动向、板块宽度、风格因子流 |
| `adr-hshare` | ADR/H股/A股交叉上市溢价：定价差套利、双重上市估值、退市风险 |
| `edgar-sec-filings` | SEC EDGAR：10-K/10-Q/8-K/委托书/Form 4 解析与投资信号 |
| `corporate-events` | 公司事件驱动：并购套利价差、增减持信号、股权激励、定增配股、ST 预警 |
| `financial-statement` | 财报三表深读：勾稽关系、盈利质量（应计 vs 现金流）、杜邦分解、10+ 造假红旗 |
| `fundamental-filter` | 基本面筛选：PE/PB/ROE 及财报字段选股（A股走 tushare、港美走 yfinance） |
| `research-goal` | 目标驱动研究工作流：设定 research-only 目标、追踪标准、累积证据，规避实盘执行 |

### tool（10）— 工具型 skill

| skill | 说明 |
|---|---|
| `backtest-diagnose` | 回测失败/表现不佳诊断：定位根因并修复 |
| `report-generate` | 专业研报生成：标准结构（摘要/观点/正文/风险/建议）、评级体系、术语规范 |
| `doc-reader` | 通用文档读取（PDF/Word/Excel/PPT/图片 OCR/CSV/JSON/…），配 `read_document` 工具 |
| `web-reader` | 网页转 Markdown（第三方 Jina Reader），配 `read_url` 工具 |
| `trade-journal` | 交割单分析：同花顺/东财/富途/通用格式解析 + 交易画像 + 4 项行为诊断，配 `analyze_trade_journal` 工具 |
| `pine-script` | 回测策略导出为 TradingView/通达信/同花顺/东财/MT5 指标或策略代码 |
| `vnpy-export` | 回测策略导出为可运行的 vnpy CtaTemplate 类（A股/期货/加密） |
| `regulatory-knowledge` | 金融监管知识库：A股涨跌停/ST 退市/融券、港股 T+0/做空、美股 PDT/熔断、加密监管、跨境税务 |
| `social-media-intelligence` | 社媒情报：Twitter/Telegram/Discord/Reddit 金融信号提取 |
| `geopolitical-risk` | 地缘风险量化：危机信号、前兆识别、战争/制裁/供应中断事件策略 |

### research（1）

| skill | 说明 |
|---|---|
| `alpha-zoo` | 预置因子库浏览与基准测试：Kakushadze 101 / GTJA 191 / Qlib 158 / Fama-French，整库跑 IC/IR |

### risk-analysis（1）

| skill | 说明 |
|---|---|
| `ashare-pre-st-filter` | A股 ST/\*ST 风险预测：基于最新财报/业绩预告预测下一财年是否被风险警示，纳入新浪监管处罚记录；仅 A股，不预测造假 |
