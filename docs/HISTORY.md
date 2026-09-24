# HISTORY — 多租户方案演进记录

本文归档多租户隔离方案的设计过程、对抗评审、v1 进程版（已退役）与 v2 CubeSandbox 切流的历史记录，内容源自原 `MULTI_TENANCY.md`（已并入本文与 `PRODUCT_DESIGN.md` 后删除）。**现行架构以 [../PRODUCT_DESIGN.md](../PRODUCT_DESIGN.md) 为准**，本文仅供追溯。

## 1. 背景与威胁模型

Vibe-Trading 上游是单用户本地 agent。laicai 早期把它当共享多租户后端（单进程 `127.0.0.1:8899`、每次匿名 `POST /sessions`、不带身份）→ 跨用户泄露。经审计 + 评审补全的**共享状态清单**（同进程 + 同 `HOME` + 同安装目录）：

| # | 共享状态 | 位置 | 跨用户风险 | 隔离手段 |
|---|---|---|---|---|
| 1 | 持久长期记忆 | `Path.home()/.vibe-trading/memory`，`find_relevant()` 无用户过滤 | A 的记忆召回进 B 的 prompt | HOME |
| 2 | 会话搜索索引 + `session_search` 工具 | `~/.vibe-trading/sessions.db`，进程单例 `_shared_index`，工具常驻可搜全部会话 | B 搜到 A 对话正文 | HOME + 进程 |
| 3 | 后台任务管理器 | 单例 `_BG`；`check(None)` 列全部任务 | B 看 A 任务输出 | 进程 |
| 4 | 会话消息存储 | `SESSIONS_DIR=<安装目录>/agent/sessions` | 安装目录共享 | `_data_root()`（B1/B4） |
| 5 | 上传文件（交割单） | `UPLOADS_DIR=<安装目录>/agent/uploads` | A 的交割单被 B 读 | `_data_root()` |
| 6 | swarm 运行产物 | `swarm_runs_root()` 由 `__file__` 派生，且另有两处硬编码重算 | 跨用户 swarm 输出泄露 | B1：三处统一走 `_data_root()` |
| 7 | 券商 OAuth + trading 工具 | `~/.vibe-trading/live/*/oauth/`；`trading_place_order` 始终注册；paper 路径绕过 mandate 门且收 caller host/port | 跨用户凭据复用 / 误下单 | HOME + M2：tenant-safe 排除全部 trading 工具 |
| 8 | 影子账户 | `~/.vibe-trading/shadow_*` | 交易规则跨用户枚举 | HOME |
| 9 | 因子库单例 | `_registry_cache`（含 `sys.modules`） | 共享编译模块 | 进程 |
| 10 | 研究目标库 GoalStore（M7） | 默认 db 与 #2 **同名** `~/.vibe-trading/sessions.db`（两个不同对象、不同锁写同一文件，潜在损坏；`VIBE_TRADING_GOAL_DB_PATH` 可分离） | 研究目标/证据跨用户泄露 | HOME |
| — | `os.environ`/进程全局（m2） | `TUSHARE_TOKEN`、LLM 凭据等写进程 env | 单进程下跨租户配置串 | 仅靠进程边界（故永勿回退单进程） |

**威胁前提**：shell 工具默认关（需显式 env）；`web_reader` 有 SSRF 防护拦 loopback/内网；会话级 MCP 默认剥离。但非 shell 即可达的常驻工具不止 `session_search`——`SwarmTool`（写共享 `.swarm` 且 spawn 子 agent）与 `trading_*`（auto-discover、paper 绕过门）同样常驻，租户安全档位须显式排除而非靠“不下发 MCP 配置”。

**片上隔离之外的风险**（评审完整性补充，原 §12）：

1. 查询载荷把用户真实持仓送往 LLM 提供方——要求明确引擎 LLM 凭据来源（由 router 注入每实例）、评估发往第三方的数据最小化。
2. 经上传交割单 / `web_reader` 内容的提示注入 → 本租户记忆投毒（跨轮复现、survive 回收，非跨用户故隔离防不住）——要求 laicai 提供记忆可见可删入口。
3. `threadId→vibe_session_id` 绑定必须按属主用户作用域，测试矩阵含“跨租户 session 拒绝”用例。

## 2. 方案对比与选型

| 方案 | 隔离边界 | 改 Vibe | RAM 单例 | 资源 | 抗升级 | 结论 |
|---|---|---|---|---|---|---|
| 1 剥离功能（单进程） | 应用 | 中 | 只能禁用 | 最省 | 中 | 牺牲记忆/连续 ✗ |
| **2 进程每用户** | **OS 进程** | **小** | **天然隔离** | 中 | **强** | **选用（v1）** |
| 3 进程内 uid 命名空间 | 应用代码 | 侵入式 | 按 uid 分桶易漏 | 省 | 弱 | 工程量大易留坑 ✗ |

核心洞察：全部落盘状态从 `Path.home()` 或安装目录派生 → **每用户独立 `HOME` + 独立进程**即可同时隔离落盘状态与进程内全局单例。五路对抗评审（隔离完整性 / 会话连续性 / 资源稳定性 / 安全爆炸半径 / 运维故障）一致认可方案 2；资源评审的 reject 系按“多租户并发 swarm”误判，其具体修复已纳入但不触发架构重选。后续 v2 把“进程”升级为“MicroVM 沙箱”，`HOME`/`VIBE_DATA_DIR` 隔离机制原样沿用。

关键边界结论（M1）：`API_AUTH_KEY` **不是**跨实例边界——引擎对所有 loopback 调用方无条件信任（校验 key 前对本地客户端直接放行），真正边界 = 进程 + HOME + 端口（v1）/ MicroVM（v2）。

## 3. 对抗评审追溯（v2 设计定稿，approve-with-changes）

Blocker：

- **B1** swarm 目录三处 `__file__` 硬编码统一经 `_data_root()`（`swarm/store.py` 成唯一真源）。
- **B2** 租户安全档位显式排除常驻可达工具（SwarmTool / session_search / background / trading，后经落地放宽，见 §4.3）。
- **B3** 复用会话必须按 `attempt_id` 轮询本轮终答——沿用旧“取最后一条 assistant”会把上一轮答案立即返回（致命 stale-read）。
- **B4** `VIBE_MULTITENANT=1` 缺 `VIBE_DATA_DIR` 时启动即报错（fail-loud），杜绝静默回落共享安装目录。

Major：

- **M1** `API_AUTH_KEY` 非 loopback 边界；每实例强制 `--host 127.0.0.1`（`serve` 默认 `0.0.0.0`）+ 端口段防火墙。
- **M2** trading 工具是 always-on 注册，必须显式排除（paper 路径还绕过 mandate 门）。
- **M3** 在途请求引用计数，reaper/LRU 淘汰跳过 `refcount>0`，防长回测被误杀。
- **M4** cgroup 硬限额兜底 OOM，保护同机 invest-web/market-data。
- **M5** `/forget` 先 SIGTERM 实例再删目录（勿在活 sqlite/WAL 上删）；路径校验（tenant_key 须 64-hex、resolve 后必须是 users base 直接子目录），绝不把原始 uid 插进路径。
- **M6** per-uid 创建锁 + per-thread 合流，防双开/分脑。
- **M7** GoalStore 与搜索索引 `sessions.db` 撞名（至今上游未改名，靠 env 可分离）。
- **M8** 磁盘无 governor（runs/sessions/uploads 单调增），需配额 + 保留期清扫。

Minor / 补充：m1 冷启动 5–15s（重 import + CJK 字体下载 + matplotlib 缓存），预置字体 + 共享只读 `MPLCONFIGDIR` 缓解；m2 `os.environ` 仅靠进程边界隔离（勿回退单进程）；m3 `/forget` 路径校验；m4 `MAX_HISTORY_CHARS=12000` 裁旧轮次、无会话压缩，长线程靠长期记忆；m5 router 强制 Bearer（即使 loopback）+ 孤儿回收 + `/healthz` 暴露 RSS/磁盘/孤儿计数；完整性补充（网络出口/提示注入/跨租户 session 拒绝）。

另一个不变量自评审起贯穿至今：**`ROUTER_SECRET` 实为 schema key（决定每租户身份派生），轮换即孤立全部数据，按不可轮换对待。**

## 4. v1 进程版：设计与落地（2026-06，已退役）

### 4.1 架构

单台 VPS（4 vCPU / 7.4G，与 invest-web/market-data 同机）上，`vibe-router`（FastAPI，loopback `:8990`，独立 systemd unit，非 root 用户 `vibe`）按 `tenant_key=HMAC(ROUTER_SECRET, uid)` 懒启动/复用/空闲回收每用户一个 `vibe-trading serve` **host 进程**：

- 租户目录 `/srv/vibe/users/<64-hex>/` 作为该实例 `HOME`，`VIBE_DATA_DIR=$HOME/.vibe-trading`；实例端口从 8901 起，显式绑 `127.0.0.1`。
- 机制全集：per-uid 创建锁 + per-thread 合流（M6）、在途 refcount 反误杀（M3）、attempt_id 轮询（B3）、空闲 20min SIGTERM 回收、`MAX_INSTANCES=4` LRU 淘汰、孤儿进程按 env 标记精准回收（m5）、`/forget` 先停进程再路径校验 `rmtree`（M5/m3）。
- 源码保留在 `ops/vibe-router/`（router.py + systemd unit + 安全测试 + 部署 runbook）。

### 4.2 与设计的关键偏差

1. **cgroup 限额 = 池级 `MemoryMax=5G`**（非设计的 per-instance `systemd-run` 1.6G）：router 以非 root `vibe` 运行，`systemd-run --scope -p MemoryMax` 需 root；靠 router unit 自身 cgroup（`Delegate=yes` + `KillMode=control-group`，子实例继承）兜底整池，`OOMScoreAdjust` 让内核优先杀 Vibe 池而非 web。`VIBE_USE_SYSTEMD_RUN=1` 可 opt-in per-instance。
2. **测试阶段租户安全档位放宽**：设计原值额外排除 `SwarmTool`/`session_search`/`background_*`，落地只裁 `trading_*` + `propose_mandate_profiles` 红线，并注入 `VIBE_TRADING_ENABLE_SHELL_TOOLS=1`（连带前台 `bash` = host 任意命令执行）。资源放大仅靠池级 cgroup 兜底——此妥协正是 v2 沙箱化的直接动因。
3. **laicai 侧连续性**：真实 threadId 经 URL `?lt=` 查询参数到达 `/api/chat`（`useChat` 会覆盖 body 里的 threadId）；深度引擎双触发 = 显式点名「用来财AI…」强制必调 + 线程已绑定 `vibe_session_id` 时软挂工具。

### 4.3 部署坑（v1 特有，已随退役归档）

- **python 软链坑**：agent venv 与 router venv 的 python 软链到 `/root/.local/share/uv/python`（root 私有），`vibe` 用户 exec 不到 → systemd rc=203。修法：`readlink -f` 取真实带版本号的 python 目录 `cp -aL` 到 `/opt/vibe-py312`（`chmod -R a+rX`），把两个 venv 的 `bin/python`、`bin/python3.12` 软链重指过去（勿 cp 无版本号的中间软链，否则仍指回 /root）。
- 回滚约束：legacy 单实例（`:8899`）不得带 `VIBE_MULTITENANT=1`（会 fail-loud）；laicai `web.env` 同时配 `VIBE_ROUTER_URL`（优先）与 `VIBE_API_URL`（回退）。

### 4.4 资源模型（v1 实测推导）

单实例空闲 RSS ≈ 437MB，warm ≈ 0.7–1.1G，单回测峰值 ≈ 1.2–1.5G，swarm 峰值 ≈ 1.7–3.3G；池预算 ~5G → `MAX_INSTANCES=4`、`MAX_CONCURRENT_ACTIVE=2`、冷启动 5–15s。磁盘无 governor（M8）列为待办，未及实施即被 v2 取代（v2 由 4G writable layer 天然封顶）。

### 4.5 验收（生产 + Playwright 实测）

- **隔离**：租户 B 召不回/搜不到租户 A `remember` 的内容；A 的 uploads/shadow/sessions.db/goals/.swarm B 不可见。✓
- **触发门**：真实 nanoid 到达服务端，explicit/bound 双触发生效。✓
- **连续性**：同线程追问 router 复用既有 session（非新建），引擎准确引用上一轮的三笔加仓价位并校准——只有复用 session 才可能知道“原来那三笔”。✓
- **效率**：复用 session ≈1min vs 冷启 ≈4.5min。冷启拆解（trace）：LLM 多步往返 252s（88%）+ 工具执行 34s（12%）+ 进程冷启数秒——慢在 agent 多步推理（17 步 thinking + 25 次工具调用），非多租户架构开销。
- **工具档位实证**：shell ON 共 39 个工具，`session_search`/`run_swarm`/`background_run`/`bash` 在册，`trading_*`/`propose_mandate_profiles` 缺席；tenant-safe 门与 shell 门正交。端到端 `bash`×13 全部落在租户目录内。
- 遗留（部分随 v2 解决）：shell = host 任意命令执行仅靠 cgroup 兜底（→ v2 解决）；「来财AI 回顾历史」功能层未闭环（`session_search` 在册但引擎倾向现场重算，外层模型也不主动转交——**至今仍存在**）。

### 4.6 v1 期间的协议演进

- `/ask` 从一次性 JSON 改为 **NDJSON 流式**（progress 帧转发引擎 `/sessions/<sid>/events`，`replay=active`；末帧 answer/error）——该协议原样延续到 v2。
- `/ask` 增加 `model` 覆盖与 `llm{}` BYOK：实例身份 = LLM 配置指纹，切换配置 = kill + respawn 注入新 env（v2 改为仅 launcher `/boot` 重启引擎，沙箱不动）。

## 5. v2：CubeSandbox 沙箱化切流（2026-07-22）

