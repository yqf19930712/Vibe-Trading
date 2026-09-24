# Vibe-Trading（来财AI 深度引擎）

本仓库是 [HKUDS/Vibe-Trading](https://github.com/HKUDS/Vibe-Trading)（自然语言量化研究 agent，Python + LangChain + FastAPI）的 fork，在上游引擎之上增加一层**多租户生产运维层（`ops/`）**，作为 laicai（来财）「来财AI 深度引擎」的后端：laicai 的 AI 聊天把深度分析请求透传给本仓库部署的 cube-router，每个 laicai 用户在独立的 KVM MicroVM 沙箱里运行一个专属引擎实例。

- 引擎本体的功能与用法（回测、因子、swarm、connector、MCP 等）见 [README.md](README.md)（上游自述，保持原样便于合并上游）与 [vibetrading.wiki](https://vibetrading.wiki/)，本文不复述。
- 多租户架构与协议契约见 [PRODUCT_DESIGN.md](PRODUCT_DESIGN.md)；观测/预算/数据可靠性/出境代理见 [docs/OBSERVABILITY.md](docs/OBSERVABILITY.md)。
- swarm 多智能体团队的 29 个 preset 清单与来财AI 触发策略见 [docs/SWARM-PRESETS.md](docs/SWARM-PRESETS.md)。
- 方案演进、评审记录与已退役的 v1 进程版见 [docs/HISTORY.md](docs/HISTORY.md)。
- **fork 的文档约定**：上游 `AGENT_CONTRIBUTOR_GUIDE.md` 的 Documentation Rules（用户可见改动要更新 `README.md` / `CHANGELOG.md`）对本 fork **不适用**——fork 只维护现状文档（本文、[PRODUCT_DESIGN.md](PRODUCT_DESIGN.md)、`docs/*.md`）并把变更叙事记进 [docs/HISTORY.md](docs/HISTORY.md)；`README.md` / `CHANGELOG.md` / 该指南本身保持上游原样便于合并。

## 仓库结构

| 路径 | 说明 |
|---|---|
| `agent/` | 上游引擎本体（`api_server.py` HTTP API、`src/` agent/工具/回测、`cli/`）。含少量本 fork 维护的差异，见下节 |
| `agent/mcp_server.py` | 上游的 MCP 插件**服务端**（`pyproject` 入口 `vibe-trading-mcp`）：把引擎工具暴露给 Claude Desktop / Cursor 等外部 MCP 客户端，与进程内工具面（`src/tools/build_registry`，即生产面）互相独立；生产拓扑不使用它。`src/tools/mcp.py` 是相反方向——引擎消费外部 MCP server 的客户端适配器。两个面已漂移：MCP 面 36 个工具里 `analyze_options` / `pattern_recognition` 对应进程内的 `options_pricing` / `pattern`；进程内的 `alpha_bench`/`alpha_compare`/`alpha_zoo`、`edit_file`、`remember`/`consolidate_memory`/`session_search`、`save_skill`/`patch_skill`/`delete_skill`/`skill_file`、假设库四件套、`compact`、`get_realtime_quotes`、shell 类以及 `trading_place_order`/`trading_cancel_order`/`propose_mandate_profiles` 不在 MCP 面；MCP 面独有的是 run / swarm 管理类（`list_runs`/`get_run_result`/`retry_run`/`get_swarm_status`/`list_swarm_presets`/`reap_stale_runs`/`list_skills`）。stdio 模式默认开 shell 工具；它自身不判断 `VIBE_TRADING_TENANT_SAFE`，但底层的 `build_registry` 读这个开关——设了时 MCP 面的 `trading_*` 工具在 registry 里找不到、调用即失败。本 fork 不维护它 |
| `ops/cube-router/` | **现行生产编排器**：FastAPI 单文件，对 laicai 暴露 `/ask`，按租户创建/复用 CubeSandbox MicroVM |
| `ops/cube-engine/` | 沙箱引擎镜像：`Dockerfile`（python:3.12-slim + 本仓库源码）+ `launcher.py`（guest 内进程管理器，模板探针目标） |
| `ops/vibe-router/` | 已退役的 v1 进程版编排器（同机多进程隔离），源码与 runbook 保留存档，沿革见 [docs/HISTORY.md](docs/HISTORY.md) |
| `frontend/` | 上游 React Web UI。生产不使用（镜像里放空 `frontend/dist` 占位） |
| `wiki/` `scripts/` `tools/` | 上游站点与 CI 杂项，与多租户层无关 |
| `CHANGELOG.md` | 上游发布记录（保持原样便于合并）；本 fork 的变更叙事记在 [docs/HISTORY.md](docs/HISTORY.md) |
| `AGENT_CONTRIBUTOR_GUIDE.md` | 上游贡献指南（保持原样）；其 Documentation Rules 在本 fork 内由文首「fork 的文档约定」覆盖 |

## 与上游的差异（`agent/` 内）

均为可长期携带的通用化改动，跟随上游合并时需保留：

- **单一数据根 `data_root()`**（`agent/src/core/paths.py`；`api_server._data_root()` 只是它的别名，run/session/upload 路径都从它派生。唯一例外是 `swarm/store.py::swarm_runs_root()`：它自己读 `VIBE_DATA_DIR`、回退到同一个 `agent/` 安装目录，语义与 `data_root()` 等价但尚未改为调用它）：`runs/` `sessions/` `uploads/` 目录可被 `VIBE_DATA_DIR` 重定向；`VIBE_MULTITENANT=1` 而缺 `VIBE_DATA_DIR` 时启动即报错（fail-loud，杜绝租户状态静默写进共享安装目录）。
- **租户安全档位**（`agent/src/tools/__init__.py`）：`VIBE_TRADING_TENANT_SAFE=1` 时 `build_registry` 排除 `trading_*` 前缀全部工具与 `propose_mandate_profiles`（动钱红线）；shell 类工具另由上游的 `VIBE_TRADING_ENABLE_SHELL_TOOLS=1` 门控制。
- **托管租户的 API 与进程边界**（`agent/src/config/tenant.py` 的 `tenant_safe_enabled` / `multitenant_enabled` / `tenant_profile_active` 是唯一判定）：`VIBE_MULTITENANT=1` 时 `api_server` 不再信任 loopback 调用方（`_loopback_trusted` 恒 False，guest 里的 loopback 调用方只可能是模型自己的 shell 子进程），全部端点都要 `API_AUTH_KEY`；引擎进程启动时 `prctl(PR_SET_DUMPABLE, 0)`，同 uid 的工具子进程读不到 `/proc/<engine>/environ`。任一租户开关在时（租户档）：`POST /swarm/runs`、`/swarm/runs/{id}/retry`、`/mandate/commit`、`/live/{halt,resume,authorize,runner/start,runner/stop}` 回 403；消息缺 `deadline_s` 时按 `VIBE_DEFAULT_DEADLINE_S`（缺省 900s）兜底；`providers/llm.py::_ensure_dotenv` 整段跳过，不读任何 `.env`（三个候选路径里 `~/.vibe-trading/.env` 在租户可写的 bind-mount 上，配置只来自 router 的 `/boot` env）。见 PRODUCT_DESIGN §2.3。
- **LLM 兼容性**（`agent/src/providers/llm.py`）：模型名含 `opus-4-7` / `opus-4-8` / `opus-5` / `sonnet-5` / `fable` / `mythos` 时省略 `temperature` 字段（这些模型拒绝该参数；名单可经 `LANGCHAIN_NO_TEMPERATURE_MODELS` 追加，见下文「换模型」节）；流式默认带 `stream_options.include_usage`（`LANGCHAIN_STREAM_USAGE=0` 可关），否则 `llm_usage` 事件恒空。
- **Anthropic 原生通道**（`agent/src/providers/llm.py` `_build_native_anthropic`）：`LANGCHAIN_PROVIDER=anthropic` 时走原生 `/v1/messages` API（`ANTHROPIC_AUTH_TOKEN`/`ANTHROPIC_API_KEY` + `ANTHROPIC_BASE_URL`），SSE ping 端到端透传、去掉两层协议转换——治 OpenAI-compat 路径吞 ping 导致长思考流被中间设备静默掐断的问题；生产内置模型即此通道。单次回复的输出 token 上限：原生通道总是发 `max_tokens`（`VIBE_ANTHROPIC_MAX_TOKENS` 优先，其次 `VIBE_MAX_OUTPUT_TOKENS`，都不设为 32000）；OpenAI 兼容通道**只在 `VIBE_MAX_OUTPUT_TOKENS` 显式设置时**才发上限字段，不设就不发、由端点自己封顶（截断会被主循环续写）。设了以后字段名由 langchain 适配器决定：`ChatOpenAI`（内置 openai 兼容配置与全部 BYOK）在每个请求里把 `max_tokens` 改名为 **`max_completion_tokens`**（`_default_params` 与 `_get_request_payload` 都改，`model_kwargs={"max_tokens": …}` 同样被改，langchain-openai 1.3 没有保留旧名的开关；Responses API 路径再改成 `max_output_tokens`），只有 `BaseChatOpenAI` 子类如 langchain-deepseek 原生适配器仍发 `max_tokens`——不是每个兼容端点都认 `max_completion_tokens`（zhipu / moonshot / sub2api 一类中转未验证），所以设这个变量前要对目标端点发一次带 tools 的请求确认不 400。**收尾轮保留工具定义**：最后一轮 / early_finalize 以 `tool_choice=none`（原生通道 `{"type":"none"}`，OpenAI 兼容通道 `"none"`）禁止调用而不是摘掉 `tools`——Anthropic Messages API 对含 tool_use/tool_result 块却无 `tools` 的请求回 400；`providers/capabilities.py` 的 `tool_choice_none=False`（目前 zhipu/glm）表示该端点不支持 `none`，退回省略 `tools` 的旧行为。
- **美股实时行情工具 `get_realtime_quotes`**（`agent/src/tools/realtime_quote_tool.py`）：TickFlow 快照报价（现价/涨跌/OHLC/量/时段），仅美股，需 `TICKFLOW_API_KEY`；替代模型 bash-curl 行情网站。
- **可观测性**（详见 [docs/OBSERVABILITY.md](docs/OBSERVABILITY.md)）：`src/core/logging_setup.py` 结构化 JSONL 日志（contextvars 绑 session/attempt id，穿透工具线程）；AgentLoop 收口发 `attempt_stats` 事件（迭代/LLM·工具耗时/tokens/逐工具/数据源统计，SSE + trace 双写）；数据链路 print 改结构化 logger。
- **预算与提前收敛**：messages API 增 `deadline_s`；`src/core/budget.py` deadline contextvar + `cap_timeout`；AgentLoop 剩余 <25% 注入收尾提示、不足一轮强制出文本（`early_finalize`：工具定义仍在请求里，`tool_choice=none` + `[SYSTEM]` 收尾行）；`finish_reason=length`（原生通道 `stop_reason=max_tokens`）的回复不当最终答案：主循环与 swarm worker 都把截断正文留在轨迹里并追加「从截断处继续」提示再跑一轮（每 attempt 最多 `VIBE_LENGTH_CONTINUATIONS`=2 次，占正常迭代），续写拼回原文；到最后一轮或次数用尽则在答案末尾附「（输出被截断）」；被截断的那一轮若带工具调用，参数已被截坏，一律不执行、回 `tool_call_truncated` 错误让模型拆小重发（长文引导用 `write_file` 的 `mode="append"` 分段写）；单次 LLM 调用本身也受 deadline 约束（流式到点即截断，已流出的正文加「时间预算耗尽」标注作为答案，deadline 之后不再开新一轮）；单工具/swarm 超时被剩余预算钳制；`VIBE_MAX_ITERATIONS` env 化。
- **数据可靠性**：`market_data` 空结果/异常沿 `FALLBACK_CHAINS` 逐源降级并输出 `_gaps` 明细；`src/core/fetch_stats.py` attempt 级数据源记账；tushare 进程内节流（`TUSHARE_MAX_PER_MIN`）+ 重试；启动 `socket.setdefaulttimeout` 兜底无超时 SDK。美/港股日线备源 `ifind`（同花顺 iFinD MCP，国内端点不走隧道，`IFIND_MCP_TOKEN` 鉴权；自然语言 quotes 工具 → markdown 表头驱动解析，仅日频，见 `backtest/loaders/ifind_loader.py`）；美股日线备源 `tickflow`（api.tickflow.org 结构化 REST，`TICKFLOW_API_KEY` 经 `x-api-key` 头，前复权列式 K 线，免费档 10 次/分·1 标的/次，429 单次重试，见 `backtest/loaders/tickflow_loader.py`）——国内直连源优先：us_equity 链 = tickflow→ifind→yfinance→akshare、hk_equity 链 = ifind→tickflow→yfinance→tencent→futu→akshare（yfinance 依赖隧道且被 Yahoo 限频，降为兜底；tickflow 免费档无港股权限故港股由 ifind 领跑）。
- **出境代理**：三个消费方读 `VIBE_TRADING_EGRESS_PROXY`——`web_search`（ddgs `proxy` 参数）、`read_url`（`tools/web_reader_tool.py`，上游是 `r.jina.ai`，须在 B 端 tinyproxy 白名单里）与 yfinance loader；搜索后端默认 `auto`（ddgs 9.x 已无 google/bing）；`ops/cube-engine/launcher.py` 按 `/boot` env 在 guest 内拉起 SSH 隧道（镜像 +openssh-client）。
- **子进程凭据隔离**（`agent/src/tools/subprocess_env.py` + `tools/redaction.py::redact_secret_values`）：引擎替模型拉起的每个子进程都只拿白名单 env——`bash` / `background_run` 与 MCP stdio 服务端子进程拿 `_subprocess_env()`（`PATH`/`HOME`/locale/`TZ`/`TMPDIR`/venv 变量 + `VIBE_*`；MCP 再叠加操作员写在该 server `env` 块里的变量），`backtest` 的 Runner 子进程（会 import 模型写的 `signal_engine.py`）拿 `backtest_subprocess_env()` = 同一白名单 + loader 在子进程内取用的三个数据 token（`TUSHARE_TOKEN`/`TICKFLOW_API_KEY`/`IFIND_MCP_TOKEN`）+ 代理/CA 变量与 loader 调参项；名字含 `_KEY`/`_TOKEN`/`_SECRET`/`_PASSWORD` 段或 `OPENAI_`/`ANTHROPIC_`/`LANGCHAIN_` 前缀者一律剔除，`API_AUTH_KEY`/`JINA_API_KEY`/`ROUTER_*` 不进任何子进程；所有工具结果在进入轨迹前按**值**把引擎 env 里的凭据替换成 `[redacted:<KEY>]`；不在引擎 env 里的出境隧道私钥（launcher 写到 `~/.ssh/egress_key`）按文件内容一并脱敏为 `[redacted:VIBE_EGRESS_SSH_KEY]`。`background_run` 与 `bash` 同在 run_dir 执行、同过 `_audit_command` 审计。租户档位下 `~/.vibe-trading/agent.json` / `swarm-agent.json` 不被读取（那是租户可写目录），MCP 服务端只认 `VIBE_TRADING_AGENT_CONFIG` / `VIBE_TRADING_SWARM_AGENT_CONFIG` 指向的镜像内路径。见 PRODUCT_DESIGN §2.3。
- **取消穿透**（`agent/src/core/cancel.py`）：attempt 的 cancel 事件经 contextvar 进入每个工具线程，工具看门狗与 `run_swarm` 的轮询按 ≤1s 切片检查它；attempt 级取消会连带 `cancel_run` 掉在等的 swarm run（worker 每次迭代/重试前检查），只有等待预算耗尽才保留 run 供续等；同会话新 attempt 到来时旧 loop 先被 cancel 再覆盖注册，取消早于 loop 注册到达时挂起、注册即投递；`run()` 退出时复位它绑定的 cancel / deadline / fetch_stats contextvar。后台任务（`background_run`）按会话归属：通知与 `check` 只看本会话，全局最多 4 个、每会话最多 2 个在跑，attempt 取消或会话删除时按进程组杀掉。
- **工具结果形状**：`get_market_data` 每标的返回紧凑表 `{summary, columns, rows}`（默认 120 行、4 位小数；`summary` 的首末收盘、涨跌、高低点按降采样之前的整个请求区间计算，`change_pct` 与 `get_realtime_quotes` 同为百分数），`write_file` 支持 `mode="append"`，`edit_file` 回报 `occurrences/replaced/remaining` 并拒绝空 `old_text`，`load_skill` 返回 SKILL.md 原文 Markdown；超 10k 字符的结果落盘 + 工具感知的预览（`agent/src/agent/tool_result_store.py`）是**唯一**的截断层：`bash` 整段返回 stdout/stderr（落盘为纯文本，stderr 接在 `--- stderr ---` 行之后；工具内只剩一道流式硬上限：每路流边读边计数，超过 100 万字符即杀掉整个进程组、只保留前 100 万字符并附标记，`background_run` 同一实现），`read_file` 默认一页 200 行、超限只做预览不再落盘副本（预览指回源文件的 offset/limit），见 docs/OBSERVABILITY.md §5.3 与 docs/SKILLS.md §2。
- **上下文与连续性**（详见 PRODUCT_DESIGN §7 与 docs/SYSTEM-PROMPT.md）：状态栏与 swarm worker 的时间行由 `core/market_clock.py::clock_lines()` 给出北京时间、美东时间与 A 股 / 港股 / 美股的常规时段状态（不查交易所节假日，文案明说）；续聊回放按问答对取舍（`session/replay.py`）；交接摘要按 `##` 分节取舍（`session/handoff.py::fit_summary`）；L3 摘要后原样回插本轮请求；`VIBE_CONTEXT_WINDOW_TOKENS` 设了时按模型窗口给压缩阈值封顶；长期记忆的系统提示段由 `PersistentMemory.snapshot` 一处渲染（`<memory-index>` 围栏 + 非指令声明 + 每行更新日期 + 估算 2000 token 总上限）。
- **删除会话的 tombstone**（`agent/src/session/tombstone.py`）：`delete_session` 删任何东西之前先登记 tombstone（进程内集合 + `sessions/.deleted/<sid>` 标记文件），此后会话级写入一律拒写，在途 attempt 收尾时再清一遍，杜绝删掉的会话被收尾写回；标记 30 天后在引擎启动时清理。
- **落盘原子性**（`agent/src/core/atomic_write.py`）：记忆条目/`MEMORY.md`/handoff/swarm 状态全部 tmp + `os.replace`；损坏的 `MEMORY.md` 隔离为 `.corrupt-<ts>` 而不是让整个租户的每次 attempt 起不来。

## 生产拓扑（速览）

```
laicai web (阿里云) ──Bearer──► cube-router :8990 (CubeSandbox 宿主机 182.92.217.17)
                                   │  CubeAPI :3000 (E2B 兼容控制面)
                                   ▼
                        每租户 MicroVM 沙箱（PVM/KVM）
                        launcher :8898 ── 引擎 vibe-trading serve :8899
```

详细拓扑、隔离模型与全部 HTTP 契约见 [PRODUCT_DESIGN.md](PRODUCT_DESIGN.md)。

## 部署 runbook（CubeSandbox 宿主机）

### 1. 宿主机准备（PVM 内核）

普通云主机（如阿里云 ECS）无嵌套虚拟化（无 `/dev/kvm`、CPU 无 vmx），需先换 CubeSandbox 的 PVM 宿主内核：

1. 安装 OpenCloudOS `6.6.69-*.cubesandbox.pvm.host` 预编译 DEB（[TencentCloud/CubeSandbox](https://github.com/TencentCloud/CubeSandbox) Releases），`modprobe kvm_pvm`（配置开机自载）后由 PVM 提供 KVM 能力。原内核保留在 GRUB 可回退。
   - PVM 内核 GRUB 参数含 `net.ifnames=0` / `console=ttyS0`，与阿里云 Ubuntu 镜像默认一致，换内核不破网。
2. 数据盘格式化为 **XFS（reflink 开启）** 挂 `/data/cubelet` —— CoW 快照的硬要求。
3. **放行 host-mount 前缀**（租户数据持久化的前提）：在 `CubeMaster/conf.yaml` 末尾加

   ```yaml
   extra_conf:
     allowed_host_mount_prefixes:
       - "/data/shared/"
   ```

   然后 `systemctl restart cube-sandbox-cubemaster`。不放行的话建沙箱时的 `host-mount` 会被拒。
4. CubeSandbox one-click 安装：`CUBE_PVM_ENABLE=1 ./install.sh`，全栈由 systemd `cube-sandbox-control.target` 管理。关键端口：CubeAPI（E2B 兼容）`:3000`（`X-API-Key`，one-click 默认 `e2b_000000`）、WebUI `:12088`、cubemaster `:8089`。WebUI 端口务必用安全组限制来源 IP。

### 2. 引擎镜像构建与模板发布

镜像定义在 `ops/cube-engine/`。构建 context = 本仓库源码树 + `launcher.py` 拷贝到 context 根（`Dockerfile` 以 `COPY launcher.py` 引用）。

```bash
# 在宿主机（本机跑一个 registry:2 容器作镜像仓库）
docker build -t 127.0.0.1:5000/vibe-engine:vN -f Dockerfile <context>
docker push 127.0.0.1:5000/vibe-engine:vN

cubemastercli tpl create-from-image \
  --image 127.0.0.1:5000/vibe-engine:vN \
  --writable-layer-size 4G \
  --expose-port 8898 --expose-port 8899 \
  --probe 8898 --probe-path /health
# 记下输出的 templateID → 写入 router env 的 VIBE_CUBE_TEMPLATE_ID
```

镜像内：launcher 常驻 `:8898`（模板探针目标），引擎 `:8899` 由 launcher 按 router 下发的租户 env 拉起；以非 root 用户 `vibe` 运行，`HOME=/home/vibe`。

**`/app`（引擎代码）归 root、对运行用户只读**：构建期 `python -m compileall -q /app/agent` 预编译字节码后 `chmod -R go-w /app`，只有 `/home/vibe/.vibe-trading`（租户 bind-mount 的挂载点）chown 给 `vibe`；镜像 `ENV` 固定 `VIBE_DATA_DIR=/home/vibe/.vibe-trading`、`PYTHONDONTWRITEBYTECODE=1`、`PYTHONNOUSERSITE=1`。理由：引擎的 shell 工具以 `vibe` 身份运行，可写的 `/app` 会让租户代码改写引擎本身，下一次 `/boot` 就以带全租户共享 LLM 凭据的 env 跑起来；`PYTHONNOUSERSITE` 挡的是同一类路径——租户在可写的 HOME 里放 `~/.local/.../site-packages` 的 `.pth` / `sitecustomize`（该变量不进 shell 子进程的白名单 env，所以 bash 里 `pip install --user` 照常可用）。运行时引擎不写 `/app`：租户状态走 `VIBE_DATA_DIR`，缓存走 HOME，backtest 子进程 cwd 虽是 `/app/agent`，产物写 run_dir。已知行为：`api_server` 的 `/settings/*` 写接口（写 `agent/.env`）在镜像里失败，router 不调用它们。**引擎新增任何运行时写路径都必须落在 `VIBE_DATA_DIR` 或 HOME 下**（`ops/cube-router/test_engine_image.py` 只检查 Dockerfile，不检查引擎代码）。

新镜像发布前在宿主上核对（任一不符都不要切模板）：

```bash
docker run --rm --entrypoint sh <image> -c 'id; stat -c "%U:%G %a %n" /app /app/agent /app/agent/src /app/agent/api_server.py'
#   uid=1000(vibe)；四项全部 root:root，目录 755、文件 644（或更严）
docker run --rm --entrypoint sh <image> -c 'touch /app/agent/x; touch /app/agent/src/x.py; mkdir /app/agent/src/__pycache__/x; echo rc=$?'
#   三条都 Permission denied
docker run --rm --entrypoint sh <image> -c 'find /app -writable 2>/dev/null | head; ls /app/agent/src/agent/__pycache__ | head -3; env | grep -E "PYTHONNOUSERSITE|PYTHONDONTWRITEBYTECODE|VIBE_DATA_DIR"'
#   find 无输出；__pycache__ 里有 .cpython-312.pyc；三个变量都在
```

切到新模板后在一个租户沙箱里经正常 `/ask` 复核：宿主 `DATA_ROOT/<tk>/` 下 `sessions/`、`runs/`、`logs/engine.jsonl` 有新内容；让模型用 bash 执行 `echo x > /app/agent/src/evil.py` 得到 Permission denied、`pip install --user six && python -c "import six"` 成功、`curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8899/sessions` 为 `401`（多租户档不信任 loopback）、`head -3 ~/.ssh/egress_key` 的工具结果为 `[redacted:VIBE_EGRESS_SSH_KEY]`（配了出境隧道时）；backtest 与一次短 `deep_team` 各跑通、`.swarm/runs` 落在租户目录。

### 3. cube-router 部署

```bash
# /opt/cube-router/{router.py,requirements.txt} + venv
python3 -m venv /opt/cube-router/.venv
/opt/cube-router/.venv/bin/pip install -r requirements.txt   # fastapi/uvicorn/httpx/pydantic

# env：/opt/cube-router/router.env（chmod 600）
# systemd：cp ops/cube-router/cube-router.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now cube-router
```

上线前检查：`grep -E '^(LANGCHAIN_(API_KEY|TRACING|TRACING_V2|ENDPOINT|BASE_URL|PROJECT|SESSION|HANDLER|ENV|CUSTOM_HEADERS|REVISION_ID|HUB_[A-Z_]+)|LANGSMITH_[A-Z_]+)=' /opt/cube-router/router.env` 必须为空——它覆盖 router `FORWARD_ENV_DENY` 的全部名字（外加整个 `LANGCHAIN_HUB_*` 族，比拒绝名单更宽）。拒绝名单里的名字 router 本就不转发（见 env 表「转发给租户引擎的 env」），但 router 进程自己也不该带着 LangSmith 凭据跑，而且拒绝名单之外的 `LANGCHAIN_*` 会按前缀进引擎。

多租户档（`engine_env()` 恒下发 `VIBE_MULTITENANT=1`）而 `VIBE_LAUNCHER_AUTH` 未开时，router 启动日志打一条 `VIBE_LAUNCHER_AUTH is off while tenant engines run multi-tenant…` warning——这是提醒，不是故障，开启顺序见下文「launcher 鉴权：开启与回滚」。

监听 `0.0.0.0:8990`（laicai web 在另一台主机上）——**必须**用云安全组把 8990 白名单到 laicai web 主机 IP，鉴权靠 Bearer token 双保险。

**env 变量**（`router.env`）：

| 变量 | 必填 | 说明 |
|---|---|---|
| `VIBE_ROUTER_SECRET` | ✔ | 租户目录派生 HMAC key。**schema key，永不轮换**（轮换即孤立全部租户数据） |
| `VIBE_ROUTER_TOKEN` | ✔ | laicai 调用 `/ask` 等的 Bearer token |
| `VIBE_CUBE_TEMPLATE_ID` | ✔ | 引擎沙箱模板 ID |
| `CUBE_API_URL` / `CUBE_API_KEY` | | CubeAPI 控制面，默认 `http://127.0.0.1:3000` / `e2b_000000` |
| `VIBE_SANDBOX_DOMAIN` / `VIBE_SANDBOX_HTTP_PORT` | | cube-proxy 数据面域名/端口，默认 `cube.app` / `80` |
| `VIBE_STATE_FILE` | | 租户映射持久化，默认 `/var/lib/cube-router/state.json` |
| `VIBE_HOST_DATA_ROOT` | | 租户引擎数据在**宿主**上的根目录，默认 `/data/shared/vibe`。每租户一个子目录（名 = tenant_key），建沙箱时 bind-mount 到 `/home/vibe/.vibe-trading`。必须落在 `allowed_host_mount_prefixes` 之内 |
| `VIBE_MAX_INSTANCES` | | 并发 RUNNING 沙箱上限，默认 3（8G 宿主机的安全值） |
| `VIBE_MAX_CONCURRENT_ACTIVE` | | 并发 `/ask` 处理上限，默认 2 |
| `VIBE_ACTIVE_QUEUE_WAIT_S` | | 超出上一项的 `/ask` 排队等处理槽的上限，默认 120（且不超过本问预算）；等不到回 503 busy 帧（`code=busy`、`busy_reason=active_queue_full`），与 RUNNING 满的 503 同形 |
| `VIBE_IDLE_TTL_S` | | 空闲 pause 阈值，默认 1200 |
| `VIBE_READY_TIMEOUT_S` / `VIBE_POLL_INTERVAL_S` / `VIBE_ASK_TIMEOUT_S` | | 就绪预算 180s / 轮询间隔 3s / 单问默认超时 900s（= `intent=standard` 的预算档，只对不带 `timeoutS` 的调用方生效，见下一行） |
| `VIBE_SWARM_ASK_TIMEOUT_S` | | `intent=deep_team`（多智能体团队）的预算档，默认 7200。`BUDGET_BY_INTENT` 与下发给租户引擎的 `SWARM_TIMEOUT` env 都从它派生。**但 ask 预算以请求里显式的 `timeoutS` 为准**，而 laicai 现在每次都显式发（缺省按 intent 取 900 / 7200，数值写在 laicai 的 `vibe-trading.ts` 与 `swarm-directive.ts`），ask_log 的 `budget_source` 对 laicai 流量恒为 `explicit`——所以改这两个 `VIBE_*_ASK_TIMEOUT_S` 只改变不带 `timeoutS` 的调用方的预算与引擎的 `SWARM_TIMEOUT`，laicai 流量的 ask 预算不变；要改 laicai 的档位，两边一起改 |
| `VIBE_POLL_FAIL_MAX` / `VIBE_POLL_FAIL_MAX_S` | | 等答案轮询的失败容忍：传输异常、非 200、非 JSON 都算一次失败，连续失败 10 次或持续 120s（先到者）才判 502；失败期间每次都探 launcher `/health`，报 `engine=stopped` 立即 502，引擎回 401（被绕过 router 重启过）也立即 502 |
| `VIBE_PUMP_READ_TIMEOUT_S` | | 引擎事件流的读超时，默认 90（引擎空闲时每 30s 发心跳，读不到即当死连接）；断开后带已转发的最大事件 id 作 `Last-Event-ID` 退避重连（0.5s 起、最长 10s），按 id 高水位去重（旧引擎的不透明 id 按集合去重，计量事件整个 ask 不淘汰；见 PRODUCT_DESIGN §3.1） |
| `VIBE_FORGET_TOMBSTONE_S` / `VIBE_FORGET_LOCK_WAIT_S` | | `/forget` 墓碑有效期，默认 2592000（30 天：期内该租户的 `/ask` 回 410、不重建任何东西；router 启动时清理已过期且不再指向沙箱的墓碑行）/ `/forget` 等租户锁的上限，默认 10s（低于 laicai 的 30s 调用超时；等不到照常清理） |
| `VIBE_LAUNCHER_AUTH` | | 默认 `0`。`1` = 新建沙箱的 launcher 在首次 `/boot` 采纳 router 派生的 token，此后 `/boot` `/stop` 必须带它；开关计入 LLM 指纹。开启与回滚顺序见「launcher 鉴权：开启与回滚」 |
| `VIBE_MEMORY_LOCK_TIMEOUT_S` | | `/memory/delete` 取记忆索引锁的上限，默认 5s（非阻塞 + 重试），超时记 warning 后不加锁照删，见「已知坑」 |
| `VIBE_TENANT_QUOTA_BYTES` / `VIBE_TENANT_WATERMARK` | | 租户用量的**统计分母**（默认 4G）与告警水位（默认 0.8）。不是文件系统 quota（租户目录在宿主盘上无配额），只影响 `/healthz` 的 `disk` 段、`GET /tenants/usage` 的 `pct`/`over_watermark`（tk8 列表）与 warn 日志——**router 目前不做任何自动清扫**；整盘水位另见两处的 `disk_used_pct` |
| `VIBE_SWEEP_STALE_TEMPLATES` | | 默认 `1`：router 启动时后台删除所有挂在非当前模板上的 vibe-engine 沙箱（含 state 之外的孤儿）与全部旧 vibe-engine 模板（`VIBE_CUBEMASTERCLI`，默认 `/usr/local/bin/cubemastercli`）。**回滚模板前必须先置 `0`**，见「更新操作」 |
| 转发给租户引擎的 env | | 经 launcher `/boot` 进每个租户引擎的是「显式名单 + 前缀」两类（`router.py` 的 `FORWARD_ENV` / `FORWARD_ENV_PREFIXES`）：显式名单 = `OPENAI_API_KEY`/`OPENAI_BASE_URL`/`OPENAI_API_BASE`/`OPENAI_MODEL`（引擎的 serve 路径不读 `OPENAI_MODEL`，只有 CLI 读）、`ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN`/`ANTHROPIC_BASE_URL`、`VIBE_MAX_OUTPUT_TOKENS`、`VIBE_LENGTH_CONTINUATIONS`、`VIBE_MEMORY_TTL_DAYS`、`VIBE_CONTEXT_WINDOW_TOKENS`（**只下发给内置通道**，BYOK 见下面 `VIBE_BYOK_CONTEXT_WINDOW_TOKENS`）、`TOKEN_THRESHOLD`（引擎压缩阈值，引擎内默认 40000）、`TUSHARE_TOKEN`、`VIBE_TRADING_SEARCH_BACKENDS`、`JINA_API_KEY`、`IFIND_MCP_TOKEN`、`TICKFLOW_API_KEY`、`TICKFLOW_BASE_URL`；前缀 = `LANGCHAIN_*`（provider / model / temperature / `LANGCHAIN_STREAM_USAGE` / `LANGCHAIN_REASONING_EFFORT` …）与 `VIBE_ANTHROPIC_*`（`_MAX_TOKENS` / `_THINKING`），**减去** LangSmith 的同名空间（`FORWARD_ENV_DENY` 是精确名：`LANGCHAIN_API_KEY` / `LANGCHAIN_TRACING_V2` / `LANGCHAIN_TRACING` / `LANGCHAIN_ENDPOINT` / `LANGCHAIN_BASE_URL` / `LANGCHAIN_PROJECT` / `LANGCHAIN_SESSION` / `LANGCHAIN_HANDLER` / `LANGCHAIN_ENV` / `LANGCHAIN_CUSTOM_HEADERS` / `LANGCHAIN_REVISION_ID` / `LANGCHAIN_HUB_API_URL` / `LANGCHAIN_HUB_API_KEY`；`FORWARD_ENV_DENY_PREFIXES` 只有 `LANGSMITH_`——拒绝名单之外的 `LANGCHAIN_HUB_*` 会按前缀转发）——这些一旦进引擎，langchain-core 就会把含持仓的 prompt 上报第三方 tracing，故无论 router.env 里有没有都不转发。不在两类里的 router env 到不了引擎（OBSERVABILITY §9 分列「router 可覆盖」与「只能改镜像」两类）。**生效方式**：LLM 指纹含整份 boot env（转发值、租户档位、出境配置、launcher 鉴权开关；不含每次随机生成的 `API_AUTH_KEY`）的摘要，所以改了这些值并 restart cube-router 之后，**每个既有租户在下一次 `/ask` 时重启一次引擎**（在跑的与 paused 的都一样；这一问多十几秒到几十秒），沙箱与会话数据不动 |
| `VIBE_BYOK_CONTEXT_WINDOW_TOKENS` | | 不设。BYOK 引擎不继承内置通道的 `VIBE_CONTEXT_WINDOW_TOKENS`（那是内置模型的窗口：套到大窗口模型上会过度压缩，套到小窗口模型上也起不到保护作用）；设了这一项就以它作为 BYOK 引擎的 `VIBE_CONTEXT_WINDOW_TOKENS`，不设则 BYOK 引擎不封顶、沿用 `TOKEN_THRESHOLD`。它自身不转发，随 env 摘要进入 BYOK 指纹 |
| `LANGCHAIN_TEMPERATURE` | | `none` / `off` / 空 = 任何模型都不发 `temperature`；否则按数值发。填了非数字会 warning 后回落 `0.0` |
| `LANGCHAIN_NO_TEMPERATURE_MODELS` | | 逗号分隔的模型名子串，**追加**到 `llm.py` 内置的 `NO_TEMPERATURE_MODELS` 名单（追加而非替换，避免为了加新模型把已知的漏掉） |
| `VIBE_ASK_LOG` | | 每次 `/ask` 一行的观测日志，默认 `/var/lib/cube-router/ask_log.jsonl`（20MB 轮转） |
| `VIBE_EGRESS_KEY_FILE` / `VIBE_EGRESS_SSH_DEST` | | 沙箱出境隧道：宿主上的 SSH 私钥路径（如 `/root/vibe-egress-key`）+ 目的地（如 `root@<B服务器>`）。配了才会给引擎注入 `VIBE_TRADING_EGRESS_PROXY`；B 端该 key 必须 `restrict,port-forwarding,permitopen="127.0.0.1:8888"`。router 只下发 key 与目的地两项；launcher 侧的远端（`VIBE_EGRESS_REMOTE`，默认 `127.0.0.1:8888`）与本地端口（`VIBE_EGRESS_LOCAL_PORT`，launcher 进程启动时读，默认 `8118`）生产上都取默认值 |
| 租户档位覆盖 | | `engine_env()` 会给每个租户注入默认档位：`VIBE_MAX_ITERATIONS=50`、`VIBE_TRADING_DATA_CACHE=1`、`VIBE_TRADING_TOOL_TIMEOUT_SECONDS=300`、`SWARM_TIMEOUT`（派生自 `VIBE_SWARM_ASK_TIMEOUT_S`）、`TIMEOUT_SECONDS=300`（LLM 流式读超时）、`VIBE_TRADING_SEARCH_BACKENDS=auto`、`VIBE_TRADING_ALLOWED_FILE_ROOTS=/tmp`——在 router.env 里设同名变量即可整体覆盖。输出上限 / 续写次数 / thinking 模式（`VIBE_MAX_OUTPUT_TOKENS`、`VIBE_ANTHROPIC_MAX_TOKENS`、`VIBE_LENGTH_CONTINUATIONS`、`VIBE_ANTHROPIC_THINKING`）走上一行的转发规则，引擎默认值见 docs/OBSERVABILITY.md §9 |

laicai 侧只需在 `web.env` 配 `VIBE_ROUTER_URL=http://<宿主机>:8990` + `VIBE_ROUTER_TOKEN`。

**引擎 env 的四个前缀**（不做统一——改名会波及生产 `router.env` 与镜像）：`VIBE_TRADING_*` 是上游引擎自己的开关（工具超时、shell 门、搜索后端、文件根…）；`VIBE_*` 是本 fork 新增的引擎 / router 旋钮（`VIBE_MAX_ITERATIONS`、`VIBE_FINALIZE_RESERVE_S`、`VIBE_ANTHROPIC_*`、`VIBE_DATA_DIR`，router 侧的 `VIBE_ROUTER_*` / `VIBE_CUBE_*` 等）；`VT_*`（主循环 `src/agent/loop.py`：`VT_HEARTBEAT_INTERVAL_S` / `VT_REASONING_DELTA_MIN_INTERVAL_S` / `VT_STREAM_RETRIES` / `VT_STREAM_RETRY_DELAY_S`）与 `SWARM_*`（swarm worker 与 `run_swarm`：`SWARM_TIMEOUT` / `SWARM_MAX_WORKERS` / `SWARM_WORKER_MAX_ITER` / `SWARM_WORKER_TIMEOUT` / `SWARM_HEARTBEAT_INTERVAL_S` / `SWARM_STREAM_RETRIES` / `SWARM_STREAM_RETRY_DELAY_S` / `SWARM_GROUNDING_MAX_SYMBOLS`）是上游这两个模块各自既有的前缀，fork 加的流重试旋钮沿用了所在模块的前缀——所以同一「流重试」策略在主循环与 worker 里要各配一遍。除 `SWARM_TIMEOUT`（router 派生下发）外，`VT_*` / `SWARM_*` 都不在上表的转发名单里，要改只能改镜像默认值。

### 4. 更新操作

两条路径的影响面完全不同：

- **只改 router**（`ops/cube-router/router.py`）：scp 覆盖 `/opt/cube-router/router.py` → `systemctl restart cube-router`。沙箱不受影响——重启后从 `state.json` 重挂既有租户沙箱，不泄漏、不丢数据。若新版本改变了 boot env 的内容或指纹格式，每个租户下一问会重启一次引擎（见下一条「只改 LLM 配置」），挑低峰发布。
- **改引擎代码**（`agent/`）：重建镜像 → push → `cubemastercli tpl create-from-image` 发新模板 → 更新 `VIBE_CUBE_TEMPLATE_ID` → restart cube-router。**不需要动既有沙箱**：router 在 `get_or_create` 里比对 `state.json` 里记的 `template_id`，不一致就删掉旧沙箱、用新模板重建；启动清扫 `_sweep_stale_templates` 同时把旧模板上的沙箱（含 state 之外的孤儿）与旧模板本身删掉。租户数据在宿主 bind-mount 里，重建无损。挑没有在途 `/ask` 的窗口做切换（清扫假定启动时无在途请求）。
- **回滚模板**：先在 `router.env` 置 `VIBE_SWEEP_STALE_TEMPLATES=0`，再把 `VIBE_CUBE_TEMPLATE_ID` 改回旧值 → restart。不先关清扫，启动时会把「新」模板与其沙箱当作过期物删除，且旧模板若已被上一次清扫删掉则无法回滚——发新模板后先验证再让清扫跑是默认顺序，不要跳过验证。
- **只改 LLM 配置**（转发名单 / 前缀里的凭据、模型、档位旋钮）：改 `router.env` → restart cube-router。LLM 指纹含整份 boot env 的摘要（`llm_fingerprint` 的 `|env:<sha16>` 段），所以每个既有租户——在跑的、paused 的、router 重启后从 state 重挂的——都在**自己的下一次 `/ask`** 时经 launcher `/boot` 重启一次引擎进程，沙箱与会话数据不动；从没再来的租户不受影响，也就一直带着旧 env（轮换泄露的 key 时，要么等各租户下一问，要么配合一次模板切换重建全部沙箱）。请求带 `llm{}` 的 BYOK 引擎凭据来自请求本身，其余转发值与档位同样在它的指纹里。

### router `state.json` 的形态

不需要迁移脚本，旧 router 也能读新文件：

- 一个租户一行 `{sandbox_id, template_id, llm_fp, api_key}`。`llm_fp` 是 `<byok:sha16 | builtin:<model> | default>|env:<sha16>`；另有两种过渡值：`boot-pending:<fp>`（`/boot` 已发、200 还没确认——冷启时这一行在 `/boot` 之前就写入，引擎确实以该 key 起来了下一问会直接采纳，否则重启）与 `stale`（引擎拒绝了 router 的 key，下次使用前重启）。旧 router 遇到不认识的指纹只会让该租户多重启一次。
- `/forget` 留下的墓碑行 `{"forgotten_at": <epoch>}`；删沙箱失败时同一行还保留 `sandbox_id` 等字段，夜间重试与 router 启动清扫都会再删。旧 router 忽略墓碑。
- 要单独重置一个租户：删掉它的沙箱（CubeAPI）并删掉这一行，下一问冷启重建；数据在宿主 bind-mount 上，不受影响。

### launcher 鉴权：开启与回滚

launcher `:8898` 的 `/boot` `/stop` 在 guest 内经 loopback 可达。多租户档的引擎已不信任 loopback，但 guest 里的 shell 仍可以向 launcher `/boot` 一个自己挑的 key 再拿它访问引擎——`VIBE_LAUNCHER_AUTH=1` 关掉这条路。token = `HMAC(VIBE_ROUTER_SECRET, "launcher:"+sandbox_id)`，按沙箱派生、无需存储；router 对 `/boot` 恒带 `Authorization: Bearer <token>`（旧 launcher 忽略这个头），开关打开时 token 另随 boot env 下发。launcher **只从生命周期内的第一次 `/boot` 采纳 token**（此前 guest 里还没有任何租户代码），此后没有正确 token 的 `/boot` `/stop` 一律 401；带正确 token、env 不带 token 的 `/boot` 解除要求。**默认关闭**：已持有 token 的 launcher，回滚到不含这段逻辑的 router 就再也 `/boot` 不了（全部 502），只能靠重建沙箱恢复。

**前提**：带 launcher 鉴权的 router 与模板都已上线并验证通过，且确认不再需要回滚到不含 launcher 鉴权的 router。

开启：

1. `router.env` 加 `VIBE_LAUNCHER_AUTH=1` → `systemctl restart cube-router`。启动日志里不应再有 `VIBE_LAUNCHER_AUTH is off…` warning。
2. 生效范围：此后**新建**的沙箱在首次 `/boot` 采纳 token。既有沙箱的指纹变了（开关计入指纹），下一问会重启一次引擎，但它们的 launcher 早已 boot 过、不会采纳 token，仍然无鉴权。
3. 覆盖全部租户：配合一次模板切换（改 `VIBE_CUBE_TEMPLATE_ID` 后重启 router；启动清扫默认销毁旧模板上的全部沙箱），各租户下一问冷启新沙箱、首次 `/boot` 采纳 token。数据在宿主，重建无损，首问多一次冷启。
4. 验证（新建的沙箱内经 bash 工具）：`curl -s -o /dev/null -w '%{http_code}' -X POST http://127.0.0.1:8898/stop` → `401`；`curl -s http://127.0.0.1:8898/health` → 200 JSON；正常 `/ask` 成功，router 日志没有 `engine boot failed: 401`。

回滚：

- **只关鉴权，router 不回退**：`router.env` 置 `VIBE_LAUNCHER_AUTH=0` → 重启 router。各租户下一问因指纹变化重启引擎，这次 `/boot` 带 Bearer 头、env 不带 token，launcher 随之解除要求。不需要重建；没有再来的租户沙箱仍持有 token，但新 router 总会带头，不受影响。
- **回退 router.py 到不含 launcher 鉴权的版本**：旧 router 不带 Authorization 头，持 token 的 launcher 一律 401、该租户所有 ask 502，所以必须**同时回退 router.py 与模板 id**：（建议先在新 router 上做一次「只关鉴权」缩小影响面）部署旧 `router.py`，同时把 `VIBE_CUBE_TEMPLATE_ID` 改成**不同于持 token 沙箱所用模板**的 id 再重启——回到旧引擎就用旧镜像重新建一次模板（启动清扫多半已删掉旧模板），只回退 router 就用新镜像再建一个模板拿新 id（新 launcher 在旧 router 下永远收不到 token）。旧 router 的启动清扫（`VIBE_SWEEP_STALE_TEMPLATES` 不为 0 时）会销毁不在当前模板上的全部沙箱；关了清扫时 `get_or_create` 也会在各租户下一问发现 `template_id` 不符、先删后建。验证：挑一个开关开启期间建过沙箱的租户发一问，应冷启成功而不是 `engine boot failed: 401`。
- 单个租户出现 launcher 401（例如 `VIBE_ROUTER_SECRET` 被改）不会自愈：删掉它的沙箱与 state 行，下一问重建。

**残余面**：launcher 与 bash 工具同为 uid 1000。launcher 启动时 `PR_SET_DUMPABLE=0` 挡住同 uid 的 ptrace 与读内存，但挡不住同 uid 的 `kill`；若 guest 里杀掉 launcher 后 `:8898` 能被租户进程重新监听，伪造的 launcher 会在下一次 `/boot` 收到完整 boot env（含共享 LLM 凭据与出境私钥）。根治要改进程模型（launcher 以 root 运行、降权后再拉起引擎，私钥放 root 专属目录或交给 agent 托管），列在放量前的收紧清单里。可以在沙箱里只观察不动手：`pgrep -af launcher; ps -o pid,ppid,user,cmd -p 1`。

### 换模型（不需要动代码）

模型名是运行时参数（`LANGCHAIN_MODEL_NAME`）。「哪些模型拒绝 `temperature`」这份策略同样由 env 表达，不需要重建镜像：

```bash
# 换模型：改这一行 → systemctl restart cube-router；各租户在下一次 /ask 时重启一次引擎换上新模型
LANGCHAIN_MODEL_NAME=claude-opus-5

# 如果新模型也拒绝 temperature，而 llm.py 的内置名单还没收录它：
LANGCHAIN_NO_TEMPERATURE_MODELS=opus-6,某新模型

# 或者干脆对所有模型都不发 temperature：
LANGCHAIN_TEMPERATURE=none
```

换到上下文窗口明显更小的内置模型（如 128k 一类）时，同时设 `VIBE_CONTEXT_WINDOW_TOKENS=<窗口减去输出上限>`，让引擎的压缩阈值按窗口封顶（引擎只会用它压低阈值：`窗口 × 0.8 ÷ 实测估算比例 − 工具 schema 体积`）。

判定实现见 `agent/src/providers/llm.py` 的 `omit_temperature()`。内置名单 `NO_TEMPERATURE_MODELS` 按**版本**精确匹配（`opus-4-7 / opus-4-8 / opus-5 / sonnet-5 / fable / mythos`）而不是笼统的 `opus`/`sonnet`——Opus 4.6、Sonnet 4.6 及更早仍接受 `temperature`，笼统匹配会让它们悄悄丢掉 `temperature=0`。

### 5. 日常运维

```bash
systemctl status cube-router && journalctl -u cube-router -f
systemctl status cube-sandbox-control.target          # CubeSandbox 全栈

# router 健康（池状态 + ask 计数器/p50/p95 + disk 段：整盘 disk_used_pct、超水位租户 tk8 列表；Bearer 必带）
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8990/healthz | jq .
curl -s -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:8990/tenants/usage?limit=10" | jq .

# 每次 /ask 的分段计时与结局（attempt_id 可与 laicai deep_engine_runs 对上）
tail -f /var/lib/cube-router/ask_log.jsonl

# 租户引擎日志/trace 在宿主 bind-mount 盘上直读（无需进沙箱）：
#   /data/shared/vibe/<tk>/logs/engine.jsonl
#   /data/shared/vibe/<tk>/sessions/<sid>/trace.jsonl
# 也可经 /obs/* 端点在线取：laicai 的执行 Trace 页用 /obs/trace、/obs/swarm-events、/obs/prompt；
# /obs/ask-log 与 /obs/engine-log 目前 laicai 没有页面在用，只能 curl：
curl -s -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:8990/obs/ask-log?uid=<laicai userId>&attempt_id=<id>" | jq .

cat /var/lib/cube-router/state.json                   # 租户 → 沙箱映射
# WebUI http://<宿主机>:12088 可视化查看沙箱列表/状态
# 手动操作单个沙箱（E2B 兼容 CubeAPI）：
curl -s -H "X-API-Key: e2b_000000" -XPOST http://127.0.0.1:3000/sandboxes/<id>/resume
```

日常排障入口优先用 laicai 管理端：深度引擎看板 `/app/admin/deep-engine` → 点明细行进调用详情页 `/app/admin/deep-run/$id`（库内数据：链路瀑布、逐工具耗时、数据缺失表）→ 执行 Trace 页 `/app/admin/deep-trace/$id`（经 `/obs/trace`、`/obs/swarm-events`、`/obs/prompt` 在线读租户 trace、swarm 事件与完整输入 prompt）。router 调用日志与引擎日志没有页面，按上面的 curl 或下机器看。命令行五步追查见 [docs/OBSERVABILITY.md §10](docs/OBSERVABILITY.md)。

## 本地开发（引擎单实例）

单机运行不需要任何多租户设施：

```bash
pip install -e .            # 仓库根；Python ≥3.11（生产用 3.12）
# LLM 配置在 agent/.env：LANGCHAIN_PROVIDER / LANGCHAIN_MODEL_NAME
#   + OPENAI_API_KEY / OPENAI_BASE_URL（或 ANTHROPIC_*），详见上游文档
vibe-trading serve --host 127.0.0.1 --port 8899
```

- `serve` 默认 `--host 0.0.0.0 --port 8000`；本地调试建议显式绑 loopback。
- 单机模式**不要**设 `VIBE_MULTITENANT=1`（缺 `VIBE_DATA_DIR` 会 fail-loud 拒绝启动，这是设计行为；设了还会取消 loopback 免鉴权）。设了 `VIBE_TRADING_TENANT_SAFE` 或 `VIBE_MULTITENANT` 任一项时引擎不读任何 `.env`，LLM 配置要走进程 env。
- 上游 Web UI：`frontend/` 下 `npm install && npm run dev`（生产不使用）。

## 已知坑

- **沙箱 pause 后，数据面流量不会自动唤醒它**（one-click 形态的 cube-proxy 行为）：必须显式 `POST /sandboxes/<id>/resume`。router 已内建处理（launcher 探活失败 → resume → 重试），手工 curl 沙箱调试时要自己 resume。
- **E2B SDK 默认给沙箱 5 分钟 TTL**（endAt 到期即销毁）。永不过期的沙箱必须用裸 CubeAPI 创建且**不带 timeout**（one-click 的 `default_timeout_insec=-1`），router 即如此；勿用 SDK 默认参数建租户沙箱。
- **引擎在多租户档不信任任何调用方**：所有对**引擎** `:8899` 的请求必须带 `Authorization: Bearer <API_AUTH_KEY>`（router 每次 boot 随机生成并持久化在 state.json）——经 cube-proxy 进来的本就不是 loopback，而 `VIBE_MULTITENANT=1` 下 guest 内的 loopback 调用（沙箱里手工 curl 调试也算）同样要带 key。引擎进程 non-dumpable，同 uid 读不到它的 `/proc/<pid>/environ`。**launcher `:8898` 默认无鉴权**（`/health` `/boot` `/stop` 裸 HTTP）：宿主外没有暴露面，但 guest 内经 loopback 可达；`VIBE_LAUNCHER_AUTH=1` 让新建沙箱的 launcher 要求 router token（`/health` 始终开放），未开时 router 启动打 warning，见「launcher 鉴权：开启与回滚」。
- **cube-router 以 root 运行**（`cube-router.service` `User=root`）——它需要读写 `/data/shared/vibe/<tk>`（owner 1000:1000）、调 `cubemastercli`、访问 CubeAPI socket。**TODO：降权**（改成专用用户 + 对 CubeAPI socket/`cubemastercli` 的权限梳理 + 租户目录 gid 共享）；在此之前所有宿主直读直写租户目录的端点都必须过 `_safe_tenant_path` 的 symlink/越界守卫，这是 root 侧唯一的防线。
- **阿里云北京机房出网限制**：Docker Hub 直连超时（配镜像加速）；镜像内 apt/pip 用 mirrors.huaweicloud.com（`Dockerfile` 已内置）——阿里云镜像站对 HTTP/1.1 客户端（apt、pip）限速到约 100 kB/s，只有 HTTP/2 的 curl 才快，一次构建会拖到小时级；华为云走 HTTP/1.1 有 10 MB/s 以上。境外搜索/雅虎数据现经**沙箱内 SSH 隧道 + B 服务器白名单代理**出境（见 docs/OBSERVABILITY.md §6）；A 股链路 akshare/tushare/mootdx 可能抖动，`market_data` 会沿降级链自动换源。
- **明文 HTTP 代理跨境必死**：`CONNECT <被墙域名>` 行明文过境会被按关键字重置（实测 duckduckgo 0.13s 秒断、yahoo 通）——出境代理必须走加密隧道，这是隧道端点放进沙箱的根本原因。
- **B 端 tinyproxy 的域名白名单是全局的**：laicai market-data 的 md 隧道流量同受约束，market-data 新增境外数据域时要同步补 `/etc/tinyproxy/filter`；引擎侧三个消费方（`web_search` 的搜索引擎域、`read_url` 的 `r.jina.ai`、yfinance 的 yahoo/yimg）少放行任何一个，对应工具在沙箱内就会整体失败而不是退回直连。另两个 tinyproxy 坑：Ubuntu 的 AppArmor 只放行规范路径，filter 文件需在 `/etc/apparmor.d/local/tinyproxy` 加 `file r` 规则；conf 无 `LogFile` 时日志在 journald 而非 /var/log。
- **ddgs 9.x 已移除 google/bing 后端**：传旧列表会 warning 并缩小引擎池；用 `auto`。数据中心出口 IP 被各免费搜索引擎随机反爬属常态，空结果不等于链路故障（先看 launcher `/health` 的 `egress_tunnel`）。
- **LLM 代理模型名只认短横线**：`claude-opus-4-8` 可用，`claude-opus-4.8` 404。
- **`VIBE_ROUTER_SECRET` 不可轮换**：它决定每个租户的身份派生（HMAC），轮换等于把所有租户的沙箱与数据全部孤立；launcher token 也由它派生，开着 `VIBE_LAUNCHER_AUTH` 时改它会让所有持 token 的沙箱 401。
- **记忆目录的 `.MEMORY.lock` 不跨宿主/来宾互斥**：租户目录是宿主 bind-mount 进 MicroVM 的，`flock` 只在同一内核内有保证——引擎内多 attempt 之间有效，router `/memory/delete` 的 flock 只防宿主侧并发编辑，两侧之间的竞争靠「索引由条目文件重建」自愈；router 侧取锁有上限（`VIBE_MEMORY_LOCK_TIMEOUT_S`，默认 5s，非阻塞 + 重试），超时记一行 warning 后不加锁照删。
- **GoalStore 与会话搜索索引共用文件名** `~/.vibe-trading/sessions.db`（上游两处硬编码同名，两个不同对象、不同锁写同一文件有损坏风险）；需要分离时用 `VIBE_TRADING_GOAL_DB_PATH` 指到别处——它不在转发名单里，多租户下只能写进镜像 `ENV`。
