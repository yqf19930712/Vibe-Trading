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

**文档同步。** OBSERVABILITY.md §4 的钳制表补齐 per-tool base、写工具 1×/2× 窗口、三个声明工具的取值与嵌套不变式，§9 env 表删掉「单**只读**工具硬超时」这个 F2 之后就已经错了的措辞并新增三个变量；SWARM-PRESETS.md 触发策略补 preset 真源、tie-break、变量抽取、两层等待的嵌套关系与 F3 冷却。**本次未部署**（无镜像重建、无模板切换、未碰生产）。

## 2026-08-29：批次 V3——数据生命周期收口 + swarm 意图结构化（router 侧）

**问题一：注销不清引擎数据。** laicai 的 `deleteUser` 只让本库的域表随 cascade 清空；引擎宿主机租户目录下的长期记忆、`sessions/` 全部对话正文、`trace.jsonl`（含**未裁剪的完整 prompt**）、runs 与用户上传的交割单**永久残留**。router 的 `POST /forget` 早就完整实现且幂等，但**laicai 侧零引用**——端点存在不等于链路存在。修法在 laicai 侧（无外键的 `engine_forget_jobs` 登记表 + `deleteUser` 前后两个钩子 + 夜间重试 + 运营看板告警，见该仓记录）；router 侧只补文档，把「谁调用、什么时候调用、失败怎么办」写进 PRODUCT_DESIGN §3.2 —— 一个没有记录调用方的清理端点，下一次评审还会把它读成死代码。

**问题二：7200 散落四处。** 「swarm 两小时预算」这一个事实此前被写在四个地方：laicai `chat-tools.ts` 的 `SWARM_TIMEOUT_S`、laicai `warlab-engine.ts` 的 `SWARM_GEN_TIMEOUT_S`、router 下发的 `SWARM_TIMEOUT` env、引擎 `swarm_tool.py` 的默认值。08-24 事故记录里那句「三处同调」就是这么来的。更糟的是**意图本身**的传递方式：模型把「使用多智能体团队(swarm)分析」写成中文散文塞进 `query`，laicai 再用正则把它嗅探回结构化来决定 15min/2h 预算，引擎侧还有第三份关键词表——`结构化 →（压成散文）→ 正则嗅探回结构化 →（再压成散文）→ 正则嗅探回结构化`，每一次往返都掉信息，模型换个措辞，本该跑两小时的 swarm 就落在 15 分钟预算里。

改法：`/ask` 接收结构化的 `intent`（`standard` | `deep_team`）与 `swarmPreset`，预算由 router 的 `BUDGET_BY_INTENT` **单点推导**，下发给引擎的 `SWARM_TIMEOUT` env 也从同一常量派生。三条兼容保证：①`body.timeoutS` 显式给出时仍最优先，因此只回滚 laicai 就能立刻回到旧预算行为；②两个新字段都是 `Optional`，老 laicai 不发即走原路径；③`intent`/`swarm_preset` 与 `deadline_s` 并列下发，不认识它们的引擎版本忽略即可——router 因此可以先于引擎发布。`swarmPreset` 只做形状校验、**不比对 router 侧的名单副本**：preset 的唯一真源是引擎的 `agent/src/swarm/presets/*.yaml`，一份过期的副本会去误拒引擎实际支持的 preset（V1-B 修的正是同一类病）。ask 日志新增 `intent` 与 `budget_source`，「我的 swarm 为什么只拿到 15 分钟」从此有据可查。

**问题三：4G 打满没有失败模式。** 租户可写层封顶 4G，但打满之后会怎样此前无定义——引擎的记忆写入与 trace 写入都是裸 `write_text`，磁盘满表现为 attempt 中途抛异常，且没有任何东西指向「这个租户没空间了」。本批次**只做只读的那一半**：`/healthz` 增 `disk` 段与每租户 `disk_bytes`/`over_watermark`，新增 `GET /tenants/usage` 列 Top N，超 80% 水位打 warn；`du` 结果缓存 5 分钟（healthz 会被轮询，逐次遍历数 GB 目录不可接受），且对缺失/竞态目录一律返回 0 而不抛——健康检查不能被一块满盘拖下水。

**真正的清扫（删 sessions/runs/uploads）刻意不做**，代码里留 `TODO(retention)` 写明落地前置条件：先积累两周真实用量再定保留窗（凭证据而不是凭猜测定阈值）、上线必须先 `--dry-run` 人工核对无活跃会话、引擎侧 FTS 索引要同步清死行否则搜索返回死链。`memory/` 永不参与清扫——那是用户资产，只有用户手删或 `/forget` 能动。删用户数据是整个计划里失误代价最高的一步，把它放在最后是刻意的。

**验收**：`python -m py_compile ops/cube-router/router.py` 通过；新增 `ops/cube-router/test_router_budget.py` 10 条纯逻辑用例全过（预算推导四种组合、`timeoutS` 优先级、引擎 env 与 `BUDGET_BY_INTENT` 一致性、磁盘统计的求和/缺目录/缓存/水位/缓存回收）。**本次未部署**（未重建镜像、未切模板、未碰生产）。