动机：v1 遗留红线——`bash`/`background_run` 是 **host** 任意命令执行，仅靠 cgroup 兜底。正解 = 把每租户实例从「同机进程」搬进「KVM MicroVM 沙箱」（[TencentCloud/CubeSandbox](https://github.com/TencentCloud/CubeSandbox)，Apache-2.0，E2B 兼容 API）：shell 落在独立 guest 内核里，host 不再暴露；`HOME`/`VIBE_DATA_DIR` 隔离机制原样沿用（只是搬进沙箱盘）。

### 5.1 切流记录

- 宿主：阿里云 ECS 182.92.217.17（4C8G，北京，Ubuntu 22.04）。无嵌套虚拟化 → 换 PVM 宿主内核（OpenCloudOS `6.6.69-*.cubesandbox.pvm.host`）+ `modprobe kvm_pvm`；100G 数据盘 XFS(reflink) 挂 `/data/cubelet`；CubeSandbox one-click v0.5.1（`CUBE_PVM_ENABLE=1`）。
- 生命周期映射（v1 → v2）：spawn 进程 → create sandbox（不带 timeout = 永不过期）+ launcher `/boot`；idle kill → pause（盘+内存保留，resume 秒级）；LLM 切换 kill+respawn → 仅 `/boot`（sessions 不丢）；forget = rm -rf HOME → delete sandbox；cgroup MemoryMax → 沙箱规格 2C/2G + MicroVM 硬隔离。
- cube-router 对 laicai 协议与 v1 完全兼容，laicai 只改 `VIBE_ROUTER_URL` 指向即完成切流；安全组放行 8990 ← laicai web 主机 IP。
- 切流当日实测：创建沙箱 → 引擎 healthy ≈ **12s**（vs v1 冷启分钟级）；生产全链路（「用来财AI…」→ 新建租户沙箱 → 5 帧 progress → 真实行情结论 → 用量入账）通过。
- 实测坑（沉淀进现行文档）：数据面经 cube-proxy 非 loopback → 必须 Bearer `API_AUTH_KEY`；pause 后代理流量不自动 resume；E2B SDK 默认 5min TTL；北京机房出网退化（Docker Hub / google / yahoo）。
- 老租户数据（`/srv/vibe/users`）未迁移：老线程首问 `_SessionGone` 自动重建会话，长期记忆重新积累——接受。

### 5.2 v1 退役

- 切流初期 v1（Vultr）保留作回滚路径：恢复 `web.env` 备份 + restart 即秒级回退。
- 2026-07-23 Vultr 旧引擎停用（`systemctl disable --now vibe-router vibe-trading`，文件保留）；2026-08-02 Vultr 整机下线，v1 仅存 `ops/vibe-router/` 源码存档。

## 6. 测试矩阵（验收基线）

设计定稿时确立、v1 生产验收执行通过，v2 切流复验核心项。后续动隔离/连续性相关代码时按此回归：

| 类别 | 用例 |
|---|---|
| 隔离 | A `remember` 的内容 B 召不回/搜不到；A 的 uploads/shadow/sessions.db/goals/swarm 产物 B 不可见 |
| 工具档位 | tenant-safe 下工具列表无 `trading_*`/`propose_mandate_profiles`（现行档位；设计原值还含 swarm/search/background） |
| 跨租户 session 拒绝 | B 用 A 的 `vibe_session_id` 发消息 → 404/拒绝，不串答 |
| 连续性 | 同线程两轮答案不同（B3 回归）；跨线程长期记忆本人可召回；`vibe_session_id` 失效 → 透明新建并回传新 id |
| 资源 | 并发超限排队不 OOM；限额生效；在途长任务不被回收误杀（refcount） |
| 故障 | 实例/沙箱被杀 → 下次自动重建；router 重启 → 不泄漏（v1 清孤儿 / v2 state.json 重挂）、用户数据不丢 |
| 注销 | `/forget` 后数据删除（v1 路径校验拒 `../../etc`）、实例/沙箱停 |

## 2026-08-21：租户数据迁出沙箱可写层，改用宿主 bind-mount

**起因。** 把深度引擎从 `claude-opus-4-8` 换到 `claude-opus-5` 时发现，`llm.py` 里「哪些模型拒绝 `temperature`」是硬编码名单，opus-5 漏网。而引擎代码烧在 CubeSandbox 镜像里 —— 改一行代码 = 重建镜像 + 发新模板，**但新模板只作用于新建沙箱**，四个既有租户沙箱照旧跑老代码。当时 CubeAPI / cubemastercli / envd 都没有对既有沙箱写文件或执行命令的通道（envd 的 49983 没经 cube-proxy 暴露），最后是靠引擎自己的 `/upload` + shell 工具逐个补的 —— 一次性的权宜之计，不可持续。

**改法。** 用 CubeSandbox 的 host-mount（官方文档称「持久化存储」）把租户数据挪出沙箱可写层：

- cubemaster `conf.yaml` 加 `extra_conf.allowed_host_mount_prefixes: ["/data/shared/"]`
- 宿主每租户一目录 `/data/shared/vibe/<tenant_key>`（owner 1000:1000 = 镜像里的 `vibe` 用户）
- router 建沙箱时传 `metadata["host-mount"]`，把该目录挂到 `/home/vibe/.vibe-trading`（即 `VIBE_DATA_DIR`）
- `state.json` 记录建沙箱用的 `template_id`；`get_or_create` 发现与当前 `VIBE_CUBE_TEMPLATE_ID` 不符就删旧沙箱重建
- `/forget` 相应地也要删宿主数据目录 —— 数据已不随沙箱消亡

**迁移。** 沙箱可写层就是宿主上一个 ext4 镜像文件（`cubecow-reflink/volumes/tpl-<tpl>-build-rootfs/sb-<sid>-rootfs-gen0`），租户数据在 `disk/<tpl>_0/upper/home/vibe/.vibe-trading`。整个迁移在宿主侧完成、不惊动引擎：reflink 复制镜像 → 对**副本** `e2fsck -fy` 重放日志 → 只读挂载 → `cp -a` 出来。直接 `mount -o ro,noload` 原盘会在最新文件上撞 EBADMSG，因为沙箱是在写入中途被暂停的。

**两个坑。**
- **paused 沙箱删不掉**，CubeAPI 报 `sandbox not in normal state` 并返回 500；而 httpx 不对 500 抛异常，`sbx_delete` 原本只 catch 异常，于是删除失败被静默吞掉、旧沙箱永久泄漏。已改成先 resume 再 delete，并检查状态码。
- 沙箱网络 `denyOut` 封了全部 RFC1918，沙箱回连不了宿主内网 IP —— 想靠「让引擎把数据 curl 回宿主」做迁移这条路走不通。

**结果。** 四个租户数据（12M / 1.5M / 832K / 752K）已落宿主并逐个核对会话数；旧沙箱全部删除，下次请求时 router 用新模板重建。此后 Vibe-Trading 迭代 = 发新模板 + 改 `VIBE_CUBE_TEMPLATE_ID`，沙箱自动重建、数据不动。

## 2026-08-21 → 08-24：可观测性三批次 + 沙箱出境隧道

**背景。** 深度引擎两大痛点——执行时间过长、金融数据偶发缺失——此前完全无法量化：引擎无 logging 配置（INFO 被丢）、271 处 print、无 metrics；router 无计时；laicai 侧零落库。方案定为三批次：①先把「慢在哪、缺在哪」变成数字，②消灭数据静默失败，③拿数据做性能优化。现状文档见 [OBSERVABILITY.md](OBSERVABILITY.md)。

**批次一（08-21，模板 v4）。** 指标主干刻意复用既有 NDJSON/SSE 通道、零新增基础设施：引擎收口发 `attempt_stats` → router 终帧携带 `stats{router,engine}` → laicai 落 `deep_engine_runs`。引擎结构化日志落租户 bind-mount 盘（宿主直读）；`attempt_id` 定为全链路 trace id；顺手修掉 `/healthz` 缺失的鉴权。评估过 Prometheus/Grafana，单台 8G 宿主机单人运维不值，弃。

**08-24 超时事故（观测链路首战）。** 用户手机端「处理超时」。数据还原：一句提问引擎跑了 40 迭代 19.4 分钟——**恰好在迭代上限 50 的 80% 收尾提醒处停下**，但外层预算 15 分钟早已到期：答案写进了会话却没人收；router 504 后引擎不取消继续烧，把同租户重试在实例锁上拖了 872s。三个教训直接变成批次三的需求：收尾要以墙钟而非迭代数驱动、超时必须取消、迭代上限 50 太奢侈。

**批次二+三（08-24，模板 v5）。** 数据侧：空结果也走降级链 + `_gaps` 明细、fetch_stats 记账、tushare 节流重试、socket 超时兜底、租户默认开 loader 缓存。预算侧：`deadline_s` 全链单向传递，剩余 <25% 收尾提示、不足一轮强制出文本（early_finalize），工具/swarm 超时被剩余预算钳制，router 未答即 cancel，迭代上限 env 化（租户 25）。**预取暖缓存被有意搁置**：loader 缓存是精确区间内容寻址键，预取命中率趋零，等区间感知缓存再做。验收：150s 紧预算重问题 126.7s 交付「明标未完成部分」的部分答案。

**沙箱出境隧道（08-24，模板 v6）。** trace 显示 web_search 三连 `ConnectError` 每次白烧 ~32s（占 attempt 12%）。第一版方案（B 端 tinyproxy 直接对引擎机 IP 开放）实测失败并留下重要结论：**明文代理的 CONNECT 行过境会被按域名关键字重置**（duckduckgo 0.13s 秒断、未封锁的 yahoo 能通）——这正是当年 market-data 用 SSH 隧道的原因。沙箱又够不到宿主隧道端点（denyOut 封 RFC1918、阿里云公网 IP hairpin 不可靠），最终把隧道端点放进沙箱：launcher 起 `ssh -L`，key 经 `/boot` 下发、B 端 `restrict,permitopen` 强约束；tinyproxy 收回 loopback 并加域名白名单。排障路上另拾三坑：tinyproxy 的 AppArmor 规范路径、无 LogFile 时日志在 journald、**ddgs 9.x 已删 google/bing 后端**（默认改 auto）。验收：沙箱内 web_search 真实搜到英伟达财报新闻 5 条，6 秒完成。

## 2026-08-24：swarm 2 小时预算 + attempt_stats 穿透 swarm 线程（模板 v8）

**背景。** 当晚一次 `investment_committee` 深度调用（run #7，attempt `0b23b324369d`）失败：swarm 跑到 21 分钟时 `bull_advocate` worker 连续两次（第 10 轮 + 任务重试后第 11 轮）撞上 LLM 流式 `ReadTimeout`——httpx 读超时 `TIMEOUT_SECONDS` 默认 120s，opus 级模型在长上下文下的思考停顿超过了它——单任务失败连锁 block 下游 risk_officer / portfolio_manager，整队报废。同时详情页 Skill 面板对 swarm 场景恒为空：`FetchStatsCollector` 靠 contextvar 传播，而 `SwarmRuntime` 用裸 `threading.Thread` 起 run、层内用 `ThreadPoolExecutor` 派发 worker，两跳都不继承 context，worker 里 `load_skill` 的 `record_skill` 全部落到 no-op。

**改动（引擎 6e2c580 + f440f26，模板 v8 = tpl-0ca4e4c7551642e4a385d860）。**
- swarm 预算全链 1800→7200：laicai `SWARM_TIMEOUT_S`、router 租户下发 `SWARM_TIMEOUT`、引擎默认值三处同调；laicai `DEEP_PENDING_MAX_AGE_MS` 联动放宽到 121 分钟。等待仍被 attempt 剩余预算钳制，不会倒挂。
- router 租户注入 `TIMEOUT_SECONDS=300`（LLM 流式读超时），吃掉思考停顿；真死上游仍在单轮迭代内暴露。
- `SwarmRuntime` 两跳都用 `contextvars.copy_context()` 包装（run 线程 spawn 时一份、每次 executor submit 一份），swarm worker 的 skill 调用 / 数据抓取 / gaps 从此计入调用方 `attempt_stats`，详情页 Skill 面板在 swarm 场景开始有数据。回归测试 `test_swarm_fetch_stats_propagation.py` 分别钉住两跳。
- 顺带解开一个虚惊：`run_swarm` 是写工具，loop 的工具超时对写工具只警告不杀（当晚 run 21 分钟 > 租户工具超时 300s 仍跑完即为此），此前无人写下这条语义。

**部署。** v8 镜像走既有 runbook（`/root/vibe-build` 构建 → 本机 registry → `tpl create-from-image` → 改 `VIBE_CUBE_TEMPLATE_ID` → restart cube-router），冒烟租户验证新模板 13.4s 冷启动出答案；存量租户下次调用自动换新模板，数据在宿主 bind-mount 不动。laicai 侧同日 `deploy:vps` 上线（014de12）。

## 2026-08-25 → 08-27：美/港股国内直连备源、read_url 走代理、原生 Anthropic 通道、迭代上限回 50、/obs/prompt 与模板清扫

三天里两条线并行：08-24 那次深度调用的复盘（run #7–#10）暴露的数据链与冷启动问题，和 router 侧的运维端点。

- **美/港股国内直连备源**（08-25，`aee8bf5` ifind、`e9d9a86` tickflow、`531414e`/`499db26` 链序、`8568cfc`）：新增 `backtest/loaders/ifind_loader.py`（同花顺 iFinD MCP，`IFIND_MCP_TOKEN`）与 `tickflow_loader.py`（api.tickflow.org，`TICKFLOW_API_KEY`），us_equity 链改为 tickflow→ifind→yfinance→akshare、hk_equity 链 ifind→tickflow→yfinance→tencent→futu→akshare——yfinance 依赖隧道且被 Yahoo 限频，降为兜底。复盘发现 `_SOURCE_PATTERNS` 的 primary 判定仍指 yfinance（attempt `f9b0c0cdcded` 主查询照旧走 Yahoo），改为与链首对齐；裸美股 ticker 与 Yahoo 特殊符号（`GC=F` / `^TNX` / `DX-Y.NYB`）此前落到 a_share 默认链，run #7/#8 因此 9 个标的全部 gap，加了三条模式行。`data-routing` 决策树与两个独立 skill 同日补齐（`0254652`、`2ad7acc`）。
- **冷启动双故障根修**（08-25，`aae5b93`、`b11e87b`）：loader 注册表的冷启动竞态（空注册表被锁存）改为加锁 + 空表不锁存并重试；launcher 的出境隧道从「只在 /health 时拉」改为常驻 keeper 线程自愈——冷启时 ssh 在 guest 网络就绪前死掉，整个 run 的代理调用全部 ECONNREFUSED（attempt `88e080ef0a46`）。
- **read_url 走出境代理 + 连接快败**（08-25，`093d9d2`、`acdfd66`）：`r.jina.ai` 直连不可达时 30s 连接死等在单次调用里烧掉 90s，改为连接 5s / 读 30s 并经 `VIBE_TRADING_EGRESS_PROXY`；Jina 对阿里云 AS20473 禁匿名（401），加 `JINA_API_KEY`。同一提交让 router 优雅退出时取消在飞 attempt（run #9 一次部署重启后孤儿 attempt 烧了 27 分钟）。
- **观测补齐**（08-25，`646af63`、`5c8ec45`、`9ce9833` 等）：swarm worker 的 token 用量并入 `llm_usage` 事件（run #8 主循环只记 21k 输出 token，两个 swarm run 实际烧掉数十万）、租户锁等待计时（run #9 有 93 分钟静默锁等待被读成巨大的 first_progress）、任务真实完成时间戳、`llm_call` 留痕、`stream_retries` 计数；观测预览不再钝器脱敏 `content` 字段（它把 skill 全文 / 文件写入 / 报告一律遮掉，deep-trace 页无法排障）。
- **主循环两处死锁修复**（08-26，`77de03d`、`1c80b58`）：去重护栏改按 (工具, 参数) 键并在结果被 microcompact 清掉后放行——attempt `dea1222743ef` 里名字级护栏拒绝了所有后续 `get_market_data`，模型被逼着把真实数据「撤回」成臆测；工具参数按 schema 无损强转——attempt `052d98f52286` 的 OpenAI 兼容通道把 `max_rows` 发成 `"0"`、数组参数发成 JSON 字符串，工具深处 TypeError 连烧四次重试。同日 `get_realtime_quotes`（`6b22846`，TickFlow 快照；此前模型 bash-curl 腾讯行情站，CBRS 这类新股返回 `pv_none_match`）与 `check_available` 改 classmethod（`68d6346`）。
- **租户迭代上限 25→50**（08-26，`befbf80`，运营决策）：25 把 swarm 意图的 run 饿死——采集阶段就吃掉约 20 轮（attempt `c5810ef14c1e`）；改回 50，硬停止交给 wall-clock deadline。两次同日提交又被 revert：截断的工具调用参数视为断流重试（`9c9eba8`/`296c8fa`）、swarm 请求采集硬上限（`f22d0b0`/`9a88cad`）。
- **Anthropic 原生 `/v1/messages` 通道**（08-26，`8a42f25`、`a303fec`）：OpenAI-compat 转换路径吞掉 SSE ping，长思考期间流字节级静默数分钟，中间设备把「空闲」连接掐断成干净截断（attempt `fc2710…`/`5d3bea33…`）；`LANGCHAIN_PROVIDER=anthropic` 走原生通道端到端透传 ping，去掉两层协议转换；`ChatAnthropicCompat` 垫片兼容中继的序列化字段。
- **router 运维端点与自愈**（08-27）：模板切换启动清扫 `_sweep_stale_templates`（`f023941`，销毁旧模板实例并删旧模板，`VIBE_SWEEP_STALE_TEMPLATES` 可关）；半删除沙箱自愈（`7254de3`：resume 报成功但 VM 没起来、cubelet 回收任务后 CubeAPI 记录残留报 500 `NotFoundAtCubelet`，视同 404 弃建重建）；`/obs/prompt` 只读端点回传 attempt 完整输入（`7c99065`）；`/memory` 端点列出/物理删除租户长期记忆（`1194407`，laicai 记忆页）；资源表标注生产容量 4/4 + 2G swap（`875b373`）。

代码里原先以「2026-08-25 复盘 run #7/#10」「2026-08-26: iterations back to 50 (operator decision)」「attempt xxxx」形式留下的这些叙事，2026-09-21 起统一搬到本节，注释只留规则与一句原因。

## 2026-08-28：上下文工程三件套——microcompact 阈值化、状态栏外移、prompt caching 接通

**背景。** 对照《深入理解 AI Agent》§2.3/§2.7 做的引擎评审发现三处反模式叠加，导致 prompt cache 命中率趋零、CJK 会话压缩时机全错：①L1 microcompact 每轮**无条件**把倒数第 4 条之前的工具结果换成占位符——教科书级滑动窗口反模式，模型被迫反复重拉刚被丢掉的数据（dea1222743ef 事故正源于此），且每轮改写轨迹中部使缓存前缀必然失效；②系统提示里嵌着分钟级时间戳和 WorkspaceMemory State 块，逐轮字节不一致，缓存从第一个 diff 字节起全废；③native Anthropic 通道全程没设 `cache_control`，就算前缀稳定也没在用缓存。另有 token 估算 `len//4` 按英文假设，中文低估 2-3 倍。

**改法（批次 E，engine 侧）。**
- **E1 microcompact 阈值化**（`loop.py` `_microcompact`，swarm worker 复用同一实现）：只在估算 token 超过 `TOKEN_THRESHOLD × 0.5` 时才触发（worker 用自己的 `_MAX_TOKEN_ESTIMATE`）；触发后保留量从「固定最近 3 条」改为按 token 预算从新到旧累计（`× 0.25`，下限仍是最近 3 条）；新增免删名单 `MICROCOMPACT_PROTECTED_TOOLS`（backtest / factor_analysis / options_pricing / get_market_data / get_realtime_quotes / run_swarm）——grounding 数据与重算代价高的关键产出永不被 L1 清除。占位符文案与「已清除结果放行重拉」的重复守卫语义原样保留（那是事故修复）。
- **E2 动态块外移**（`context.py` + `loop.py`）：系统提示删掉 `## State` 与 `## Current Date & Time`，改由主循环每轮在轨迹末尾注入一条 `<agent_status>` user 消息（ISO 时间戳 + State 计数器），预算/收尾 nudge 并入同一条消息、条件成立期间逐轮重算；下一轮先移除上一条再追加（用后即弃）。系统提示自此整会话字节稳定。
- **E3 prompt caching**（`llm.py` `ChatAnthropicCompat._get_request_payload` 覆写）：native Anthropic 通道请求构建时注入三个 `cache_control: ephemeral` 断点——tools 尾、system 尾、最新一条非状态栏消息的末块（thinking 块不可缓存，自动跳过；断点注入失败静默降级为不缓存）。
- **E4 估算加权**（新模块 `src/core/token_estimate.py`，loop/worker 共用）：ASCII /4、CJK ×0.6/字、其余 /3；worker 的兜底计费估算与 auto_compact 尾部预算同步接线。
- **E5 工具文档去双份**（`context.py`）：`## Tools` 块收缩为「工具名 — description 首句（截 100 字符）」的索引；完整描述与参数 schema 本就每轮随 API `tools` 载荷传递，不再在提示词里重复数千 token。

**结果。** 全量回归改前基线 3285 passed / 5 failed / 2 skipped → 改后 3311 passed / 5 failed / 2 skipped：失败清单逐项相同（均为本地缺 langchain-anthropic 包等环境因素），零新增失败；passed 净增 26 = 新增的 microcompact 阈值/预算/免删、状态栏、缓存断点、CJK 加权、系统提示字节稳定用例。既有测试同步更新：goal-context / background-results 断言从「末条消息」改为「状态栏之前的最后一条真实 user 消息」，microcompact 旧断言以 `token_threshold=0` 复现固定 keep-3 行为。文档同步：SYSTEM-PROMPT.md §2/§3 改为状态栏与缓存断点的现状描述。

## 2026-08-28：批次 F——harness 校验/写工具硬超时/工具与记忆生命周期加固

**背景。** 同一轮引擎评审的第六批：harness 五要素缺 Verify（成功判据只有「metrics.csv 存在或有最终文本」）、写工具超时只警告不杀（挂死即吃光预算，架空 FINALIZE_RESERVE 部分答案机制）、swarm「失败先打捞别重跑」只有提示词一句话没有代码兜底，外加 bash 静默截断、MCP 远端结果不设防、registry 兜底异常泄内部路径、记忆生命周期三处缺口。

**改法（批次 F，engine 侧）。**
- **F1 收口轻校验**（新模块 `src/agent/verify.py` + `loop.py` 挂接）：run 判成功时跑两类零 LLM 结构化检查——①metrics.csv 数值可解析且在宽松合理区间（total_return/annual_return ∈ [-100%,+10000%]、sharpe ∈ [-20,20]、max_drawdown ∈ [-1,0]、win_rate ∈ [0,1]，抓引擎爆炸不评策略好坏）；②最终文本中标的邻近的价格类数字（落在参考价 1/3×~3× 带内才视为价格声明）与本 run get_market_data/get_realtime_quotes 抓到的参考价差超 20% 记警告。警告不翻转 success，只进 `attempt_stats.verify_warnings` + trace `verify_warnings` 条目 + 同名事件，供观测面板看。
- **F2 写工具硬超时**（`loop.py` `_invoke_tool` 重构 + `WRITE_TOOL_TIMEOUT_FACTOR=2`）：写工具与只读工具统一走 worker 线程 + 队列；1× 超时发 timeout_warning 继续等，2×（宽限段同样被预算钳制）仍未归即放弃等待——run 标 `degraded=true`（attempt_stats 可见）、给模型回 `write_tool_timeout` 结构化错误（明示副作用可能仍在后台完成、勿假设干净失败）、迟到结果照只读路径丢弃。
- **F3 swarm 失败打捞代码化**（`swarm_tool.py`）：同一 SwarmTool 实例内，preset 失败后 30 分钟（`_FAILURE_COOLDOWN_SECONDS`）内再调同 preset → 不执行，返回 `swarm_preset_cooldown` 结构化拒绝，附上次失败 run 已完成 worker 的产物摘要（completed tasks summary 各截 1200 字符、final_report 截 4000、最多 12 条）与「基于已有产物继续或换 preset」提示。系统提示原句保留作解释。
- **F4 bash/read_file 感知边界**（`bash_tool.py` / `read_file_tool.py`）：①bash 输出超 50k 改头 40k+尾 8k、中间插明确标记，完整输出落盘 run_dir（`bash_output_{stream}_{ts}.log`，标记内给文件名可 read_file 分页读）；②危险模式审计黑名单（rm -rf /、绝对路径重定向（容 /dev/null、/tmp）、curl|sh、sudo、dd of=/dev/、chmod 777 /）——只审计不拦截，命中记入结果 JSON `security_audit` 字段（随 tool_result 进 trace）+ emit_progress 事件；bash description 补出境走白名单代理说明；③read_file 增 `offset` 参数（1-based 行号，与 limit 配合分页），行截断提示「还有 N 行，可用 offset=M 继续」。
- **F5 MCP 结果设防**（`mcp.py`）：远端调用结果两级截断（超长字符串字段先各截 20k 带标记，仍超 50k 则降级为 envelope+序列化摘录，均标 `result_truncated`），统一过 `with_security_warnings`（text/error/data/content.*.text，与 reader 工具同款 scanner）；远端工具 description 注册时截 500 字符（第三方 description 属不可信输入）。
- **F6 registry 兜底脱敏**（`agent/tools.py`）：`ToolRegistry.execute` 兜底 except 的 `str(exc)` 统一过 `redact_internal_paths`（懒 import 避免包循环），个别工具自带的脱敏保持不变。
- **F7 记忆生命周期**（`persistent.py` / `remember_tool.py` / `context.py`）：①索引满 200 行时 remember save 返回值携带警告 + emit `memory_index_full` 事件（`_update_index` 返回是否入索引、`last_add_indexed`/`index_full` 暴露）；②`<recalled-memories>` 块首加非指令声明；③frontmatter 增 `created`（ISO）与可选 `source`（remember 新参数），老条目无字段兼容；④检索改加权计分——中文相邻 2-gram 满权、孤立单字降权 0.3，乘 `1+0.1×新鲜度`（mtime 30 天线性衰减）recency 权重，零依赖；⑤`consolidate()` 去重（同 title 跨 type 并列条目按 mtime 保留最新、旧 body 以合并标记折入、合并失败不删旧文件）+ 新工具 `consolidate_memory`（共享 PersistentMemory 注入）；⑥remember description 补齐何时存/不存、同名同 type 覆盖语义、索引上限。
- **F8 skill 横向链接**（`skill_writer_tool.py`）：save_skill description 要求新 skill 正文含 Related 段、链接 ≥2 个相关已有 skill。

**结果。** 全量回归改前基线 3311 passed / 5 failed / 2 skipped → 改后 3366 passed / 5 failed / 2 skipped，失败清单逐项相同（均为本地环境因素：缺 langchain-anthropic 等），零新增失败；净增 55 = 新增用例（verify 13、写工具硬超时 1、swarm 冷却 4、bash 截断/审计 10、read_file offset 5、MCP 设防 9、记忆生命周期 12、registry 脱敏 1）。既有测试同步更新：写工具「永不杀」断言改为「宽限内完成只警告 + 超 2× 放弃并标 degraded」两条。文档同步：SYSTEM-PROMPT.md §2 Guidelines/§3 记忆通道、SKILLS.md save_skill 行。注意 agent/SKILL.md 的 MCP 插件工具表未加 `consolidate_memory`——该表只列 mcp_server.py 暴露的工具，`remember`/`consolidate_memory` 均为进程内 agent 工具不在其列。

## 2026-08-29：批次 V1——修 F2 写工具硬超时吃掉 swarm 两小时预算（P0）

**背景（承接上面 08-28 批次 F 的 F2，以及 08-24 条目末尾那句「写工具只警告不杀」）。** F2 给写工具装上 1×警告 / 2×放弃的看门狗，但那个 1× 的 base 被写死成租户档 `VIBE_TRADING_TOOL_TIMEOUT_SECONDS`（生产 300s），没有 per-tool 覆写。于是 `run_swarm`——一个「正常就要跑几十分钟」的写工具——被按「300s 的写工具挂死了」处理：**每次 swarm 在第 600 秒被放弃等待**。08-24 那条记录的前提（写工具不杀）自此失效，同一个跑 21 分钟的 `investment_committee` 在 F2 之后会在第 10 分钟被丢掉。

连带损害比一级失效更贵：①返回体是 `write_tool_timeout`，**不含 run_id**，`wait_budget_exhausted` 打捞路径 100% 不执行；②`SWARM-PRESETS.md` 承诺的两小时档（chat swarm 意图 + 作战室四个专业报告）完全不可达；③attempt 标 `degraded`，把运营 Tab 这个信号污染成噪声；④F3 的 preset 失败冷却靠工具**返回**失败才装填，工具从没返回过，冷却从未生效，加上 `repeatable=True`，模型大概率立刻重开一轮；⑤被放弃的 run 不 cancel，daemon 线程带 4 个 worker 继续跑到两小时，两小时档最多叠出 ~11 个并发孤儿 run，**这些 token 既不计费也不进 `attempt_stats`**（`_emit_swarm_usage` / `record_swarm` 都只在终态调用）。

**改法（批次 V1）。**
- **V1-A 工具级 `timeout_seconds` 声明**（`agent/tools.py` / `loop.py` / `swarm_tool.py` / `alpha_bench_tool.py` / `mcp.py`）：`BaseTool` 加可选声明位，`loop._tool_timeout(name)` 取 `max(全局, 声明值)` 作为 1×/2× 窗口的 base——**声明只能放宽不能收紧**（运维调低租户档不误伤 swarm，工具作者也无法悄悄缩短自己的窗口把结果丢掉）。`cap_timeout` 语义与 `budget.py` 零改动，声明多少仍被 attempt 剩余预算钳死，F2「一个挂死写工具不能吃光 attempt」的原始保证逐条保留。`run_swarm` 用 property 声明 `SWARM_TIMEOUT + 120s`（读取时求值，`SWARM_TIMEOUT` 与测试 patch 都生效）；`alpha_bench` 声明 `VIBE_ALPHA_BENCH_BUDGET_S + 120s` 并**同时**加自身预算（耗尽即停止起新 alpha、照常出报告、回 `budget_exhausted` + `n_not_run`——只声明不自限等于把无界问题从循环挪进工具）；MCP 远端工具声明 `tool_timeout + max(tool_timeout,30) + 30`，配套给 `config/schema.py` 的 `tool_timeout` 补上界 `le=1800`（原先只有 `ge=0.1`）。
- **V1-A 配套常量**：`loop.py` 的 `reserve_s=45.0` 字面量提为 `_TOOL_CAP_RESERVE_S = max(45.0, FINALIZE_RESERVE_S)`——原先 45s 比 `FINALIZE_RESERVE_S` 默认 60s **少 15s**，放弃工具后可能剩不够做强制收尾，「部分答案胜过超时」只是名义上的；`floor_s` 两处字面量与 `swarm_tool` 的 `reserve_s=90/floor_s=60` 一并提为模块常量，缩放回归才 patch 得到，也让嵌套不变式在代码里可读。
- **V1-B preset 名单真源改为 YAML 目录**（`_discover_preset_names`）。原先 `_PRESET_NAMES` 派生自关键词表，导致 4 个已发布 YAML（`crypto_trading_desk` / `earnings_research_desk` / `global_equities_desk` / `macro_rates_fx_desk`）被判 Unknown preset、系统提示自称的「29 teams」实为 25 个可点名；作战室那句散文「preset 用 macro_rates_fx_desk」还会因为「macro」命中而静默跑成 `macro_strategy_forum`——**四个专业报告里一直有一个跑错团队**。四个 preset 同时补齐关键词行与 `_build_variables` 条目（缺后者会落到 `{market, goal}` 默认分支、自己的模板变量不被替换）。
- **V1-C 变量抽取带上用户原文**。`commodity_research_team` 的 `commodity` 恒为 "gold"、`crypto_research_lab` 的 `target` 恒为 "BTC, ETH, SOL"、derivatives 的 `view` 恒 neutral、factor 的 `factor_type` 恒 value、event 的 `event_type` 恒 "all types"、fund 的 `fund_type` 恒 equity——全部改为先读 prompt（复用 `_extract_market` 同款关键词表），抽不到才回落默认值；前两个 preset 的 YAML 另加 `{goal}` 段落（用户原话），回落时 worker 看到的是真实诉求而非一个自信的错主题。
- **V1-D 关键词路由 tie-break**：平分时优先精确短语命中数更多者（多词英文短语或 3 字以上中文词；单个泛词不计），仍平分按表内顺序；路由置信度作为 `preset_score` 随结果返回（99=点名 / 正数=关键词 / 0=兜底），此前点名与兜底在结果里长得一模一样。
- **V1-E 工具描述与报错四修**：`save_skill` 不再指向不存在的 `list_skills`（改指系统提示的 Skills 摘要 + `load_skill` 验证）；`load_skill` 的幻影示例 `'momentum'` 换成真实存在的 `technical-basic`；`web_search` 描述删掉 ddgs 9.x 已移除的 Google/Bing 名单（工具本来也没有选引擎的参数），失败报错删掉「改 `VIBE_TRADING_SEARCH_BACKENDS`」这条模型根本执行不了的建议、只留 retry / read_url；`bash` 的 120s 硬超时改为可配（`VIBE_BASH_TIMEOUT_S`）并被 attempt 预算钳制，description 写明它与 `background_run` 的分工，超时报错直接给出 `background_run` + `check_background` 的替代路径；`ToolRegistry` 的 not-found 报错改为列出可用工具名。
- **V1-F `run_swarm` 加 `run_id` 入参**。`wait_budget_exhausted` 一直告诉模型「re-invoke with the returned run_id」，但 `parameters` 里**根本没有这个字段**，唯一可执行的选项是重开一轮。现在传 `run_id` 即续等同一个后台 run（不起新 run、不产生额外 worker token），结果标 `resumed`，`attempt_stats.swarm_runs` 同步标记以免一个 run 被读成两个。

**结果。** 全量回归改前基线 3366 passed / 5 failed / 2 skipped → 改后 3395 passed / 5 failed / 2 skipped，失败清单逐项相同（均为本地环境因素：缺 langchain-anthropic 等），零新增失败；净增 29 = 新增用例（超时嵌套 6、per-tool 声明 6、其它工具声明与预算 6、preset 路由与变量 6、preset 打包覆盖 2、bash 超时 3）。既有测试同步更新：`test_web_search_tool` 的失败报错断言从「必须提到 `VIBE_TRADING_SEARCH_BACKENDS`」改为「必须**不**提它、也不提已删除的引擎名」。F2 原有三条写工具用例逐字保留——它们用的是未声明 `timeout_seconds` 的工具，正是「1×/2× 语义对普通写工具不回退」的护栏。

**核心护栏**：新增 `agent/tests/test_swarm_timeout_nesting.py`，把生产常量按 1:300 等比例缩放（TOOL_TIMEOUT 300→1、SWARM_TIMEOUT 7200→24、reserve 60/90→0.2/0.3、margin 120→0.4）复刻本次故障，断言拿到的是带 run_id 的 `wait_budget_exhausted` 而非 `write_tool_timeout`；deadline 一项刻意不缩放（生产两侧只差 30s/7000s ≈ 0.4%，缩放后是 0.1s 的竞态、CI 上必然 flaky），预算钳制那一半改由同文件的纯函数用例 `test_tool_timeout_nesting_invariant` 按真实生产数值断言（`remaining=150` 时两个 floor 相遇，顺带把 floor 取值也钉住）。

**文档同步。** OBSERVABILITY.md §4 的钳制表补齐 per-tool base、写工具 1×/2× 窗口、三个声明工具的取值与嵌套不变式，§9 env 表删掉「单**只读**工具硬超时」这个 F2 之后就已经错了的措辞并新增三个变量；SWARM-PRESETS.md 触发策略补 preset 真源、tie-break、变量抽取、两层等待的嵌套关系与 F3 冷却。**部署**：批次 D/E/F 随模板 v35（`tpl-34c2c3971a654f7796bc4385`）、本批次 V1 与后续 V2/V3 随模板 v37（`tpl-b7fb286c6b0247cd9b0a9b6a`）于 2026-08-29 上生产；两次都走既有 runbook（重建镜像 → 发模板 → 改 `VIBE_CUBE_TEMPLATE_ID` → restart cube-router），存量租户下次调用自动换模板，数据在宿主 bind-mount 不动。

## 2026-08-29：批次 V2——上下文层交界、跨 attempt 交接、harness 韧性、记忆卫生

**这一批修的全是「层与层的交界处」。** 三轮评审（上下文工程 / harness / 记忆管理）的共同结论是：每一层单独看都做对了，出问题的是层之间没有共用同一套预算与豁免规则。

**上下文层。**

- **V2-1 三层压缩共享一份保护规则**（新 `agent/context_policy.py`）。此前 L1 有一份私有的 `MICROCOMPACT_PROTECTED_TOOLS`、L2 只认一个 `startswith("[cleared")`、L3 什么都不认，于是 **L2 会把 L1 明确免删的 grounding 结果拦腰折断**（`get_market_data`/`backtest`/`run_swarm` 的大 JSON 中段被剪掉，「所有被引用数字可溯源」的立意在下一层被推翻），也会折断 L3 刚花一次 LLM 调用产出的交接摘要。关键设计选择是**分级而非布尔豁免**：一律豁免会让首条 user 消息（原始请求 + goal + `<recalled-memories>`，长会话里最大的一条）永远压不下去，所以改成 `DEFAULT(2400/900/500)` / `FIRST_USER(9600/3000/1200)` / `SKIP` / `PROTECTED_HARD_CAP(24000/6000/3000)` 四档。最后一档是逃生阀——没有它，一个畸形巨结果能把上下文顶爆而三层都不敢动它。
- **V2-2 工具结果落盘 + 显式截断标记**（新 `agent/tool_result_store.py`）。原先是 `result[:10_000]`，**不追加任何标记**：模型拿到一份 40k JSON 的前 10k，会把它当完整文档解析，并把被砍掉的行报成「数据源没有」。现在超限结果写进 `run_dir/tool-results/{iter:03d}-{tool}-{callid8}.{json,txt}`，轨迹里放 `<tool-result-truncated total_chars=… shown=…>` 包裹的 head+tail 预览 + 磁盘路径 + 「这是预览，不要整体解析，也不要据此断言数据缺失」的指引。文件名是 (iter, tool, call_id) 的确定性函数，重放不产生新文件、预览文案字节稳定（书 §2.3.4）。盘满时 `OSError` 降级为「带标记的纯截断」并计 `attempt_stats.offload_failures`。**原始 result 一字未动**——错误判定、F1 grounding 校验器、trace 三条路径继续吃全量，只有进 messages 的那份变预览。
- **V2-3 `run_swarm` 单独收口**。它在免删名单里（重跑要几十分钟），但**入场那一刻就已经被砍**：返回体是 `final_report` + 每个 task 的 report.md **全文**，多 worker preset 轻易几十 KB，10k 的刀正好落在 JSON 中部 → 模型收到的是非法 JSON 且后面的 task 凭空消失。改为 task summary 800 字符预览 + `report_path` 指针，`final_report` 拿**实测剩余额度**。这里与设计稿有偏差：稿子同时写了「final_report 封顶 20k」和「返回天生 <10k」，两者不可能同时成立——20k 单项就已经超了限。改成先序列化其余字段量出开销、再把剩下的给 final_report，并让 task 预览随团队规模收缩（`TASKS_BLOCK_TARGET_CHARS`，下限 250 字符）。实测 1/3/6/8/12 任务的 preset 全部落在 9748 字符，最宽的已发布 preset 是 12 任务的 `technical_analysis_panel`。
- **V2-4 跨 attempt 交接**（新 `session/handoff.py`）。`_previous_summary` 是实例态、每次 `run()` 归零，所以上一 attempt 压缩掉的决策/约束在下一 attempt **完全不可见**，laicai 同线程追问只剩一个 12k 字符的滑窗。现在 L3 摘要在 `_auto_compact` 产出的**当下**就原子写进 `sessions/<sid>/handoff.json`（不是等 run 结束——崩溃/超时的 attempt 恰恰是最需要这份摘要的），下次 `run()` 从 `handoff.load()` 复原，L5 因此走增量更新而非从零重建，零额外 LLM 调用。sidecar 而非 `messages.jsonl` 里的一条 Message：后者是 append-only 且用户可见（前端会看到一条用户没说过的话），可覆写的派生状态不该进去。
- **V2-5 会话历史两层化 + token 口径统一**。`MAX_HISTORY_CHARS=12000` 的注释写「roughly 3000 tokens」，但按本仓自己的 CJK 估算器，中文 12k 字符 ≈ 7.2k token——同一个仓库两套口径。换成 `MAX_HISTORY_TOKENS`，但**取 6000 而不是注释里的 3000**：直接按注释改会把中文会话的历史砍掉一半以上，这一步只统一口径、不同时缩预算。被裁的旧轮次留一行「N 轮已省略」的显式占位（P2-9）。
- **V2-6 microcompact 滞回**。单条触发线导致越线之后**每轮**重算 keep 集合、新结果不断把老结果挤出预算，于是每轮都有一两条老结果在轨迹深处变占位符 → provider cache 从该 diff 点起逐轮重建。批次 E 消除了「无条件逐轮清」，阈值以上区间实际又回到了逐轮改写。改为 armed 滞回：越 0.5 线触发并**深切一刀**（keep 0.25→0.15），保持 armed 直到回落 0.35 解除；中间隔开多轮全热缓存（书 §2.7.3）。状态由调用方持有（`state` 参数），主循环与 swarm worker 各自一份，不引入全局。
- **V2-7 `_auto_compact` 输入按 token 从旧端裁**。`json.dumps(head)[:80000]` 是从**尾部**砍，砍掉的是 head 里最新、信息密度最高的轮次；40k token ≈ 160k ASCII 字符 >> 80k，英文重会话几乎必然触发。改为按 `TOKEN_THRESHOLD*0.5` 的 token 预算从最新往回装、丢旧端，并在输入前缀写明丢了几条。

**harness 韧性。**

- **V2-8 per-(tool,args) 连续失败熔断**。重复调用守卫只登记**成功**调用（`if success:`），所以同一工具同一参数的失败可以无限重复到迭代上限——一个挂了的上游能吃掉 40+ 迭代的 LLM 费用。复用 `_tool_call_key` 基建，连续 3 次（`VIBE_TOOL_CIRCUIT_FAILURE_LIMIT`）后返回结构化 `circuit_open` 拒绝，文案给出可执行的三条出路（换参数/换工具/带缺口作答）而不只是说「不行」。成功一次即清零。
- **V2-9 swarm worker 复用主循环工具看门狗**。`_invoke_tool` 的机体提取为模块级 `invoke_tool_guarded(registry, name, args, *, readonly, timeout, emit, on_degraded)`，主循环退化为薄壳。worker 此前是 `registry.execute` 内联，**没有任何超时**：挂死的工具在迭代内无限阻塞（worker 只在迭代边界查自己的 deadline），只能等 runtime 的层级 deadline 在 `layer_budget+60s` 后把整层判掉。现在 worker 的每个工具调用被 `min(工具声明超时, worker 剩余时间)` 钳制，再经 `cap_timeout` 被 attempt 预算钳一次，嵌套仍是「内层 ≤ 外层」。心跳同步接上（worker 把守卫的 `tool_heartbeat` 翻译成 `task_heartbeat`，stale-run reaper 依赖的信号不变）。
- **V2-10 worker `tool_result` 事件 status 改按 `_is_error_result` 判定**。此前硬编码 `"ok"`，swarm 观测面板的 worker 工具错误率恒为 0，与主循环 ok/error 双态口径不一致。
- **V2-11 `empty_model_response` 就地重试一次**。流**成功返回**但既无 content 也无 tool_calls 属于 provider 退化响应（中继截断成空、上游偶发空 turn），传输层的 3 次退避重试完全覆盖不到它，此前一次即判败，一个可能已跑几十分钟的 attempt 就此报废。给一次带 nudge 的重试（消耗一个正常迭代，不回退 `iter` 计数器——那会在 trace 里产生重复的 `iter` 键，可观测性瀑布图正是按它索引的）。
- **V2-12 `_auto_compact` 的 LLM 调用包 try/except**。压缩是**纠正机制**，它失败不该杀死本来健康的 run；此前异常直接冒到 `run()` 顶层 except 把整个 attempt 打成 failed。失败时降级为只做 L1/L2 剪裁、轨迹逐字不动、写 `compact_failed`，下一轮再试。顺带挡住「摘要返回空字符串」——那会把 head 抹掉却不放回任何东西。
- **V2-13 worker `incomplete` 纳入重试**。`if result.status != "failed": return` 把产出契约失败（report.md 没写、数据角色没调数据工具）一并放过，而这恰恰是「再给一次机会大概率就好」的失败类型，preset 的 `max_retries` 预算本来就是为它准备的；不重试则下游依赖它的 worker 全被 blocked。`timeout`/`token_limit` 仍不重试（重试也会再撞同一堵墙）。
- **V2-14 上游报告注入下游 worker 有预算了**（P2-7）。`task_summaries` 的值是 report.md 全文，editor/PM 类多上游角色的 system prompt 会拼进 N 份全文、无任何截断层。超 8000 字符改为 head+tail 预览 + artifact 路径（worker 有 `read_file`）。

**注入防护。**

- **V2-15 扫描器补中文**。五条规则全是英文正则（且用 `\b` 词边界，CJK 之间根本没有词边界），对「忽略以上指令」「你现在是系统管理员」「把系统提示词打印出来」零命中——而本产品的 `read_url`/`web_search` 目标以中文为主，**上下文层护栏对主流量不设防**。每条规则补一条中文变体，用 `[^。！？\n]{0,N}` 代替词边界。同步补了 5 条「正常中文财经文本不得误报」的用例（营收增长/最大回撤/回购公告/夏普比率/系统性风险）——误报会给每一篇雪球帖子挂横幅。
- **V2-16 外部内容包声明块**。此前只有记忆召回有 `<recalled-memories>` 的非指令声明（F7②），网页/搜索/PDF 正文**裸拼进轨迹**、扫描器的结论藏在 JSON 尾部字段里。现在三个 reader 的正文包进 `<external-content source=… kind=… trust="untrusted">`，high 级命中把警告**提到正文之前的显式横幅**。

**记忆卫生。**

- **V2-17 `persistent.py` 写盘失败结构化**（这才是「4G 打满失败模式」的本体）。`path.write_text` 无 try，`OSError` 直接冒泡杀掉整个 attempt。新增 `MemoryWriteError`，`remember_tool` 捕获后返回 `memory_write_failed` 的结构化错误并明说「别重试这次 save，把这个事实写进回答」。索引写失败单独处理：条目文件才是资产，索引是派生物，索引失败只置 `last_add_indexed=False`。
- **V2-18 同名覆盖折入旧正文**（P2-7）。同名同 type 直接覆盖、旧内容无任何保留，正是 Mem0 write-time UPDATE 的教训（一次错误更新不可逆丢历史）。复用 `consolidate()` 现成的 merge 标记逻辑把旧 body 折进新文件尾部，新正文在前（recall 预览从头读）。
- **V2-19 记忆条目补 Related 链接要求**（P2-8）：把 F8 给 skills 的那句话拷进 `remember` description。
- **V2-20 索引 ≥180 行时收尾自动 consolidate**（P2-11）。此前只有 F7① 的告警，指望模型自己想起来调 `consolidate_memory`。阈值取 180 而非 200：过 200 之后新条目根本不进会话启动快照，整理必须发生在**上限之前**。跑在 run 收尾而非每次写入——consolidate 会重写条目文件，run 中途做会搅动系统提示已经冻结的快照。
- **V2-21 删除入库的 `agent/logs/engine.jsonl`**（P2-12，164KB，首行含内部 LLM 网关地址 `sub2api.laicai8.co`）并把 `logs/` 加进 `agent/.gitignore`。

**结果。** 全量回归 3395 passed / 5 failed / 2 skipped（V1 基线）→ **3525 passed / 5 failed / 2 skipped**，失败清单逐项相同（3 条 anthropic 凭据缺失 + 1 条 dotenv latch 的套件顺序依赖 + 1 条 web_search backend 断言，均为本地环境因素），**零新增失败**，净增 130。新增 7 个测试文件：`test_context_policy.py`（14，分级策略与 L2 实际行为）、`test_tool_result_store.py`（13，含字节稳定性与盘满降级）、`test_session_handoff.py`（17）、`test_v2_harness_resilience.py`（13）、`test_v2_swarm_result_budget.py`（15）、`test_v2_memory_hygiene.py`（15）、`test_v2_injection_defense.py`（32）、`test_v2_loop_integration.py`（11）。

既有测试的语义更新（都是行为确实变了，不是迁就实现）：`test_loop_helpers` 的两条折叠用例从 `messages[1]` 改看 `messages[2]`（下标 1 现在是走 FIRST_USER 分档的首条 user）；两条 `empty_model_response` 用例的断言从 "iteration 1" 改 "iteration 2"（多了一次重试）；`test_swarm_status_hydration` 的心跳源码检查从「worker 必须用 HeartbeatTimer 包住 registry.execute」改为「worker 必须走 `invoke_tool_guarded` 且转发它的心跳 + 守卫自身必须包住阻塞等待」；三处 FakeStore/\_Store 测试替身补 `run_dir()`；doc_reader / web_search 的三条相等断言改为包含断言（正文现在带 `<external-content>` 包裹）。

**部署**：随模板 v37（`tpl-b7fb286c6b0247cd9b0a9b6a`）于 2026-08-29 上生产（与 V1、V3 同一次模板切换）。

## 2026-08-29：批次 V3——数据生命周期收口 + swarm 意图结构化（router 侧）

**问题一：注销不清引擎数据。** laicai 的 `deleteUser` 只让本库的域表随 cascade 清空；引擎宿主机租户目录下的长期记忆、`sessions/` 全部对话正文、`trace.jsonl`（含**未裁剪的完整 prompt**）、runs 与用户上传的交割单**永久残留**。router 的 `POST /forget` 早就完整实现且幂等，但**laicai 侧零引用**——端点存在不等于链路存在。修法在 laicai 侧（无外键的 `engine_forget_jobs` 登记表 + `deleteUser` 前后两个钩子 + 夜间重试 + 运营看板告警，见该仓记录）；router 侧只补文档，把「谁调用、什么时候调用、失败怎么办」写进 PRODUCT_DESIGN §3.2 —— 一个没有记录调用方的清理端点，下一次评审还会把它读成死代码。

**问题二：7200 散落四处。** 「swarm 两小时预算」这一个事实此前被写在四个地方：laicai `chat-tools.ts` 的 `SWARM_TIMEOUT_S`、laicai `warlab-engine.ts` 的 `SWARM_GEN_TIMEOUT_S`、router 下发的 `SWARM_TIMEOUT` env、引擎 `swarm_tool.py` 的默认值。08-24 事故记录里那句「三处同调」就是这么来的。更糟的是**意图本身**的传递方式：模型把「使用多智能体团队(swarm)分析」写成中文散文塞进 `query`，laicai 再用正则把它嗅探回结构化来决定 15min/2h 预算，引擎侧还有第三份关键词表——`结构化 →（压成散文）→ 正则嗅探回结构化 →（再压成散文）→ 正则嗅探回结构化`，每一次往返都掉信息，模型换个措辞，本该跑两小时的 swarm 就落在 15 分钟预算里。

改法：`/ask` 接收结构化的 `intent`（`standard` | `deep_team`）与 `swarmPreset`，预算由 router 的 `BUDGET_BY_INTENT` **单点推导**，下发给引擎的 `SWARM_TIMEOUT` env 也从同一常量派生。三条兼容保证：①`body.timeoutS` 显式给出时仍最优先，因此只回滚 laicai 就能立刻回到旧预算行为；②两个新字段都是 `Optional`，老 laicai 不发即走原路径；③`intent`/`swarm_preset` 与 `deadline_s` 并列下发，不认识它们的引擎版本忽略即可——router 因此可以先于引擎发布。`swarmPreset` 只做形状校验、**不比对 router 侧的名单副本**：preset 的唯一真源是引擎的 `agent/src/swarm/presets/*.yaml`，一份过期的副本会去误拒引擎实际支持的 preset（V1-B 修的正是同一类病）。ask 日志新增 `intent` 与 `budget_source`，「我的 swarm 为什么只拿到 15 分钟」从此有据可查。

**问题三：4G 打满没有失败模式。** 租户可写层封顶 4G，但打满之后会怎样此前无定义——引擎的记忆写入与 trace 写入都是裸 `write_text`，磁盘满表现为 attempt 中途抛异常，且没有任何东西指向「这个租户没空间了」。本批次**只做只读的那一半**：`/healthz` 增 `disk` 段与每租户 `disk_bytes`/`over_watermark`，新增 `GET /tenants/usage` 列 Top N，超 80% 水位打 warn；`du` 结果缓存 5 分钟（healthz 会被轮询，逐次遍历数 GB 目录不可接受），且对缺失/竞态目录一律返回 0 而不抛——健康检查不能被一块满盘拖下水。

**真正的清扫（删 sessions/runs/uploads）刻意不做**，代码里留 `TODO(retention)` 写明落地前置条件：先积累两周真实用量再定保留窗（凭证据而不是凭猜测定阈值）、上线必须先 `--dry-run` 人工核对无活跃会话、引擎侧 FTS 索引要同步清死行否则搜索返回死链。`memory/` 永不参与清扫——那是用户资产，只有用户手删或 `/forget` 能动。删用户数据是整个计划里失误代价最高的一步，把它放在最后是刻意的。

**验收**：`python -m py_compile ops/cube-router/router.py` 通过；新增 `ops/cube-router/test_router_budget.py` 10 条纯逻辑用例全过（预算推导四种组合、`timeoutS` 优先级、引擎 env 与 `BUDGET_BY_INTENT` 一致性、磁盘统计的求和/缺目录/缓存/水位/缓存回收）。**部署**：router 改动随 2026-08-29 的 cube-router 重启上生产，引擎侧同批次改动在模板 v37（`tpl-b7fb286c6b0247cd9b0a9b6a`）里；laicai 侧 `intent`/`swarmPreset` 的发送与 `engine_forget_jobs` 链路同日 `deploy:vps` 上线。

## 2026-09-04：第三轮评审整改——symlink 越界、shell 凭据泄露、数据工具截断（3 P0 + 一批 P1）

**背景。** 第三轮双仓评审在本仓判定三个 P0，全部是「上一轮把机制做对了、边界没守住」：

1. **symlink 读宿主机。** router 以 root 跑在宿主上，`/memory`、`/memory/delete`、`/obs/*`、`_dir_bytes` 都直接拼 `DATA_ROOT/<tk>/...` 读写；而 guest 里的引擎以 uid 1000 在同一个 bind-mount 目录里可以任意建链接。`memory/x.md -> /etc/shadow` 经 `/memory` 可读，`MEMORY.md -> /root/.ssh/authorized_keys` 经 `/memory/delete` 的索引改写可写，`big -> /` 让 `rglob` 以 root 遍历整个宿主文件系统。
2. **bash 泄 key。** `bash` / `background_run` 继承引擎进程 env，而多租户下那份 env 装着全租户共享的内置 LLM 凭据、`TUSHARE_TOKEN`/`JINA_API_KEY`/`IFIND_MCP_TOKEN` 与引擎自己的 `API_AUTH_KEY`——模型跑一句 `env` 就把它们写进工具结果、trace 和 LLM 上下文。
3. **数据工具 5.6× 截断。** `get_market_data` 默认 250 行、`indent=2` 的 record 列表，单标的一年日线实测 **63,158 字符**，是 10k 轨迹预算的 5.6 倍以上——V2 加的落盘+预览信封让模型每次只看到头尾 1k，中间被砍掉的 bar 被读成「数据源没有」；`load_skill` 同样被 10k 一刀切，27 个内置 skill 超限（tushare 约 100k），信封还把 JSON 单行落盘，`read_file` 按行翻页永远翻不到第二行。

**router 侧改动（`ops/cube-router/router.py`）。**
- `_safe_tenant_path(base, p)` / `_tenant_root` / `_tenant_file`：路径自身是 symlink、或 `resolve()` 后不在租户目录内（含父目录是外指 symlink）一律按不存在处理。`/memory` 列表跳过 symlink 条目与 symlink 形态的 `memory/`；`/memory/delete` 对 symlink 目标 404、索引改写走 tmp+replace；五个 `/obs/*` 全部改经 `_tenant_file`；`_dir_bytes` 改 `os.walk(followlinks=False)` + `lstat` 只计普通文件，symlink 租户根计 0，`DATA_ROOT` 遍历跳过 symlink 目录。
- `POST /sessions/delete {uid, session_id}`：laicai「删除对话」此前只删自己库里的线程，引擎侧 `sessions/<sid>/`（含完整 prompt 的 trace、transcript 转储、handoff）永久残留，唯一清理口是注销时的整租户 `/forget`。沙箱在跑走引擎 `DELETE /sessions/<sid>`（`mode=engine`，引擎侧同时 cancel 在跑 loop + 清 `sessions.db` FTS 行），否则直接删宿主目录（`mode=offline`），symlink 拒删，幂等（`deleted=false`）。
- `/forget` 失败可见：`sbx_delete` 返回 bool，沙箱删除失败或 `rmtree`（改到 `asyncio.to_thread`，`onerror` 收集明细）不完整 → 500 `{ok:false,error}`，laicai 的 `engine-forget.ts` 按 `res.ok` 让 job 留队等 23:30 重试；沙箱未删成功时 state 行保留。
- `/healthz` 与 `/tenants/usage` 的 `over_watermark` 从计数改为 tk8 列表（看得见「谁」超线才能有人处理），新增 `disk_used_pct`（整盘水位——租户配额在盘满之后无意义）。
- 引擎 422 → 400：`SendMessageRequest.content` 上限从 5000 提到 20000（laicai 注入持仓上下文后中等规模的账本就把旧上限撞成裸 422），router 把长度类 422 转成「问题过长，请精简后重试（引擎单次输入上限 20000 字符，含注入的持仓上下文）」，其余 422 截 200 字符转 400。
- 失败 attempt 不再当答案：引擎在回复 `metadata` 写 `ok`/`error`，router 的 `_classify_answer_message` 遇 `ok=false`（或旧引擎的 `status="failed"`）抛 `_EngineFailed` → outcome `engine_failed`、走 error 帧 502、并触发未答即 cancel；此前「Execution failed: …」的 assistant 文本被原样当研究结论回给用户。

**引擎侧改动（`agent/`）。**
- **shell 最小 env**（新 `src/tools/subprocess_env.py`）：白名单（`PATH`/`HOME`/locale/`TZ`/`TMPDIR`/venv 变量 + `VIBE_*`）+ 段级拒绝（`_KEY`/`_TOKEN`/`_SECRET`/`_PASSWORD` 作后缀或中间段，`VIBE_EGRESS_SSH_KEY_B64` 会被抓、`VIBE_TRADING_KEYWORDS` 不会）+ 前缀拒绝（`OPENAI_`/`ANTHROPIC_`/`LANGCHAIN_`）。**按值脱敏**（`redaction.redact_secret_values`）：引擎 env 里凭据形名字、≥12 字符的值在任何工具结果进入轨迹前替换为 `[redacted:<KEY>]`，最长值优先替换防止短 secret 把长 secret 切成仍可辨认的残片；bash/background 的 stdout/stderr 在截断与落盘之前先脱敏，主循环 `_finalize_tool_result` 兜底覆盖其余工具（读到 `.env` 的 `read_file`、回显 header 的 MCP 报错）。
- **取消穿透**（新 `src/core/cancel.py`）：cancel 事件绑进 contextvar，`invoke_tool_guarded` 按 1s 切片等 worker 队列、取消即返回 `error_code=cancelled` 的结构化结果；`run_swarm` 的轮询用 `sleep_unless_cancelled`，取消返回 `cancelled_wait`（带 run_id，run 不动）。一个实测修正：取消恰好落在最后一个切片、与 deadline 同时到期时，旧写法会把它报成工具超时——改为 `queue.Empty` 之后先查 cancel 再判 deadline，**取消优先**（`test_cancel_tool_wait.py` 钉住）。`SessionService` 同会话新 attempt 先 cancel 旧 loop 再覆盖注册、`finally` 只弹自己的条目（旧写法静默覆盖，旧 loop 变成 cancel 够不着、token 照烧的孤儿）。
- `_auto_compact` 之后清理 `_called_ok`：重复调用守卫按工具结果消息**对象**键控，被压进摘要的结果已不在轨迹里，却仍让模型被告知「用上面的结果」——现在只保留消息仍在尾部的条目。
- **原子写**（新 `src/core/atomic_write.py`，从 `handoff.py` 抽出）：`persistent.py` 的条目文件、`MEMORY.md`、`_rebuild_index`、`consolidate` 合并写全部改走 tmp+`os.replace`；`swarm/store.py` 的 `_atomic_write` 临时名带 pid、失败清理。**`MEMORY.md` 损坏隔离**：非法 UTF-8 的索引此前在 `PersistentMemory()` 构造时抛 `UnicodeDecodeError`，该租户每次 attempt 都起不来直到有人 SSH 进去；现在搬到 `MEMORY.md.corrupt-<ts>`、以空快照继续，条目文件不动可重建；单条目解码失败只跳过。
- `attempt_stats` 补发 `compact_failures` / `offload_failures`（此前计了数但没发）；`delete_session` 清 `sessions.db` 的消息行与会话行（FTS 影子表随触发器同步）。
- **`get_market_data` 载荷重做**（`src/market_data.py` / `tools/market_data_tool.py` / `agent/tool_result_store.py`）：默认 `max_rows` 250→120；每标的从 record 列表改为紧凑表 `{summary, columns, rows}`（列名只出现一次、日期裸 `YYYY-MM-DD`、4 位小数、整数值去 `.0`、无缩进）。同一标的一年日线：旧 250 行 `indent=2` = **63,158 字符**，新默认 120 行 = **8,944 字符**，落在 10k 之内不再落盘。超 10k 走结构化预览：每标的保 `summary` + 首尾 20 根 bar（多标的收缩到 5，再超退回通用信封），`rows_omitted` 计中段，预览是合法 JSON；落盘改为每根 bar 一行，`read_file` 按行翻页即按 bar 翻页。`source` 改 enum（`auto` + 注册表 `VALID_SOURCES` 动态取），`interval` 改 enum（`1m…1M`，按各 loader 支持面注明）。grounding 校验器 `extract_reference_prices` 改经共享的 `table_rows()` 读表，旧形状仍兼容；`mcp_server.py` 的 `get_market_data` docstring 同步（source 清单、默认 120）。
- **`load_skill` 返回 Markdown**：去掉 `{"status","content"}` JSON 包裹，首行 `# skill: <name>`；未知 skill 仍回 `{"status":"error","error":…}` 信封（两个错误分类器都按它判失败，纯文本会被算成成功）。`tool_result_store` 给它单独的 `SKILL_RESULT_LIMIT=60000`，超限按 `##` 分节裁、列出省略小节标题与起始行号、落盘 `.md`；围栏代码块内的 `##` 不分节；落盘失败改指向内置 `<name>/SKILL.md`。系统提示 Guidelines 与工具 description 同步说明这一行为。
- 信封文案去掉不存在的 `grep_file`（改 `read_file` + bash `grep -n`）；其它工具的单行 JSON 落盘前按 `indent=1` 重排成多行。`tools/bash_tool.py` description 改为「最小环境、无凭据，认证数据走专用工具」。

**验收。** 新增 `ops/cube-router/test_router_security.py`（`_safe_tenant_path` 四态、memory 端点 symlink 拒绝、三个 obs 端点 symlink 为空、`_dir_bytes` 不跟链接、答案分类 + `engine_failed` 帧、422→400、`/forget` 三种失败态 + 幂等 + symlink 拒绝、`/sessions/delete` 两种 mode + 回退 + 400 + 鉴权、usage/healthz 的 tk8 列表与 `disk_used_pct`）与引擎侧 `test_subprocess_env_redaction.py`、`test_cancel_tool_wait.py`、`test_loop_compact_called_ok.py`、`test_memory_corrupt_index.py`、`test_session_delete_cleanup.py`、`test_load_skill_tool.py`；`test_tool_result_store.py` 补结构化预览/分节裁剪/每 bar 一行落盘，`test_get_market_data_size.py` 改为紧凑表断言，`test_run_verify.py` 补新形状的参考价抽取。

**文档同步。** PRODUCT_DESIGN §2.2/§2.3/§3/§6/§7 按上述现状重写（租户数据在宿主 bind-mount 而非沙箱可写层；4G 只是 healthz 分母、无 fs quota；租户 env 表补 `TIMEOUT_SECONDS=300` 与 `VIBE_TRADING_ALLOWED_FILE_ROOTS=/tmp`；模板切换重建与启动清扫 `_sweep_stale_templates` 的回滚红线；`intent`/`swarmPreset` 只到 router 预算档、引擎不消费；`/sessions/delete` 契约）；README_CUSTOM 修正「launcher 请求必须带 Bearer」（launcher 无鉴权）、去掉更新操作里的 08-21 叙事、补 `VIBE_SWEEP_STALE_TEMPLATES` 与回滚步骤、记下 cube-router 仍以 root 运行的降权 TODO；OBSERVABILITY §3.6 cancel、§4 收尾提示为每轮而非一次性、§5.3 数据工具载荷、§7 `engine_failed` outcome、§9 env 表；SYSTEM-PROMPT Guidelines 与 `context.py` 文案对齐；SKILLS §2 `load_skill` 新行为；SWARM-PRESETS 触发策略改为结构化字段作用域的现状陈述；`router.py`/`launcher.py` 模块 docstring 与实现对齐（数据位置、/health 字段、launcher 无鉴权）。本文补齐 08-29 三批次的实际部署事实（v35 承载 D/E/F、v37 承载 V1–V3）。

**部署**：2026-09-05 随模板 v38（`tpl-d349a7e354a74998a0e450c5`，镜像 `vibe-engine:v38`）上生产；router.py 同次覆盖——切换前生产 router.py 仍是 1dcf5ba，即 V3 的 router 侧改动（意图预算、磁盘水位）此前从未上机，本次一并落地。顺序：先以 `VIBE_SWEEP_STALE_TEMPLATES=0` 重启验证（临时租户冒烟：冷启 10.7s / 总 27.5s，沙箱内 `env` 仅 15 个变量名、无凭据；`/memory` 对指向 router.env 的符号链接不列出、删除 404；`/forget` 删净目录），再置 1 重启，启动清扫销毁两个 v37 沙箱并删除 v37 模板。引擎机根盘构建前 100% 满，清理旧本地镜像标签与构建缓存后才能构建。备份 `router.py.bak-v37` / `router.env.bak-v37`。

## 2026-09-21 四轮评审整改 V-T1–V-T4：凭据边界收干净——回测 Runner 与 MCP stdio 子进程改走白名单 env、租户档位不读 HOME 配置、background_run 对齐 bash

第三轮把 `bash` / `background_run` 改成白名单 env 之后，四轮评审指出同一漏洞还剩两扇门：`backtest` 的 Runner 子进程（import 模型写的 `signal_engine.py`，AST 扫描只拦 import 期语句）与 MCP stdio 子进程仍 `os.environ.copy()`；而 `agent.json` 就在租户可写的 `~/.vibe-trading` 里，bash 写一个文件，下一次 attempt 引擎就以完整 env 替模型拉起任意命令。本批：

- **V-T1** `core/runner.py::_build_runtime_env` 改以 `subprocess_env.backtest_subprocess_env()` 为基底：`bash` 白名单 + loader 在子进程内取用的三个数据 token（`TUSHARE_TOKEN`/`TICKFLOW_API_KEY`/`IFIND_MCP_TOKEN`，逐个核对 `backtest/loaders/` 的 `os.environ` 读取）+ 代理/CA 变量 + `TUSHARE_`/`TICKFLOW_`/`IFIND_`/`CCXT_`/`OKX_`/`FUTU_`/`RSSHUB_` 前缀的调参项（凭据形名字仍剔除，如 `FUTU_PASSWORD`）；`OPENAI_*`/`ANTHROPIC_*`/`API_AUTH_KEY`/`JINA_API_KEY`/`ROUTER_*` 不进。`PYTHON*` 三个设置与 `PYTHONPATH` 前置保留。`Runner` 只有 `backtest_tool` 一个调用方（`run_shadow_backtest` 不走子进程），无其它受影响面。
- **V-T2** `tools/mcp.py` stdio 传输的 env 改为 `_subprocess_env()` + `server_config.env`（操作员显式写进配置的才放行）。新增 `src/config/tenant.py::tenant_safe_enabled()` 作为唯一判定（`tools/__init__.py` 的 `_tenant_safe_enabled` 改为委托，避免 config 包反向 import 工具包触发发现）；`config/loader.py` 在租户档位下：`load_agent_config()` 无显式路径时只认新增的 `VIBE_TRADING_AGENT_CONFIG`，否则返回默认；`_resolve_swarm_agent_config_path` 只认 `VIBE_TRADING_SWARM_AGENT_CONFIG`。核实生产模板（`ops/cube-engine/Dockerfile`、router `/boot` env）不放也不依赖这两个文件，租户引擎本就没有 MCP 服务端，故忽略无副作用。
- **V-T3** `tools/background_tools.py`：`cwd = run_dir or WORKDIR`（此前固定在引擎安装目录 `agent/`，bash 超时后按提示改用 `background_run` 跑同一条命令必然找不到 run_dir 里的文件）；复用 `bash_tool._audit_command`，命中写入 `security_audit` 并发 `security_audit` 进度事件；描述写明 run_dir、300 s 上限、50k 输出、`check_background(task_id)` 取结果；`tasks` 表超过 50 条时按最旧先淘汰**已完成**任务（运行中的不动）。
- **V-T4 短期** 七个数据源 skill（`data-routing`/`tushare`/`tickflow`/`ifind`/`okx-market`/`yfinance`/`ccxt`）正文顶部加固定注记：托管沙箱里凭据与境外网络都不在 bash 子进程里，行情用 `get_market_data`、网页用 `read_url`，脚本示例只在自托管有 token 的环境有效；`bash` 描述补「境外站点不可直连」。脚本本身不动；中期的进程内 `tushare_query` 白名单工具未做。
- 测试 `tests/test_credential_boundary.py`：策略表断言、`_build_runtime_env` 与真实子进程 dump `os.environ` 断言不含 `OPENAI_API_KEY`/`API_AUTH_KEY`、MCP stdio 传输 env 断言、租户档位下 HOME 两份配置被忽略而 env 路径/显式路径仍有效、`background_run` cwd = run_dir / 回退 WORKDIR / 审计 / 淘汰上限、skill 注记与 bash 文案护栏。

## 2026-09-21 四轮评审整改 V-H1–V-H3：harness——attempt 准备段失败有回执、cube-router 容量护栏不再竞态、swarm run 随 attempt 一起取消

三条 P1 都在主循环之外的骨架上：`SessionService._run_attempt` 在 `AgentLoop` 主 `try` 之前抛错（`ChatLLM()` 凭据、`build_registry`、盘满时 run 目录/trace 建不出来）只标 attempt failed、不写 assistant 回执，router 轮询 messages 等满整个预算才 504、真实错误丢失；router 的 RUNNING 上限只数 `pool` 里 ready 的实例，冷启在 `_ensure_ready`（最长 180s）之后才入池、state 重挂根本不查容量，并发冷启可到 `MAX_RUNNING + MAX_CONCURRENT_ACTIVE − 1`；swarm run 对任何取消都不响应，router 兜底取消对 `deep_team` 实际无效。本批：

- **V-H1** `loop.py::run()` 把准备段（mkdir / create_run_dir / save_request / build_messages / TraceWriter）包进 try，异常经 `_fail_before_loop` 产出与 loop 内失败同形的 `{"status":"failed","error_code","reason"}` 并发 `attempt_stats{status:"error"}`（`_emit_attempt_stats` 接受 `trace=None`）；loop 内失败路径的 trace/state 写改为 `_best_effort`。`service.py` 的 except 走 `_fail_attempt`：写 `metadata.ok=false/status=failed/error` 的回执、`update_attempt`/`append_message`/`index`/`emit` 各自 try 住互不拖累，`receipt_written` 防止正常回执之后再抛错时写第二条；`mark_running` 也进 try。router `_wait_answer` 增加 `_FailSignal`：`_ask_stream` 在事件流上看到本 attempt 的 `attempt.failed` 就打断轮询抛 `_EngineFailed`（`outcome=engine_failed`）。
- **V-H2** `get_or_create` 改为「先占位再 boot」：新建 / state 重挂 / 池内 paused 的实例都先经 `_reserve_running_slot`（`capacity_lock` 下 `_evict_for_capacity` 循环 pause LRU 空闲者直到 `running < MAX_RUNNING`，再以 `booting=True` 入池），失败时新建实例退出池并删掉半成品沙箱；booting 实例计入 running、不被 LRU/reaper 选为 victim、`/sessions/delete` 不把它当活沙箱；`/healthz` 增 `booting` 计数与逐租户标记。
- **V-H3** swarm 取消注册表移到 `swarm/runtime.py` 模块级（run_id → Event，工具每次调用新建的 runtime 与 API 单例共用），并按会话登记在等的 run（`register_session_run` / `cancel_session_runs`）；`SwarmTool._wait_for_run` 的 `cancelled_wait` 分支调用 `cancel_run`（`wait_budget_exhausted` 仍不动 run），`SessionService.cancel_current` / `delete_session` 也取消会话登记的 run；`cancel_event` 传进 `run_worker`（每次迭代顶部检查 + 传给 `invoke_tool_guarded`）与 `_run_worker_with_retries`（每次重试前检查），新增 `WorkerStatus.cancelled`，runtime 把它记成 `TaskStatus.cancelled` / `task_cancelled` 事件。
- **P2 三项** `run()` 不再无条件 `clear()` 取消令牌（先到的取消由第一个检查点转成 `cancelled`）；`build_registry` 期到达的取消记为 `_pending_cancel`，loop 注册即投递，`/cancel` 不再对准备中的 attempt 回 `no_active_loop`；router 的 `engine_cancelled` 只在引擎答复 `status=cancelled` 时为 true，另记 `engine_cancel_status`，ask_log 这一行改由 cancel 任务在拿到答复后写；`asyncio.create_task` 的 fire-and-forget（`_run_attempt`、`_cancel_attempt_bg`、`_reaper`、`_sweep_stale_templates`）存进集合并 `add_done_callback(discard)`。
- **顺带** `backtest/runner.py` 在 `tenant_safe_enabled()` 下不再 `load_dotenv()`（避免租户盘上的 .env 把白名单 env 灌回子进程）；`test_dotenv_observability.py::test_latch_still_skips_second_call` 在首次调用后 `caplog.clear()`，去掉对前序测试 logger 级别的顺序依赖。
- 测试：`tests/test_attempt_prep_failure.py`（registry 抛错 → ok=false 回执与 attempt.failed；store 失败不拖累事件；正常回执后再抛错不重复；build_registry 期取消被投递；loop 准备段两处失败的结果与 attempt_stats；run 前置取消被尊重）、`tests/test_swarm_cancel_midrun.py`（worker 工具中途取消下一迭代即停、重试前取消、run 记 cancelled、注册表跨实例与按会话取消、会话取消触达无人等待的 run）、`test_swarm_status_hydration.py` 的源码级护栏改为行为用例（预算耗尽不 cancel / attempt 取消必 cancel）、`ops/cube-router/test_router_harness.py`（4 路并发冷启只有 2 个成功且峰值 ≤ MAX_RUNNING、冷启/重挂/resume 都 evict、冷启失败释放槽位并删沙箱、booting 不被 LRU/reaper 选中、evict 循环到位、`attempt.failed` 信号在下一次轮询前结束等待、cancel 三种答复的 stats、`_spawn` 强引用）。

## 2026-09-21 四轮评审整改 V-C1–V-C2 + P2×4：上下文工程——收尾轮保留工具定义、当前请求不再被 L2 掏空、截断回复续写、思考不计入估算、单层截断、外部内容隔离扩面

四轮评审（vt-2-context）指出上下文层剩两处硬伤：**Anthropic 原生通道下收尾轮把 `tools` 整个摘掉**，而 Messages API 对含 tool_use/tool_result 块却无 `tools` 的请求回 400（这一发不可重试，attempt 以 `provider_stream_error` 收场——预算越紧越需要部分答案的那次调用越会失败；08-24 验收的 early_finalize 发生在换通道之前）；**L2 盲折叠按下标认「首条用户消息」**，续聊线程里下标 1 是交接摘要或回放轮次，当前 attempt 的用户消息只拿 DEFAULT 规则，作战室 1.5 万字符的计划 prompt 跑满几轮工具后只剩头 3000 + 尾 1200，持仓/纪律/任务要求全被掏空。本批：

- **V-C1** `providers/chat.py` 的 `stream_chat`/`chat` 增 `tool_choice` 参数：收尾轮（`loop.py` 最后一轮 / early_finalize，`worker.py` 最后一轮）保留 `registry.get_definitions()`，以 `TOOL_CHOICE_NONE` 下发——原生通道 `bind_tools(tools, tool_choice={"type":"none"})`（langchain-anthropic 1.6.1 的 `bind_tools` 对 dict 原样 `copy()` 进请求，thinking 守卫只丢 `any`/`tool`；venv 未装该包，按 uv 缓存里锁定版本的源码核对），OpenAI 兼容通道 `"none"`（langchain-openai 1.3.4 原样保留，真实 `ChatOpenAI.bind_tools` 有用例）。`capabilities.py` 新增 `tool_choice_none` 项（zhipu/glm 文档只支持 `auto`，置 False → 退回省略 `tools`）与 `anthropic` 表项；trace `forced_text_only` 带 `mode`。状态栏在最后一轮加「工具已禁用，只输出最终答案」的 `[SYSTEM]` 行。
- **V-C2** `context_policy.py`：消息按类别识别——`ContextBuilder.build_messages` 给本 attempt 的用户消息打 `vibe_class=request` 标记（LangChain 把未知键折进 `additional_kwargs`，两家序列化器都不发出，有用例断言不进 payload），`collapse_rule` 对它 `SKIP`；`FIRST_USER` 只剩「续聊线程最早的回放用户轮」这一用途，`first_user_index` docstring 改为现状。L3 兜底不变（`_select_summary_input` 仍序列化它，端到端用例验证超阈值后被摘要替换）。
- **P2 finish_reason=length** 主循环与 worker：截断回复不当最终答案——写 `output_truncated` trace/事件（worker `worker_output_truncated`），正文留在轨迹并追加「从截断处继续」提示再跑一轮（`VIBE_LENGTH_CONTINUATIONS`=2，占正常迭代），续写与原文拼接；最后一轮或次数用尽则末尾附「（输出被截断）」；`attempt_stats.output_truncations`。`llm.py::max_output_tokens()`：OpenAI 兼容通道也发 `max_tokens`（含 langchain-deepseek 原生适配器）；`VIBE_MAX_OUTPUT_TOKENS` 两通道共用，不设时各用自己的默认（原生 32000、兼容 8192）——没有做成「一个共用默认值」是因为 deepseek-chat/qwen 对 >8192 回 400 硬错，而 8192 给原生通道会把长报告砍掉四分之三；`VIBE_ANTHROPIC_MAX_TOKENS` 保留且优先。
- **P2 reasoning_content** `core/token_estimate.py::messages_for_estimate` 剔除 `reasoning_content`，`estimate_messages_tokens(count_reasoning=)` 只在 `ChatLLM.sends_reasoning_content`（moonshot）时计入；L1/L2/L3 触发估算与 `_select_summary_input` 都改走它；`loop.py` 不再把可见正文 `thinking_text` 镜像进 `reasoning_content`。transcript/trace 的思考落盘不变。
- **P2 双重截断** `tool_result_store` 成为工具与轨迹之间唯一的截断层：`bash` 去掉 50k 自裁与毫秒时间戳 dump（只剩 100 万字符内存护栏，命中显式标记），落盘为纯文本流（stdout + `--- stderr ---` + stderr，`<iter>-bash-<callid>.txt`）；`read_file` 去掉 50k 自裁，默认一页 200 行，超限只做预览、**不落盘副本**（预览指回源文件 offset/limit）。
- **P2 external-content** 远端 MCP 结果的 `text` 与 `content[*].text`（`kind=mcp_result`，扫描器 findings 进横幅）、`session_search` 片段（`kind=session_snippet`）包进 `<external-content>`；`read_file` 翻到含 `<external-content` 标记的落盘文件时把该页重新包一层（`kind=offloaded_external`）。`bash` 抓取的网络正文与本地输出无法区分，不包。
- **顺带（V2 发现）** `loop.py`/`worker.py` 流重试退避改 `sleep_unless_cancelled`（取消立即结束 run / worker，worker 的 `_stream_once` 也传 `should_cancel`）；cube-router `_evict_for_capacity` / `_reap_idle_once` 的 victim 条件加 `not i.lock.locked()`（refcount 0 但请求正在引擎里的实例不被 pause）。
- 测试 `tests/test_context_engineering_v3.py`（33 例：两通道 tool_choice 形态与 zhipu 回退、真实 ChatOpenAI.bind_tools、主循环/worker 收尾请求带 tools+none、fresh/续聊线程 15k 请求不折叠、标记不进 payload、L3 端到端兜底、length 续写/封顶标记/最后一轮标记、max_tokens 默认与优先级、估算与摘要输入剔除思考、read_file 默认页/无自裁/不落盘/外部页重包、MCP 与 session_search 包裹、退避中取消 <5s 结束）、`test_bash_output_and_audit.py` 改为单层截断用例、`test_router_harness.py` 加 lock 持有不被 pause；既有 stub 补 `tool_choice`/`should_cancel` 形参，`_StubLLMAlwaysToolCalls` 改按 `tool_choice` 判收尾。

## 2026-09-21 四轮评审整改 V-M1 + 记忆 P2×5 + V-D1/V-D2：删除对话清干净、记忆索引按类别淘汰与并发互斥、出境代理与转发 env 文档回正

四轮评审（vt-3-memory、vt-5-docs）指出两处硬伤：**「删除对话」在 offline 模式下清不干净**——`sessions.db` 的 FTS 行不从宿主碰、文档所依赖的 reindex 仓内没有任何调用方，被删对话的原文（含 laicai 附上的持仓）仍能被 `session_search` 召回成 snippet；`runs/<id>/req.json` 又存着整条 user message，两种 mode 都不删。**长期记忆层只有合并没有淘汰**——索引满 200 行时 `_rebuild_index` 按文件名序取前 200（`user_*` 排最后最先被截）、追加路径又是新条目掉出，两条路径丢的条目不同；同租户并发 attempt 的读-改-写没有互斥。文档侧：三份现状文档把出境代理消费方写成「只有 web_search 与 yfinance」，而 `read_url` 自 08-25 起一律经代理（`r.jina.ai` 不在白名单清单里）；`FORWARD_ENV` 是 16 个显式名，文档却写成 `LANGCHAIN_*` 通配，`LANGCHAIN_STREAM_USAGE=0` 等承诺的旋钮在生产拓扑里到不了引擎。本批：

- **V-M1** `core/state.py::save_request` 只存 prompt 前 200 字符 + `prompt_chars` + `prompt_sha256`（读取方 ui_services / api_server 运行列表 / CLI `show` 本就只展示前缀，全文在随会话删除的 `trace.jsonl`）；新增 `runs_for_session()` 按 `req.json` 的 `context.session_id` 找 run 目录。`SessionService.delete_session` 顺带删这些 run 目录；`SessionSearchIndex` 新增 `bind_store` / `list_session_ids` / `reconcile_with_store`，`search()` 命中目录已不在的会话时当场删行不返回；`SessionService.reconcile_orphans(goal_store)` 在 `api_server._get_session_service()` 构造时跑一次（FTS 行 + `GoalStore.delete_session_goals`，`GoalStore.list_session_ids` 为此新增）——不放进 `SessionService.__init__`，因为测试与 CLI 会以 tmp store 配全局共享索引构造 service，自动对账会把开发机真实 `sessions.db` 清空。cube-router offline 路径 `_remove_session_dir` 同样按 `req.json` 扫 `runs/`（逐个 `_safe_tenant_path`，symlink 跳过不跟），会话目录已不在时 runs 仍会被删。
- **记忆 P2 #1/#2/#7** `memory/persistent.py`：`_update_index` 删除，`_rebuild_index()` 成为唯一索引写入者（返回入选文件名集合，`add` 据此设 `last_add_indexed`），顺序 = `user` 类在前、其余 mtime 新到旧，满 200 行挤出的是最旧的非 user 条目；`VIBE_MEMORY_TTL_DAYS` 软过期（非 user 条目超期退出索引与自动召回，文件保留；默认不设 = 不过期，router 显式转发）；每个目录一把 `_DirLock`（进程内 RLock + `.MEMORY.lock` 的 `flock`，最外层才取文件锁以支持 consolidate 内嵌 rebuild），`add`/`remove`/`remove_entry`/`consolidate` 全在锁内；router `/memory/delete` 改写索引时取同一把文件锁并记 `memory/delete tenant <tk8> name <file> existed=…` 审计日志；引擎 `remember forget` 发 `memory_forgotten` progress 事件 + info 日志；`find_relevant` 同分按 `created` 新者优先再按 mtime。`RememberTool` 的 index-full warning 改为两种文案（新条目已入索引但最旧非 user 条目被挤出 / 200 行全是 user 条目新条目未入索引），description 加「同题更新优先于另存近似条目」。
- **P2 #5（/obs/prompt 脱敏）未做**：留档口径的实现是 laicai `lib/pii-redact.ts::redactFreeText`，修法是 `server/deep-run-debug.ts::getDeepRunPrompt` 返回前套一次；laicai 工作树当时被另一批次占用，且在 router 侧用 Python 复刻一份规则会造成两份实现漂移，故移交 laicai 批次。
- **V-D1** README_CUSTOM「出境代理」/ PRODUCT_DESIGN §2.3 表·§9 / OBSERVABILITY §6 图与消费方段·§9 表改为三个消费方，白名单清单补 `r.jina.ai`；README_CUSTOM 已知坑加「少放行任一消费方的域，对应工具在沙箱内整体失败而非退回直连」。`ops/` 下没有 tinyproxy 白名单模板文件（配置只在 B 端机器上），无可补。
- **V-D2** `router.py` `FORWARD_ENV` 改为「显式名单 + `FORWARD_ENV_PREFIXES=(LANGCHAIN_, VIBE_ANTHROPIC_)` 前缀」（`forwarded_env_names()`），显式名单补 `VIBE_MAX_OUTPUT_TOKENS` / `VIBE_LENGTH_CONTINUATIONS` / `VIBE_MEMORY_TTL_DAYS` / `TICKFLOW_BASE_URL`；README_CUSTOM env 表按代码逐名列出，PRODUCT_DESIGN §5 图与 OBSERVABILITY §9 表（补 `VIBE_ANTHROPIC_THINKING` / `LANGCHAIN_REASONING_EFFORT` / `LANGCHAIN_STREAM_USAGE` / `TICKFLOW_BASE_URL` / `VIBE_MEMORY_TTL_DAYS` 五行，去掉「router 不下发」）同步。
- **顺带（V3 发现）** `background_tools.py` 去掉 50k 自裁，改用 bash 同一 `_cap_output` 百万字符内存护栏，输出完整交给 `tool_result_store` 单层截断，description 同步；`BackgroundManager.reset()` + `tests/conftest.py` autouse fixture 每个用例前后重置进程级单例（`_execute` 对已被重置的任务表不再写回），治 `test_agent_goal_context` 与 `test_credential_boundary` 的后台任务通知串扰。
- 测试 `tests/test_review4_memory_sessions.py`（20 例：engine 删除只删本会话 runs、`runs_for_session` 跳 symlink/坏 JSON、req.json 预览/长度/哈希无全文、offline 删除后 `search()` 绑定目录即删行、启动对账清 FTS + goal 行、无孤儿时 noop、user 领先/最旧非 user 被挤出/三条路径同序、TTL 默认关/软过期/非法值、四线程并发 add 不丢行、锁可重入、`created` 次序键、遗留条目排后、forget 审计事件、后台单例隔离三例）；`test_memory_lifecycle.py` 的 index-full 用例改为 200 个真实条目并断言淘汰对象；`test_router_security.py` 新增转发 env（含 `LANGCHAIN_STREAM_USAGE=0`、BYOK 仍剔除内置 Anthropic 凭据）、offline 删除删 runs（不动他人 runs / 会话目录已不在仍删 / symlinked run 不跟）、`/memory/delete` 审计行。

## 2026-09-21 四轮评审整改 V5：注释与文档收口——代码注释去批次号 / 事故日期 / attempt id，HISTORY 补 08-25→27，现状文档补三条约束与 mcp_server 定位

四轮评审（vt-5-docs、vt-1-harness、vt-4-tools 的 P2）指出：fork 触及的代码里 100+ 处注释带评审批次号（F1/V2/E2/P08 R1…）、事故日期与 attempt id，脱离 HISTORY 无法解码；HISTORY 从 08-24 跳到 08-28，那三天的叙事只活在代码注释里；`tools/__init__.py` / `core/paths.py` 仍说「vibe-router 注入 / cgroup 兜底」；`api_server._data_root()` 与 `core.paths.data_root()` 两份真源、README_CUSTOM 指的是错的那份；OBSERVABILITY §2 的 copy_context 计数（3 实为 6）、§4「立刻进入 early_finalize」（实为第 2 轮起）、SKILLS.md「27 个超 10k」（实测 24）；`mcp_server.py` 定位与 `patch_skill` / `save_skill` 的永久遮蔽未入文档、未向模型披露；HISTORY 里三条当前约束（denyOut RFC1918、测试矩阵、回顾历史缺口）现状文档没有；上游 AGENT_CONTRIBUTOR_GUIDE 的文档规则与 fork 约定冲突无说明。本批：

- **注释清洗**：33 个文件 139 处替换（只动注释与 docstring，AST 去 docstring 后逐文件比对不变；`api_server.py` 是唯一逻辑改动）：纯叙事删掉，守卫型改成「规则 + 一句原因」并去掉批次号 / 日期 / attempt id；顺手改正 `swarm_tool.py` 的「12-agent preset」为 6（最宽的 `technical_analysis_panel` 是 6 agents）。保留未动：正常英语的 `no longer`（如「tenants that no longer exist」）、字符串字面量（`remember` 的告警文案、preset 关键词「组合复盘」）、两个 router 测试文件的模块 docstring（测试夹具）。
- **单一数据根**：`api_server._data_root()` 改为 `src.core.paths.data_root()` 的别名（两者语义逐字相同：`VIBE_DATA_DIR` 展开，否则 `agent/` 安装目录）；`tools/__init__.py` 与 `core/paths.py` 的注释改为 cube-router / launcher `/boot` 注入、MicroVM 规格兜底的现状；README_CUSTOM「与上游的差异」第一条改指 `src/core/paths.py`。
- **env 前缀**：README_CUSTOM env 表后补「四个前缀的来源与归属」——`VIBE_TRADING_*` 上游开关、`VIBE_*` fork 旋钮、`VT_*` / `SWARM_*` 是上游 loop.py / swarm 各自既有前缀（fork 的流重试旋钮沿用所在模块前缀），且除 `SWARM_TIMEOUT` 外都不在转发名单里；不改名（影响生产 env）。`service.py` 的「router hands laicai tenants 25」改为现状；OBSERVABILITY §4 的 early_finalize 触发时机改为「第 1 轮照常跑（工具窗钳到 10s 地板）、第 2 轮起判定」。
- **docs**：OBSERVABILITY §2 改为「三个模块六处 copy_context」并逐处列出（含 swarm runtime 两跳与回归测试）；§6 补「沙箱 denyOut RFC1918 所以隧道端点必须在 guest」一句。SKILLS.md 「27 个」改「24 个（按 load_skill 返回文本计，字节计 25）」，游离文件 `agent/skills/ashare-mootdx` 注明是上游原样文件、不动不登记；§3 表写明 `patch_skill` 永久遮蔽连模板升级也带不回、`save_skill` 同名顶替内置。`patch_skill` / `save_skill` / `delete_skill` 的 description 各加一句披露；`write_file` / `edit_file` / `backtest` / `factor_analysis` / `options_pricing` 的 description 补「何时用 / 返回什么 / 何时报错」（只改文案，参数面不动；`edit_file` 只改第一处的语义写进描述，不改行为）。README_CUSTOM 仓库结构表加 `agent/mcp_server.py`（上游 MCP 服务端、生产不用、与进程内工具面的漂移清单）与 `AGENT_CONTRIBUTOR_GUIDE.md` 两行，文首加「fork 的文档约定」覆盖上游 Documentation Rules。
- **HISTORY / PRODUCT_DESIGN**：本文补 08-25→08-27 节（从 git log 与被删注释整理）；PD §6 补沙箱到宿主 `denyOut` RFC1918 的约束、§8 补「回顾历史」功能缺口、新增附录 A 测试矩阵（本文 §6 原文保留）。
- **`ops/vibe-router/`**：README 顶部加「Frozen — not maintained」；本分支未碰过该目录，无需回退。
- 验证：全量 pytest 与 router 测试见提交说明；`uvx ruff check` 改动文件告警数与基线相同（10，均为既有）。

## 2026-09-21 四轮评审整改 V6：复核修复——兼容通道输出上限改保守、原生通道收尾轮有真实用例、冷启断连不漏沙箱、LangSmith 变量不下发、记忆锁口径与上限、bash 输出上限真成护栏

对 V1–V5 的只读复核（`regression-vt.md`）给出 2 条 P1、4 条 P2。**P1-1**：V3 给 OpenAI 兼容通道加的默认 `max_tokens=8192` 经 langchain-openai `ChatOpenAI` 无条件改名为 `max_completion_tokens` 发出（`_default_params` 与 `_get_request_payload` 两处，`model_kwargs` 里的同名键一样被改，1.3.x 没有保留旧名的开关；只有 `BaseChatOpenAI` 子类如 `ChatDeepSeek` 仍发 `max_tokens`），所有 BYOK 与兼容内置配置的请求形状因此改变而各端点接受度未验证。**P1-2**：收尾轮 `tool_choice={"type":"none"}` 走的正是生产内置模型所在的原生通道且与 adaptive thinking 叠加，本地 venv 没装 `langchain_anthropic`，零覆盖。本批：

- **P1-1** `llm.py::max_output_tokens("openai")` 在 `VIBE_MAX_OUTPUT_TOKENS` 未设时返回 `None`（兼容通道不发任何上限字段，与加上限之前一致），设了才发；原生通道不变（`VIBE_ANTHROPIC_MAX_TOKENS` 优先、默认 32000）。`OPENAI_COMPAT_MAX_OUTPUT_TOKENS_DEFAULT` 删除。README_CUSTOM 与 OBSERVABILITY §9 写明两种字段名与适配器的对应关系、设变量前须对目标端点实测。用例改为真实 `ChatOpenAI` 的 `_get_request_payload`：不设 env 无 `max_tokens`/`max_completion_tokens`，设了只有 `max_completion_tokens`；deepseek 原生适配器同一开关。
- **P1-2** 本地 `uv pip install langchain-anthropic==1.6.1`（`requirements.txt` 的锁定线；连带 langchain-core 1.4.7→1.6.3，仍在 `<2` 约束内），`test_anthropic_channel.py` 全部 20 例通过，并新增 3 例：真实 `ChatAnthropicCompat`（经 `_build_native_anthropic`，thinking=adaptive）`bind_tools(tools, tool_choice={"type":"none"})` 后 `_get_request_payload` 里 `tool_choice`、`tools`、`thinking`、`max_tokens=32000` 共存且无 warning；对照 `any` 被 thinking 守卫丢掉；`ChatLLM._bind` 走到同一 payload。不做真实 API 调用；模板上线后仍建议在 staging 租户以 `VIBE_MAX_ITERATIONS=2` 跑一次 ask 看收尾轮 200。
- **P2-1** router `get_or_create` 冷启期间 `CancelledError`：新建实例已有 `sandbox_id` 时 `_spawn(sbx_delete(...))` 分离删除（不阻塞取消；该沙箱既不在 pool 也不在 state，没有别的路径会回收它）。用例：`_ensure_ready` 期间取消 → 取消立即返回、删除在后台完成。
- **P2-2** `FORWARD_ENV_PREFIXES` 加 `FORWARD_ENV_DENY`（`LANGCHAIN_API_KEY` / `TRACING_V2` / `TRACING` / `ENDPOINT` / `BASE_URL` / `PROJECT` / `SESSION` / `HANDLER` / `ENV` / `CUSTOM_HEADERS` / `REVISION_ID` / `HUB_*`，取自 langchain-core / langsmith 的 env 读取名）与 `FORWARD_ENV_DENY_PREFIXES=("LANGSMITH_",)`；README_CUSTOM 部署段加上线前 grep router.env 的检查。用例：九个 tracing 名全设也不进 `engine_env`，`LANGCHAIN_STREAM_USAGE` / `LANGCHAIN_MODEL_NAME` 照常。
- **P2-3** 记忆文件锁口径写进 `_DirLock` docstring、PRODUCT_DESIGN §7、README_CUSTOM 已知坑：`flock` 只在同一内核内有保证，租户目录是宿主 bind-mount 进 MicroVM 的，引擎内有效、router `/memory/delete` 只防宿主侧并发，两侧之间不保证互斥（靠索引由条目文件重建自愈）。router 取锁改为 `_flock_bounded`（`LOCK_NB` + 50ms 重试，上限 `VIBE_MEMORY_LOCK_TIMEOUT_S` 默认 5s），超时记 warning 后不加锁照删。用例：另一 open file description 持锁时等到上限即绕过且索引照改；空闲锁取后释放。
- **P2-4** bash / background_run 的「1M 内存护栏」原是 `subprocess.run(PIPE)` 全量读完之后的截断，不是内存护栏。改为 `bash_tool.run_capped`：`Popen` + 两个读线程按 64k 块流式读，任一路流超过 `_OUTPUT_HARD_CAP` 字节即 `killpg` 整个进程组、只保留前缀并附标记（结果多 `output_capped` 字段）；超时同样杀进程组（`start_new_session`），治 `subprocess.run` 杀了 shell 却被仍持有管道的孙进程拖住的老问题。`_cap_output` 与 head+tail 常量删除。用例：`yes A` 在 200k 上限下秒级结束、stderr 独立封顶、恰到上限整段透传、孙进程持管道不延长超时、background_run 同一实现。文案：README_CUSTOM「工具结果形状」、OBSERVABILITY §5.3、background 描述与注释改为「流式硬上限、超过即杀」。
- 验证：`tests/test_context_engineering_v3.py -k "MaxOutputTokens or ToolChoice"` 11 例、`test_anthropic_channel.py` 23 例、`test_bash_output_and_audit.py` + `test_credential_boundary.py` 41 例、router 72 例；全量见提交说明。

## 2026-09-21 引擎镜像构建源改华为云：阿里云镜像站对 HTTP/1.1 限速

v39 构建时 apt 与 pip 在阿里云镜像站只有约 100 kB/s（python 基础层 5 分钟、apt 13 分钟、pip 半小时未完），而主机 curl 同一文件 17 MB/s。逐项排除后定位：`curl` 默认 HTTP/2，apt / pip / python urllib 只会 HTTP/1.1，阿里云 CDN 节点对 HTTP/1.1 长下载限速；与 docker 桥接网络、代理、IPv6 均无关（`--network host` 与容器内外一致慢）。清华 / 华为云走 HTTP/1.1 分别 9 / 11 MB/s（apt 包 7 / 50 MB/s），`ops/cube-engine/Dockerfile` 的 apt 与 pip 源改为 mirrors.huaweicloud.com。顺带：构建前 `docker image prune -a` 会连 python:3.12-slim 基础镜像一起删掉，下次重拉；引擎机 CubeSandbox 的 MySQL binlog 从未清理占了 19 GB（根盘 100%），已 `PURGE BINARY LOGS` 并 `SET PERSIST binlog_expire_logs_seconds=604800`。

## 2026-09-24 第五轮评审与整改：router 韧性与归属、引擎租户边界、上下文与记忆、跨线收尾、文档回正

第五轮评审在基线 `6436869`（laicai 同期 `66f0bc6`）上按五个维度对引擎做只读评审：harness（VH-）、上下文工程（VC-）、会话与记忆（VM-）、工具（VT-）、代码与文档一致性（VD-），随后逐条做对抗性复核（试图从「文档别处已写对 / 代码另有路径 / 属有意设计」三个方向推翻），按复核后的严重度整改。文档维度报 19 条（P1 7、P2 12），复核维持 P1 的两条：VD-01（「改 router.env 后重启 router、下一问指纹变化触发 /boot」与代码不符——指纹只由请求里的 `model` / `llm` 算出，router.env 不进指纹，在跑与 paused 的租户会一直带着旧凭据）与 VD-03（作战室专业报告只把 preset 放进引擎并不消费的结构化 `swarmPreset`，query 里既无 preset 名也无 swarm 指令，四个按钮三个大概率被关键词打分路由到别的团队——08-29 从 query 里删掉点名那一行起的功能回归，属 laicai 代码缺陷，由 laicai 整改线修复：`generateLabSwarmReport` 改走 `withSwarmDirective`）；其余降为 P2，其中 VD-05（`attempt_stats.output_truncations` 文档有、代码从未发出）按代码缺陷处理。整改分四条代码线并行（V1 router 与 launcher、V2 agent 循环与模型通道、V3 引擎 API / 会话 / 记忆 / 工具），合并后由 V4 做跨线收尾，最后由本批 VD 把现状文档与注释对齐。本批：

- **V1（`ops/cube-router/router.py`、`ops/cube-engine/launcher.py`、`Dockerfile`）**：
  - 等答案轮询把传输异常 / 非 200 / 非 JSON 都算失败，连续 10 次或持续 120 s 才判 502，失败期间探 launcher，引擎已停或 key 被拒立即 502；事件流解析 SSE `id:`，断开后带 `Last-Event-ID` 退避重连并按 id 去重（`VIBE_POLL_FAIL_MAX*`、`VIBE_PUMP_READ_TIMEOUT_S`）。
  - 转发与计量按 `attempt_id` 归属：续聊时引擎回放的上一轮 `llm_usage` / `attempt_stats` 不再被转发和重复记账（`stale_events_dropped`）。
  - LLM 指纹追加整份 boot env 的摘要 `|env:<sha16>`，改 router.env 后各租户下一问重启一次引擎（VD-01 的代码部分）。
  - `/boot` 前先把新 key 与 `boot-pending:<fp>` 写进 state，拿到 200 才转正；下一问用 `GET /sessions/keyprobe` 判断能否直接采纳；引擎 401 时标 `stale`，`/ask` 自动重启一次再试（`auth_reboot`）；launcher 的 `/boot` `/stop` 串行。
  - 处理槽排队设上限（`VIBE_ACTIVE_QUEUE_WAIT_S`），busy 帧带 `code` 与 `busy_reason`；`sbx_pause` 看返回码，暂停被拒计回 RUNNING；等答案窗口从请求到达起算，`attempt_meta` 帧补 `answer_deadline_s` / `engine_deadline_s`。
  - `/forget` 先写墓碑 `forgotten_at`、有界等租户锁，墓碑期内 `/ask` 回 410（`tenant_forgotten`），与之赛跑的冷启中止并清掉自己建的东西；access log 里的 `uid` 改写成 tk8，记忆删除审计只记文件名哈希；error 帧加机器可读 `code`。
  - launcher 鉴权（VT-3 的一半）：按沙箱派生的 HMAC token，只从首次 `/boot` 采纳，`VIBE_LAUNCHER_AUTH` 默认关——持 token 的 launcher 在旧 router 下永远 401、只能重建沙箱，回滚代价不值得为「本租户自伤」承担。
  - 追加：V2 核查 `.env` 时发现镜像 `chown -R vibe:vibe /app` 让租户 shell 能改写引擎代码、下一次 `/boot` 就带着共享凭据跑起来（P1），改为 `/app` 归 root 只读、构建期预编译、`ENV` 固定 `VIBE_DATA_DIR` / `PYTHONDONTWRITEBYTECODE` / `PYTHONNOUSERSITE`（未构建，验证命令写进 README_CUSTOM）；`VIBE_CONTEXT_WINDOW_TOKENS` 加入转发名单。router 测试 72 → 147。
- **V2（`agent/src/agent/{loop,context}.py`、`providers/{chat,llm}.py`、`core/*`、`swarm/worker.py`）**：
  - 单次 LLM 调用纳入 deadline 与取消：流式按 chunk 检查墙钟，到点关流、部分正文加「时间预算耗尽」标注作答，deadline 之后不再开新一轮；SDK 请求超时只收紧不放宽；L3 摘要剩余不足两轮时跳过、否则流式可取消且不重试；goal 续跑遵守收尾轮；`run()` 退出复位 contextvar。
  - 被输出上限截断的工具调用一律不执行（参数已被截坏），回 `tool_call_truncated`；顺带补发了 `attempt_stats.output_truncations`（VD-05）。
  - 原生通道消息级缓存断点改为按内容块跳过合并消息里的状态栏（转换器把状态栏与前面的工具结果合并成同一条消息，断点因此落在每轮都变的状态栏上，缓存实际只命中 tools 与 system）；`llm_usage` / `attempt_stats` 带 cache 读写量。
  - L3 摘要不再吞掉当前请求：摘要后原样回插（超长首尾截取 + transcript 指针），模板 Goal 跟随本轮请求；L1 / L2 按批改写、`compact` 工具设最小上下文门槛、阈值可按 `VIBE_CONTEXT_WINDOW_TOKENS` 封顶并用厂商实报 token 校准估算比例（系数本身待生产数据重标）。
  - 状态栏与 swarm worker 的时间行改为北京时间、美东时间与各市场时段（`core/market_clock.py`）；租户档不读任何 `.env`；成功以答案为准（只有 `metrics.csv` 不算成功）、成功路径写盘尽力而为；worker 到 60k 的 85% 先收尾写报告、托管档不再引导写跑不通的取数脚本、`worker_text` 批量发送。全量 3681 → 3756 passed。
- **V3（`agent/api_server.py`、`src/session/*`、`src/memory/*`、`src/swarm/{models,store}.py`、`src/tools/*`、`src/market_data.py`）**：
  - 多租户档取消 loopback 免鉴权（guest 里的 loopback 调用方只可能是模型自己的 shell），租户档关闭 `/swarm/runs` 直起与 `/mandate/*`、`/live/*`，无 `deadline_s` 的 attempt 给 900 s 兜底，引擎进程 non-dumpable。
  - 删除会话先登记 tombstone（进程内 + `sessions/.deleted/<sid>`），此后会话级写入一律拒写、attempt 退出时再清一遍——此前在途 attempt 收尾会用 `mkdir(parents=True)` 把刚删掉的会话写回来；swarm run 记录 `session_id`、随会话删除；在途标记按 attempt 区分；后台任务按会话隔离、限并发、随取消终止。
  - `replay=active` 只回放当前 attempt，计费与控制事件走不丢弃的通道（只有流式增量可丢）。
  - swarm 用量按 run 增量计费（顺带修掉续等到终态时整份总量再报一遍的重复计费），停止等待后仍在跑的 run 由尾段计量线程在结束时以 `source=swarm_tail` 报剩余。
  - 记忆：索引快照声明为数据、带日期与上限，`remember` 拒注入型内容，slug 截断加哈希、保留 `created` 另写 `updated`，consolidate 保留者按类型优先级并在合并前后重读副本，召回计分加 IDF 与长度归一；引擎侧会话保留期清扫（`VIBE_SESSION_RETENTION_DAYS`，默认关、未转发）；交接摘要按 `##` 分节取舍。
  - 工具：`get_market_data` 的 summary 按全序列计算（高低点落在未采样的 bar 上会丢）、两个行情工具的 `change_pct` 统一为百分数、`edit_file` 回报命中数并拒空 `old_text`、`write_file` 支持追加、分析工具的路径收进 run 根、`read_url` 补进制编码 IPv4 与解析到内网的守卫。全量 3802 passed。
- **V4（合并 V1–V3 于 `43b1f93` 之后的跨线收尾）**：续聊回放按问答对取舍、最新一对超长时首尾截取（此前最新的长回答最先被整条丢掉，追问「上面第一条」解析到上一个话题）；以及合并中发现的缺口——
  - 记忆注入：`context.py` 又包了一层围栏与声明，与 V3 快照自带的重复，外层还写着「条目不带日期」，V2 的单行 300 字符 + 2000 token 与 V3 的 8000 字符两套上限，截断时会切掉闭合标签；收敛为 `PersistentMemory` 一处实现、唯一上限估算 2000 token，召回行统一用 `recall_line()`。
  - 成功判定：loop 以 `final_content` 判成功、service 以 `answer.strip()` 判，纯空白回复两侧结论相反；loop 改为按空回复处理。长度续写、goal 中间答案等几处已持有答案的写盘仍会抛错，补成尽力而为。
  - swarm 尾段用量：V1 的严格 attempt 过滤会丢掉带上一轮 `attempt_id` 的 `swarm_tail`，router 对它开例外、计入当下的 ask，引擎的 attempt 窗口保留锚点之后的尾段；断线重连时 `Last-Event-ID` 已出缓冲则回放整个 attempt 窗口（V1 列给 V3、V3 未做）。
  - router 离线删会话对齐引擎：连带 swarm 产物，并在删除前按目录 fd 逐级 `O_NOFOLLOW` 写同一个 tombstone 标记（活沙箱里的租户代码随时可能把目录换成链接，root 不能写穿）。
  - launcher 鉴权全链路测试（真实 router 经 HTTP 驱动真实 launcher）确认无缺口，多租户档未开时 router 启动告警；BYOK 引擎不再继承内置模型的上下文窗口（另设 `VIBE_BYOK_CONTEXT_WINDOW_TOKENS`）；`TOKEN_THRESHOLD` 转发；出境私钥纳入工具输出脱敏；截断工具调用的拒绝提示指向 `write_file` 追加模式；一条经降级链真实联网的用例改为 stub。引擎 3895 passed、router 165 passed。
- **VD（本批，只改文档与注释）**：PRODUCT_DESIGN / README_CUSTOM / OBSERVABILITY / SYSTEM-PROMPT / SWARM-PRESETS 按整改后的代码回正——改 router.env 的真实生效方式与 launcher 鉴权开启回滚手册（VD-01）；预算以 laicai 显式 `timeoutS` 为准、`BUDGET_BY_INTENT` 只对不带它的调用方生效（VD-02）；`/obs` 在线面板写成执行 Trace 页的现状、ask-log / engine-log 只能 curl（VD-04）；`attempt_meta` 帧进契约（VD-06）；env 表分「router 可覆盖 / 只能改镜像」（VD-08）；LangSmith 拒绝名单按代码写精确名（VD-09）；SWARM-PRESETS 的 `cancelled_wait` 与 `{goal}` 口径（VD-10）；attempt_stats / ask_log / healthz 字段补齐（VD-11）；fork 注释与现状文档里的批次号、事故日期与 attempt id 再清一轮、扩到 `agent/backtest/`（VD-12）；失实的模块注释（VD-13）；单一数据根注明 `swarm_runs_root` 例外（VD-14）；mcp_server 与 `agent/SKILL.md` 的计数（VD-15）；symlink 守卫口径（VD-16）；主站对引擎的三处接触面（VD-17）；威胁模型的现行结论提炼进 PRODUCT_DESIGN §2.5、代码注释改指它（VD-18）。VD-07、VD-19 属 laicai 文档与注释，由 laicai 批次处理。核对跨仓描述时发现一处 laicai 与 VT 现行协议不符：laicai `vibe-trading.ts::isForeignAttemptEvent` 按 `attempt_id` 做第二道过滤时没有对 `source=swarm_tail` 开例外，router 放行的尾段用量到 laicai 又被丢掉；VT 文档按 router 行为写明了例外，laicai 侧待修。
- **V5（整合后的独立复核发现的回归）**：V4 的「id 已出缓冲就回放整窗」遇上 router 只记最近 4096 个 id 的去重，长 attempt 断线重连会把早先的 `llm_usage` 再计一次费——引擎事件 id 改为 `<epoch>-<seq>`、按序号精确续传，router 改高水位去重（旧引擎的计量事件 id 整个 ask 不淘汰）；swarm 尾段在两问之间结束时无人接收却已记为「已计费」，改为暂存到会话、下一问开头补发一次（`deferred`）；另把 deadline 截流那次调用的用量按估算补记（`estimated`）、答案轮询容忍按预算放大且 launcher 报引擎在跑就继续等、排队上限随预算放宽且 busy 帧带 `retry_after_s`、小窗口下 L3 压不到阈值以下时加冷却。
- **未做（产品决策或需另排的改造）**：VT-7 工具面收敛（目标域、假设域、因子四件套合并或隐藏，会改变对外工具面）、VM-16 记忆的用户可控 UI（laicai 设置页、router 下发开关、清空接口三方一起定）、VC-09 / VC-10 技能与工具面的产品化裁剪，都要先定产品口径。出境私钥与 launcher 的残余面：launcher 与 bash 同为 uid 1000，私钥文件对租户可读（脱敏只是按字符串匹配的纵深，换编码即可绕过），`PR_SET_DUMPABLE` 挡不住同 uid 的 `kill`——launcher 被杀后若 `:8898` 能被租户进程重新监听，伪造的 launcher 会在下一次 `/boot` 拿到完整 boot env。托管方案二选一：① launcher 以 root 运行、`setuid` 降权后再拉起引擎，私钥放 root 专属目录（需改镜像 `USER` 与进程模型）；② launcher 把私钥交给 non-dumpable 的 `ssh-agent` 托管后删文件，bash 仍能经 agent socket 用这把 key 但拿不到明文。两者都与 launcher 残余面同一改造，列入放量前的收紧清单；B 端 `permitopen` 已把这把 key 的爆炸半径限在「借用白名单代理」。其余留待后续：router 对记忆删除写墓碑（引擎侧已在 consolidate 前后重读，不依赖它）、召回 query 改用用户原话（需 laicai → router → 引擎新增可选字段）、最新回放回答在 L2 的宽松折叠、估算系数按生产数据重标、`VIBE_LAUNCHER_AUTH` 与引擎侧会话保留期的开启、laicai 侧配合项（410 不重试不告警、`swarm_tail` 例外、cache 字段落库、聊天侧补北京时间、BYOK 在 `llm{}` 里带模型窗口）。
- **发布**：新镜像未构建、未部署。顺序是先发 router（兼容 v39 引擎与 launcher）验证后再走「新镜像 → 新模板 → 切 `VIBE_CUBE_TEMPLATE_ID`」，验证通过前 `VIBE_SWEEP_STALE_TEMPLATES=0` 保留旧模板；指纹格式变化使每个租户首问重启一次引擎（与切模板同做则只冷启一次），低峰发布。state.json、租户目录、记忆条目的新形态都不需要迁移脚本。镜像与沙箱内的验证命令见 README_CUSTOM「引擎镜像构建与模板发布」与「launcher 鉴权：开启与回滚」。
