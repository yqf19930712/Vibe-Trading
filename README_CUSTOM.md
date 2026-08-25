# Vibe-Trading（来财AI 深度引擎）

本仓库是 [HKUDS/Vibe-Trading](https://github.com/HKUDS/Vibe-Trading)（自然语言量化研究 agent，Python + LangChain + FastAPI）的 fork，在上游引擎之上增加一层**多租户生产运维层（`ops/`）**，作为 laicai（来财）「来财AI 深度引擎」的后端：laicai 的 AI 聊天把深度分析请求透传给本仓库部署的 cube-router，每个 laicai 用户在独立的 KVM MicroVM 沙箱里运行一个专属引擎实例。

- 引擎本体的功能与用法（回测、因子、swarm、connector、MCP 等）见 [README.md](README.md)（上游自述，保持原样便于合并上游）与 [vibetrading.wiki](https://vibetrading.wiki/)，本文不复述。
- 多租户架构与协议契约见 [PRODUCT_DESIGN.md](PRODUCT_DESIGN.md)；观测/预算/数据可靠性/出境代理见 [docs/OBSERVABILITY.md](docs/OBSERVABILITY.md)。
- swarm 多智能体团队的 29 个 preset 清单与来财AI 触发策略见 [docs/SWARM-PRESETS.md](docs/SWARM-PRESETS.md)。
- 方案演进、评审记录与已退役的 v1 进程版见 [docs/HISTORY.md](docs/HISTORY.md)。

## 仓库结构

| 路径 | 说明 |
|---|---|
| `agent/` | 上游引擎本体（`api_server.py` HTTP API、`src/` agent/工具/回测、`cli/`）。含少量本 fork 维护的差异，见下节 |
| `ops/cube-router/` | **现行生产编排器**：FastAPI 单文件，对 laicai 暴露 `/ask`，按租户创建/复用 CubeSandbox MicroVM |
| `ops/cube-engine/` | 沙箱引擎镜像：`Dockerfile`（python:3.12-slim + 本仓库源码）+ `launcher.py`（guest 内进程管理器，模板探针目标） |
| `ops/vibe-router/` | 已退役的 v1 进程版编排器（同机多进程隔离），源码与 runbook 保留存档，沿革见 [docs/HISTORY.md](docs/HISTORY.md) |
| `frontend/` | 上游 React Web UI。生产不使用（镜像里放空 `frontend/dist` 占位） |
| `wiki/` `scripts/` `tools/` | 上游站点与 CI 杂项，与多租户层无关 |

## 与上游的差异（`agent/` 内）

均为可长期携带的通用化改动，跟随上游合并时需保留：

- **单一数据根 `_data_root()`**（`agent/api_server.py`）：`runs/` `sessions/` `uploads/` 目录可被 `VIBE_DATA_DIR` 重定向；`VIBE_MULTITENANT=1` 而缺 `VIBE_DATA_DIR` 时启动即报错（fail-loud，杜绝租户状态静默写进共享安装目录）。
- **租户安全档位**（`agent/src/tools/__init__.py`）：`VIBE_TRADING_TENANT_SAFE=1` 时 `build_registry` 排除 `trading_*` 前缀全部工具与 `propose_mandate_profiles`（动钱红线）；shell 类工具另由上游的 `VIBE_TRADING_ENABLE_SHELL_TOOLS=1` 门控制。
- **LLM 兼容性**（`agent/src/providers/llm.py`）：模型名含 `opus-4-8` / `fable` / `mythos` 时省略 `temperature` 字段（这些模型经 OpenAI-compat 代理会拒绝该参数）；流式默认带 `stream_options.include_usage`（`LANGCHAIN_STREAM_USAGE=0` 可关），否则 `llm_usage` 事件恒空。
- **可观测性**（详见 [docs/OBSERVABILITY.md](docs/OBSERVABILITY.md)）：`src/core/logging_setup.py` 结构化 JSONL 日志（contextvars 绑 session/attempt id，穿透工具线程）；AgentLoop 收口发 `attempt_stats` 事件（迭代/LLM·工具耗时/tokens/逐工具/数据源统计，SSE + trace 双写）；数据链路 print 改结构化 logger。
- **预算与提前收敛**：messages API 增 `deadline_s`；`src/core/budget.py` deadline contextvar + `cap_timeout`；AgentLoop 剩余 <25% 注入收尾提示、不足一轮强制出文本（`early_finalize`）；单工具/swarm 超时被剩余预算钳制；`VIBE_MAX_ITERATIONS` env 化。
- **数据可靠性**：`market_data` 空结果/异常沿 `FALLBACK_CHAINS` 逐源降级并输出 `_gaps` 明细；`src/core/fetch_stats.py` attempt 级数据源记账；tushare 进程内节流（`TUSHARE_MAX_PER_MIN`）+ 重试；启动 `socket.setdefaulttimeout` 兜底无超时 SDK。美/港股日线备源 `ifind`（同花顺 iFinD MCP，国内端点不走隧道，`IFIND_MCP_TOKEN` 鉴权；自然语言 quotes 工具 → markdown 表头驱动解析，仅日频，见 `backtest/loaders/ifind_loader.py`）。
- **出境代理**：`web_search`（ddgs `proxy` 参数）与 yfinance loader 读 `VIBE_TRADING_EGRESS_PROXY`；搜索后端默认 `auto`（ddgs 9.x 已无 google/bing）；`ops/cube-engine/launcher.py` 按 `/boot` env 在 guest 内拉起 SSH 隧道（镜像 +openssh-client）。

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

### 3. cube-router 部署

```bash
# /opt/cube-router/{router.py,requirements.txt} + venv
python3 -m venv /opt/cube-router/.venv
/opt/cube-router/.venv/bin/pip install -r requirements.txt   # fastapi/uvicorn/httpx/pydantic

# env：/opt/cube-router/router.env（chmod 600）
# systemd：cp ops/cube-router/cube-router.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now cube-router
```

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
| `VIBE_IDLE_TTL_S` | | 空闲 pause 阈值，默认 1200 |
| `VIBE_READY_TIMEOUT_S` / `VIBE_POLL_INTERVAL_S` / `VIBE_ASK_TIMEOUT_S` | | 就绪预算 180s / 轮询间隔 3s / 单问默认超时 900s |
| `OPENAI_*` `ANTHROPIC_*` `LANGCHAIN_*` `TUSHARE_TOKEN` `IFIND_MCP_TOKEN` `VIBE_TRADING_SEARCH_BACKENDS` | | 内置默认 LLM 凭据与数据源配置，经 launcher `/boot` 转发进每个租户引擎（完整清单见 `router.py` 的 `FORWARD_ENV`） |
| `LANGCHAIN_TEMPERATURE` | | `none` / `off` / 空 = 任何模型都不发 `temperature`；否则按数值发。填了非数字会 warning 后回落 `0.0` |
| `LANGCHAIN_NO_TEMPERATURE_MODELS` | | 逗号分隔的模型名子串，**追加**到 `llm.py` 内置的 `NO_TEMPERATURE_MODELS` 名单（追加而非替换，避免为了加新模型把已知的漏掉） |
| `VIBE_ASK_LOG` | | 每次 `/ask` 一行的观测日志，默认 `/var/lib/cube-router/ask_log.jsonl`（20MB 轮转） |
| `VIBE_EGRESS_KEY_FILE` / `VIBE_EGRESS_SSH_DEST` | | 沙箱出境隧道：宿主上的 SSH 私钥路径（如 `/root/vibe-egress-key`）+ 目的地（如 `root@<B服务器>`）。配了才会给引擎注入 `VIBE_TRADING_EGRESS_PROXY`；B 端该 key 必须 `restrict,port-forwarding,permitopen="127.0.0.1:8888"` |
| 租户档位覆盖 | | `engine_env()` 会给每个租户注入默认档位：`VIBE_MAX_ITERATIONS=25`、`VIBE_TRADING_DATA_CACHE=1`、`VIBE_TRADING_TOOL_TIMEOUT_SECONDS=300`、`SWARM_TIMEOUT=1800`、`VIBE_TRADING_SEARCH_BACKENDS=auto`——在 router.env 里设同名变量即可整体覆盖 |

laicai 侧只需在 `web.env` 配 `VIBE_ROUTER_URL=http://<宿主机>:8990` + `VIBE_ROUTER_TOKEN`。

### 4. 更新操作

两条路径的影响面完全不同：

- **只改 router**（`ops/cube-router/router.py`）：scp 覆盖 `/opt/cube-router/router.py` → `systemctl restart cube-router`。沙箱不受影响——重启后从 `state.json` 重挂既有租户沙箱，不泄漏、不丢数据。
- **改引擎代码**（`agent/`）：重建镜像 → push → `cubemastercli tpl create-from-image` 发新模板 → 更新 `VIBE_CUBE_TEMPLATE_ID` → restart cube-router。**不需要动既有沙箱**：router 在 `get_or_create` 里比对 `state.json` 里记的 `template_id`，不一致就删掉旧沙箱、用新模板重建。租户数据在宿主 bind-mount 里，重建无损。

  > 这是 2026-08-21 之后的行为。在那之前引擎数据在沙箱可写层里，换模板只影响新建沙箱，老租户要么继续跑旧代码、要么 `/forget` 丢数据——踩过一次，见 `docs/HISTORY.md`。
- **只改 LLM 配置**（`FORWARD_ENV` 里的凭据/模型）：改 `router.env` → restart cube-router。下一次 `/ask` 时 LLM 指纹变化会自动触发 launcher `/boot` 重启引擎进程，沙箱与会话数据不动。

### 换模型（不需要动代码）

模型名本来就是运行时参数（`LANGCHAIN_MODEL_NAME`）。以前唯一逼着改代码的是「哪些模型拒绝 `temperature`」那份硬编码名单——而引擎代码烧在沙箱镜像里，改它就要重建镜像并逐个补既有租户沙箱。该策略现已可由 env 表达：

```bash
# 换模型：改这一行 → systemctl restart cube-router，完事
LANGCHAIN_MODEL_NAME=claude-opus-5

# 如果新模型也拒绝 temperature，而 llm.py 的内置名单还没收录它：
LANGCHAIN_NO_TEMPERATURE_MODELS=opus-6,某新模型

# 或者干脆对所有模型都不发 temperature：
LANGCHAIN_TEMPERATURE=none
```

判定实现见 `agent/src/providers/llm.py` 的 `omit_temperature()`。内置名单 `NO_TEMPERATURE_MODELS` 按**版本**精确匹配（`opus-4-7 / opus-4-8 / opus-5 / sonnet-5 / fable / mythos`）而不是笼统的 `opus`/`sonnet`——Opus 4.6、Sonnet 4.6 及更早仍接受 `temperature`，笼统匹配会让它们悄悄丢掉 `temperature=0`。

### 5. 日常运维

```bash
systemctl status cube-router && journalctl -u cube-router -f
systemctl status cube-sandbox-control.target          # CubeSandbox 全栈

# router 健康（池状态 + ask 计数器/p50/p95；Bearer 必带）
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8990/healthz | jq .

# 每次 /ask 的分段计时与结局（attempt_id 可与 laicai deep_engine_runs 对上）
tail -f /var/lib/cube-router/ask_log.jsonl

# 租户引擎日志/trace 在宿主 bind-mount 盘上直读（无需进沙箱）：
#   /data/shared/vibe/<tk>/logs/engine.jsonl
#   /data/shared/vibe/<tk>/sessions/<sid>/trace.jsonl
# 也可经 /obs/* 端点在线取（laicai 管理端详情页就是这么看的）

cat /var/lib/cube-router/state.json                   # 租户 → 沙箱映射
# WebUI http://<宿主机>:12088 可视化查看沙箱列表/状态
# 手动操作单个沙箱（E2B 兼容 CubeAPI）：
curl -s -H "X-API-Key: e2b_000000" -XPOST http://127.0.0.1:3000/sandboxes/<id>/resume
```

日常排障入口优先用 laicai 管理端：运营 Tab「深度引擎」→ 点最近调用明细行 → 详情页有链路瀑布、逐工具耗时、数据缺失表和三个在线日志面板。命令行五步追查见 [docs/OBSERVABILITY.md §10](docs/OBSERVABILITY.md)。

## 本地开发（引擎单实例）

单机运行不需要任何多租户设施：

```bash
pip install -e .            # 仓库根；Python ≥3.11（生产用 3.12）
# LLM 配置在 agent/.env：LANGCHAIN_PROVIDER / LANGCHAIN_MODEL_NAME
#   + OPENAI_API_KEY / OPENAI_BASE_URL（或 ANTHROPIC_*），详见上游文档
vibe-trading serve --host 127.0.0.1 --port 8899
```

- `serve` 默认 `--host 0.0.0.0 --port 8000`；本地调试建议显式绑 loopback。
- 单机模式**不要**设 `VIBE_MULTITENANT=1`（缺 `VIBE_DATA_DIR` 会 fail-loud 拒绝启动，这是设计行为）。
- 上游 Web UI：`frontend/` 下 `npm install && npm run dev`（生产不使用）。

## 已知坑

- **沙箱 pause 后，数据面流量不会自动唤醒它**（one-click 形态的 cube-proxy 行为）：必须显式 `POST /sandboxes/<id>/resume`。router 已内建处理（launcher 探活失败 → resume → 重试），手工 curl 沙箱调试时要自己 resume。
- **E2B SDK 默认给沙箱 5 分钟 TTL**（endAt 到期即销毁）。永不过期的沙箱必须用裸 CubeAPI 创建且**不带 timeout**（one-click 的 `default_timeout_insec=-1`），router 即如此；勿用 SDK 默认参数建租户沙箱。
- **经 cube-proxy 访问引擎不再是 loopback**，引擎的 loopback 信任不生效——所有对引擎/launcher 的请求必须带 `Authorization: Bearer <API_AUTH_KEY>`（router 每次 boot 随机生成并持久化在 state.json）。
- **阿里云北京机房出网限制**：Docker Hub 直连超时（配镜像加速）；镜像内 apt/pip 用 mirrors.aliyun.com（`Dockerfile` 已内置）。境外搜索/雅虎数据现经**沙箱内 SSH 隧道 + B 服务器白名单代理**出境（见 docs/OBSERVABILITY.md §6）；A 股链路 akshare/tushare/mootdx 可能抖动，`market_data` 会沿降级链自动换源。
- **明文 HTTP 代理跨境必死**：`CONNECT <被墙域名>` 行明文过境会被按关键字重置（实测 duckduckgo 0.13s 秒断、yahoo 通）——出境代理必须走加密隧道，这是隧道端点放进沙箱的根本原因。
- **B 端 tinyproxy 的域名白名单是全局的**：laicai market-data 的 md 隧道流量同受约束，market-data 新增境外数据域时要同步补 `/etc/tinyproxy/filter`。另两个 tinyproxy 坑：Ubuntu 的 AppArmor 只放行规范路径，filter 文件需在 `/etc/apparmor.d/local/tinyproxy` 加 `file r` 规则；conf 无 `LogFile` 时日志在 journald 而非 /var/log。
- **ddgs 9.x 已移除 google/bing 后端**：传旧列表会 warning 并缩小引擎池；用 `auto`。数据中心出口 IP 被各免费搜索引擎随机反爬属常态，空结果不等于链路故障（先看 launcher `/health` 的 `egress_tunnel`）。
- **LLM 代理模型名只认短横线**：`claude-opus-4-8` 可用，`claude-opus-4.8` 404。
- **`VIBE_ROUTER_SECRET` 不可轮换**：它决定每个租户的身份派生（HMAC），轮换等于把所有租户的沙箱与数据全部孤立。
- **GoalStore 与会话搜索索引共用文件名** `~/.vibe-trading/sessions.db`（上游两处硬编码同名，两个不同对象、不同锁写同一文件有损坏风险）；需要分离时用 `VIBE_TRADING_GOAL_DB_PATH` 指到别处。
