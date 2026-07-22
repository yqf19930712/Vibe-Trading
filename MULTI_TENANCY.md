# Vibe-Trading 多租户隔离技术方案（MULTI_TENANCY）— v2（已纳入评审）

> 目标：让一个对外服务（来财 / laicai）背后的多个最终用户，各自调用 Vibe-Trading agent
> 时，**数据（配置、长期记忆、会话历史、上传的交割单、券商凭据、研究目标）严格隔离**，同时
> 保留**会话连续性（线程内多轮 + 跨会话长期记忆）**，并在**资源开销可控、稳定**的前提下运行于
> 现有单台小内存 VPS。
>
> 本文是 Vibe-Trading 侧设计；laicai 侧的 `askVibeTrading` 改造见
> `laicai/docs/vibe-multitenancy-laicai.md`。
>
> **v2 变更**：经 5 路对抗评审（隔离完整性 / 会话连续性 / 资源稳定性 / 安全爆炸半径 / 运维故障）
> 后修订，结论 **approve-with-changes**。本版已纳入全部 blocker/major 修复（B1–B4、M1–M8）与
> 关键 minor，并新增 §12「网络出口与提示注入」。逐条评审追溯见 §13。

## 0. TL;DR（结论先行）

> **落地校正（2026-06 实施，详见 §14）**：正文 §0–§13 为 v2 **设计**；实际落地有三处偏差——
> ① cgroup 限额改为**池级 `MemoryMax=5G`**（router 以非 root `vibe` 跑，per-instance `systemd-run`
> 需 root，故默认未用，靠 router unit 的 cgroup + `Delegate=yes` 兜底整池；`VIBE_USE_SYSTEMD_RUN=1`
> 可 opt-in per-instance）；② **测试阶段**租户安全档位只裁 `trading_*`/`propose_mandate` 红线，**放开**
> swarm/session_search/background（见 §4.3）；③ laicai 侧连续性最终用 URL `?lt=` 传真实 threadId +
> 双触发（显式点名/线程绑定）。**已部署生产并经 Playwright 实测验收通过**（隔离 / 连续性 / 工具放开）。

- **架构选型：进程级"每用户一实例"池（process-per-tenant pool），不引入 Docker。** 全部落盘
  状态从 `Path.home()` 或安装目录派生 → **每用户独立 `HOME` + 独立进程**即可隔离落盘状态与进程
  内全局单例（`_shared_index`/`_BG`/`_registry_cache`/`_swarm_runtime`/`_goal_store`/`_ocr_engine`）。
  五路评审一致认可此架构。
- **真正的隔离边界 = 进程 + HOME + 独立端口。**（评审 M1）`API_AUTH_KEY` **不是**跨实例边界——
  Vibe 对**所有 loopback 调用方无条件信任**（`api_server.py:767-770` 在校验 key 前对本地客户端
  直接放行），故每实例必须**显式 `--host 127.0.0.1`**（`serve` 默认 `0.0.0.0`，见 `api_server.py:3295`）
  并辅以端口段防火墙。
- **Vibe 改动（最小集，B1/B4）**：把 `runs/sessions/uploads/.swarm-runs` **全部**经单一
  `_data_root()`（认 `VIBE_DATA_DIR`）解析（含 `swarm_runs_root()` 与其两处 `__file__` 硬编码
  调用点）；新增 `VIBE_MULTITENANT=1` 标记 → 缺 `VIBE_DATA_DIR` 时**启动即报错**（杜绝静默回落
  到共享安装目录）。
- **一期租户安全档位（B2/M2）**：`VIBE_TRADING_TENANT_SAFE=1` 由 router 注入，`build_registry`
  据此**排除 `SwarmTool`、`session_search`、`background_*`、以及全部 `trading_*`/`propose_mandate`/
  连接器工具**；shell 默认关；会话级 MCP 关。SwarmTool 与 trading 工具都是"非 shell 即可达"的
  always-on 工具，必须显式排除而非靠"不下发 MCP 配置"。
- **新增轻量编排器 `vibe-router`**（FastAPI，loopback，systemd）：按 `tenant_key=HMAC(ROUTER_SECRET,
  userId)` 懒启动 / 复用 / 空闲回收每用户实例；**在途引用计数**（不被 last_used 误杀，M3）、**per-uid
  创建锁 + per-thread 合流**（防双开/分脑，M6）、**`systemd-run` cgroup 硬限额**（M4）、**孤儿进程回收**
  （M5/m5）、**`attempt_id` 轮询**（修复"复用会话返回上一轮答案"的致命 bug，B3）。
- **会话连续性**：每用户实例自带其 `HOME`（长期记忆 + 会话历史 + 搜索索引），天然"跨会话长期记忆"；
  laicai 按对话线程持久化 `vibe_session_id`，同线程追问复用之 → 线程内多轮。**复用会话必须按
  `attempt_id` 等待本轮终答**（B3）。
- **资源评估（现网 4 vCPU / 7418 MB）**：单实例**空闲 ≈ 0.45 GB**，单回测峰值 ≈ 1.2–1.5 GB；
  **一期禁用 swarm 后无 fan-out**，峰值有界。预留 OS+现有服务后约 5 GB → **温实例上限 4、并发活跃
  上限 2**（保守，cgroup 兜底）；现实负载 ~1 用户，配额是防 OOM 护栏。每实例 `MemoryMax=1.6G` 由
  cgroup 强制，**保护 invest-web/market-data 不被全局 OOM-killer 误杀**。

---

## 1. 背景、威胁模型与共享状态清单

Vibe-Trading 是**单用户本地 agent**。laicai 把它当**共享多租户后端**（现网单进程
`127.0.0.1:8899`、每次匿名 `POST /sessions`、不带身份）→ 跨用户泄露。已审计 + 评审补全的**共享
状态清单**（同进程 + 同 `HOME` + 同安装目录）：

| # | 共享状态 | 位置 / 证据 | 跨用户风险 | 隔离手段 |
|---|---|---|---|---|
| 1 | 持久长期记忆 | `memory/persistent.py:24` `MEMORY_BASE=Path.home()/".vibe-trading"/"memory"`；`context.py:172` `find_relevant()` 无用户过滤 | A 记忆召回进 B prompt | HOME |
| 2 | 会话搜索索引 + `session_search` 工具 | `session/search.py:23` `~/.vibe-trading/sessions.db`；`search.py:345` 单例 `_shared_index`；`tools/session_search_tool.py:14` 常驻工具跨全部会话 | B 搜到 A 对话正文 | HOME + 进程 + 一期排除工具 |
| 3 | 后台任务管理器 | `tools/background_tools.py` 单例 `_BG`；`check(None)` 列全部任务 | B 看 A 任务输出 | 进程 + 一期排除工具 |
| 4 | 会话消息存储 | `api_server.py:44` `SESSIONS_DIR=<install>/agent/sessions`（**评审 nit：43-45**）；`:1763` `SessionStore(base_dir=SESSIONS_DIR)` | 安装目录共享 | `_data_root()`(B1/B4) |
| 5 | 上传文件（交割单） | `api_server.py:45` `UPLOADS_DIR=<install>/agent/uploads`（50MB/个，`:50`） | A 交割单被 B 读 | `_data_root()` |
| 6 | swarm 运行产物 | `swarm/store.py:72` `swarm_runs_root()=Path(__file__).parents[2]/".swarm"/"runs"`；**另两处硬编码** `api_server.py:2320`、`tools/swarm_tool.py:717`；`path_utils.py:92` 沙箱 allow-list 也由它派生 | 跨用户 swarm 输出泄露 | **B1：三处统一走 `_data_root()`** + 一期排除 SwarmTool |
| 7 | 券商 OAuth + **trading 工具** | `config/schema.py:191/222` `~/.vibe-trading/live/*/oauth/`；**`trading_connector_tool.py:319` `trading_place_order` 始终注册**；`trading/service.py:274-275` paper 路径**绕过 mandate 门**且收 caller host/port | 跨用户凭据复用 / 连接原语 / 误下单 | HOME + **M2：tenant-safe 排除全部 trading 工具** |
| 8 | 影子账户 | `~/.vibe-trading/shadow_accounts|shadow_runs|shadow_reports/` | 交易规则跨用户枚举 | HOME |
| 9 | 因子库单例 | `factors/registry.py:389` `_registry_cache`（含 `sys.modules`） | 共享编译模块 | 进程 |
| **10** | **研究目标库 GoalStore（评审 M7）** | `goal/store.py:33` `_DEFAULT_DB_PATH=Path.home()/".vibe-trading"/"sessions.db"`；`api_server.py:1786` `GoalStore()` | 研究目标/证据跨用户泄露 | HOME；**且与 #2 同名 `sessions.db`，见下** |
| — | **`os.environ`/进程全局（评审 m2）** | `api_server.py:1122/1129/1135/1139/1140/1642(TUSHARE_TOKEN)`；`providers/llm.py:478-499` | 单进程下跨租户配置串 | **仅靠进程边界隔离**（故"回退单进程"会重新打开，务必勿回退） |

**`sessions.db` 文件名冲突（M7）**：#2 搜索 FTS 索引（`search.py:23`）与 #10 GoalStore
（`goal/store.py:33`）**硬编码同一文件名** `~/.vibe-trading/sessions.db`。需核实二者是否实际打开
同一文件：若是，重命名其一（如 GoalStore 用 `goals.db`，其已支持 `VIBE_TRADING_GOAL_DB_PATH`
覆盖，`goal/store.py:34`）——这是"单写者串行"也防不住的潜在损坏（两个不同对象、不同锁）。

**威胁前提**：shell 工具默认关（`api_server.py:838-850` 需显式 env）；`web_reader` 有 SSRF 防护
拦截 loopback/内网（`web_reader_tool.py:38-57`）；会话级 MCP 默认剥离（`config/loader.py:114`，
`ALLOW_SESSION_MCP_SERVERS` 门）。但**非 shell 即可达的工具不止 `session_search`**——`SwarmTool`
（写共享 `.swarm` 且 spawn 子 agent）与 `trading_*`（auto-discover、paper 绕过门、收 host/port）
同样常驻。故租户安全档位**显式排除**它们（§4.3），不靠"不下发 MCP 配置"。

---

## 2. 方案对比与选型（不变）

| 方案 | 隔离边界 | 改 Vibe | RAM 单例 | 资源 | 抗升级 | 结论 |
|---|---|---|---|---|---|---|
| 1 剥离功能（单进程） | 应用 | 中 | 只能禁用 | 最省 | 中 | 牺牲记忆/连续 ✗ |
| **2 进程每用户** | **OS 进程** | **小** | **天然隔离** | 中 | **强** | **选用（进程版）** |
| 3 进程内 uid 命名空间 | 应用代码 | 侵入式 | 要按 uid 分桶、易漏 | 省 | 弱 | 工程量大易留坑 ✗ |

五路评审一致认可方案 2；资源评审的 `reject` 系按"多租户并发 swarm"误判，其具体修复（B1/C/G/H）
已纳入，但不触发架构重选——一期禁用 swarm 后其担忧的预算击穿场景不成立。

---

## 3. 目标架构

```
                 ┌────────────── VPS (4 vCPU / 7.4 GB, systemd) ──────────────┐
 laicai(Node)    │  vibe-router (FastAPI, 127.0.0.1:8990, 独立 systemd unit)   │
 askVibeTrading ─┼─► POST /ask {uid, query, threadId, vibeSessionId?}          │
 (Bearer ROUTER  │     • tenant_key = HMAC(ROUTER_SECRET, uid)                  │
  _TOKEN，强制)   │     • pool: uid → Instance{port,pid,home,refcount,lock,...}  │
                 │     • per-uid 创建锁 + per-thread 合流；在途 refcount         │
                 │     • attempt_id 轮询；/forget 路径校验；/healthz            │
                 │           │              │              │                    │
                 │   systemd-run --scope -p MemoryMax=1.6G --uid=vibe ...       │
                 │           ▼              ▼              ▼                    │
                 │   vibe@A:8901       vibe@B:8902     vibe@C:8903              │
                 │   --host 127.0.0.1  VIBE_MULTITENANT=1  TENANT_SAFE=1         │
                 │   HOME=/srv/vibe/users/<hmacA>  VIBE_DATA_DIR=$HOME/.vibe-trading│
                 └─────────────────────────────────────────────────────────────┘
```

---

## 4. 每用户状态隔离机制

### 4.1 用 `HOME` 收口（#1/#2/#7/#8/#9/#10）
`HOME=/srv/vibe/users/<tenant_key>` 启动 → `Path.home()/.vibe-trading/...` 全落该用户目录；进程独立
→ 全局单例各自一份。Linux 下 `HOME` 也会重定向 `~/.config`、`~/.cache`（XDG 未设时），故 matplotlib
字体缓存等亦随 HOME（见 §7 冷启动）。

### 4.2 单一数据根 `_data_root()`（B1/B4，覆盖 #4/#5/#6）
现状 `api_server.py:43-45` 三目录写死安装目录、不可 env 覆盖；**swarm 目录另在三处写死**
（`swarm/store.py:72`、`api_server.py:2320`、`tools/swarm_tool.py:717`）。补丁（向后兼容）：

```python
# agent/api_server.py
def _data_root() -> Path:
    env = os.getenv("VIBE_DATA_DIR")
    return Path(env).expanduser() if env else Path(__file__).resolve().parent  # 旧默认不变
_DATA = _data_root()
RUNS_DIR, SESSIONS_DIR, UPLOADS_DIR = _DATA/"runs", _DATA/"sessions", _DATA/"uploads"

# agent/src/swarm/store.py — 成为唯一真源
def swarm_runs_root() -> Path:
    env = os.getenv("VIBE_DATA_DIR")
    base = Path(env).expanduser() if env else Path(__file__).resolve().parents[2]
    return base / ".swarm" / "runs"
# api_server.py:2320 与 tools/swarm_tool.py:717 改为 import 并调用 swarm_runs_root()，不再各自 __file__ 重算
```

**fail-loud 不变量（B4）**：
```python
if os.getenv("VIBE_MULTITENANT") == "1" and not os.getenv("VIBE_DATA_DIR"):
    raise SystemExit("VIBE_MULTITENANT=1 requires VIBE_DATA_DIR (refuse silent shared install-dir)")
# 启动日志打印解析后的 SESSIONS_DIR / swarm_runs_root()，misconfig 要响不要静默
```
- 默认（未设 env）行为与上游/单机完全一致；多租户实例由 router 同时注入 `VIBE_DATA_DIR=$HOME/.vibe-trading`
  + `VIBE_MULTITENANT=1`，使 sessions/uploads/runs/swarm 与 memory/search/goal **同根**。

### 4.3 租户安全档位 `VIBE_TRADING_TENANT_SAFE=1`（B2/M2）
router 给每实例注入，`build_registry()` 据此排除工具（`tools/__init__.py`）。

**当前（测试阶段）配置**：只排除**会动钱的硬红线**——全部 `trading_*`（真实下单）+ `propose_mandate_profiles`（资金授权）。一个只读分析产品无论如何不得触发真实交易/资金动作，这条不可放开。
- **放开**（测试阶段）：`run_swarm`、`session_search`、`background_run`/`check_background`。进程 + HOME 隔离已把它们的状态限定在本租户（swarm runs → `swarm_runs_root()`/VIBE_DATA_DIR；`session_search` 索引 = `Path.home()/.vibe-trading/sessions.db`，故**只搜本租户自己跨 thread 的全部历史会话，绝不跨租户**）。
- `background_run` 额外需要 shell 门：router 注入 `VIBE_TRADING_ENABLE_SHELL_TOOLS=1`（它跑任意 host 命令）——**这同时放开前台 `bash`**（能力等价）。
- 资源放大（并行 swarm worker、常驻后台任务）**测试阶段接受**，仅靠 pool 级 cgroup `MemoryMax=5G` 兜底；真正多用户上量前须复审（恢复排除 swarm/background 或加每请求配额）。
- 实证（box `build_registry`）：shell ON 共 39 个工具，`session_search`/`run_swarm`/`check_background`/`background_run`/`bash` 在册，`trading_*`/`propose_mandate_profiles` 仍缺席；shell OFF 时 `background_run`/`bash` 被 shell 门挡、`session_search` 不受影响仍在册（两道门正交）。

> 历史（一期设计原值，现已放宽）：曾额外排除 `SwarmTool`/`session_search`/`background_*`。评审纠偏过"仅 `session_search` 一条非 shell 路径不成立、SwarmTool/trading 同为常驻可达"——该判断仍正确，只是测试阶段主动接受 swarm/background/search 的可达性，仅死守 trading/mandate 红线。

### 4.4 端口与监听（M1）
- 每实例 **显式 `--host 127.0.0.1`**（覆盖 `serve` 默认 `0.0.0.0`），端口段 8901–8949 分配。
- `API_AUTH_KEY` 仍设，但**文档明确其不是 loopback 隔离边界**；真正边界是 进程+HOME+端口。加端口段
  防火墙（nftables drop 段内 inter-instance + 外部）作 belt-and-suspenders。

---

## 5. 编排器 `vibe-router`

单文件 FastAPI，loopback `127.0.0.1:8990`，独立 systemd unit（web/部署重启不动池）。

### 5.1 数据结构
```python
Instance = { tenant_key, port, proc, home, last_activity, refcount:int, starting:bool, lock:asyncio.Lock }
pool: dict[tenant_key -> Instance]
pool_mutex: asyncio.Lock            # 守护 get-or-create（防双开，M6）
uid_locks: dict[tenant_key -> asyncio.Lock]
active_sem = asyncio.Semaphore(MAX_CONCURRENT_ACTIVE)   # =2（swarm 关后保守）
```

### 5.2 接口（laicai 调用）
`POST /ask {uid, query, threadId?, vibeSessionId?, timeoutS?}` → `{answer, vibeSessionId}`
- **认证（m5）**：必须校验 `Authorization: Bearer VIBE_ROUTER_TOKEN`（常量时间比较），**即使 loopback**——
  不复制 Vibe 的 loopback-trust。
- `tenant_key=HMAC(ROUTER_SECRET, uid)`。
- 取/起实例（§5.3）→ 在 `active_sem` + per-thread 合流下：若 `vibeSessionId` 有值则在该实例**续聊**，
  否则 `POST /sessions` 新建并**尽早回传** `vibeSessionId`（B3/m-ops，便于 laicai 立即落库、重试可复用）。
- **发送 + 轮询（B3，致命修复）**：`POST /sessions/{sid}/messages` 拿回 `attempt_id`；轮询直至出现
  `linked_attempt_id == attempt_id` 且非空 content 的 assistant 消息（或该 attempt status 终态）。
  **不得**沿用旧 `.at(-1)` 取最后一条 assistant（复用会话会立刻返回上一轮答案）。记录发送前消息基线。
- **会话失效兜底（M-continuity）**：`POST messages` 若 404（session 不存在，如 HOME 被 `/forget`/重置）
  → 透明新建 session、重发、回传**新** `vibeSessionId`（laicai 重绑线程）；丢上下文但长期记忆仍在
  （记忆在 HOME/memory，非 session）。

`POST /forget {uid}`（M5/m3）→ **先 SIGTERM 该实例**（勿在活 sqlite/WAL 上删）→ 路径校验后
`shutil.rmtree`：
```python
tk = hmac_sha256(ROUTER_SECRET, uid).hexdigest()
assert re.fullmatch(r"[0-9a-f]{64}", tk)
root = (BASE/tk).resolve(); assert root.parent == BASE.resolve() and root != BASE
shutil.rmtree(root, ignore_errors=True)   # 幂等
```
绝不把原始 uid 插进路径。`ROUTER_SECRET` 实为"schema key"（决定每用户目录名），**轮换即孤立全部
数据**，按不可轮换对待。

`GET /healthz` → `{instances, active, orphan_count, total_vibe_rss, free_ram, per_tenant_disk, sem_queue}`
（m5：含孤儿计数、磁盘、RSS，便于早发现泄露/OOM）。

### 5.3 生命周期与护栏
- **懒启动 + 防双开（M6）**：`async with pool_mutex:` 取 `uid_locks[tk]`；`async with uid_lock:` 复查
  pool→缺则 `starting=True` + `systemd-run --scope -p MemoryMax=1.6G -p MemorySwapMax=0 -p CPUQuota=150%
  -p OOMScoreAdjust=600 --uid=vibe -- vibe-trading serve --host 127.0.0.1 --port p`，env 注入
  `{HOME, VIBE_DATA_DIR=$HOME/.vibe-trading, VIBE_MULTITENANT=1, VIBE_TRADING_TENANT_SAFE=1, API_AUTH_KEY,
  LLM/代理 env}` → 轮询 `/health` 就绪 → 入 pool。并发同 uid 阻塞在 uid_lock、复用。
- **在途引用计数（M3，反误杀）**：每次 `/ask` 全程（含轮询）`refcount+=1`，`finally` `-=1`；
  **reaper 与 LRU 淘汰都跳过 `refcount>0`**；`last_activity` 在 **attempt 完成**时更新，非请求到达时。
  另可查实例 `_active_loops`（`service.py:140`）二次确认再 SIGTERM；强制关停前先
  `POST /sessions/{id}/cancel`（`api_server.py:2111`）drain 再 SIGKILL。
- **per-thread 合流（M6）**：router 维护 `threadId→pending vibe_session`，首轮并发合流到同一 session，
  防分脑（亦可由 laicai 对 `chat_threads` 行加咨询锁）。
- **cgroup 硬限额（M4，一期必做）**：见上 `systemd-run`；runaway 在自身 cgroup 内被杀，不连累
  invest-web/market-data；`OOMScoreAdjust` 让内核优先杀 Vibe 而非 web。
- **空闲回收**：每分钟扫，`now-last_activity>IDLE_TTL`(20min) 且 `refcount==0` → SIGTERM(超时 SIGKILL)。
- **容量上限** `MAX_INSTANCES=4`：满则 LRU 淘汰 `refcount==0` 的最久空闲再起新的；若全忙→排队/返回"繁忙"。
- **孤儿回收（M5/m5）**：每扫 `proc.poll()`+`wait()` 收僵尸；router 重启时按 state 文件
  `/run/vibe-router/pool.json` 或 argv 标记 `pkill` 清理上轮残留实例（防 0.45GB×N 泄漏致 OOM）；
  unit 设 `KillMode=control-group`、`Delegate=yes`、`LimitNOFILE`，`ExecStartPre` 清残留。

---

## 6. 会话连续性
1. **线程内多轮**：Vibe 原生支持同 `session_id` 追加消息带全历史（`service.py:153` 读全量 +
   `context.py:169` 注入），磁盘持久（`store.py`），**reap/重启后从盘恢复**。配合 B3 的 `attempt_id`
   轮询，追问得到的是**本轮**答案而非上一轮。
2. **跨会话长期记忆**：每用户实例 `~/.vibe-trading/memory/` 私有，`remember` 写 + 自动召回都在本用户
   实例内 → 跨线程/跨会话且绝不跨用户。
3. **限制（m4）**：`service.py:319` `MAX_HISTORY_CHARS=12000` 会裁掉过旧轮次，**无会话压缩**。长
   做T/复盘线程靠长期记忆而非全文回放；建议每轮自动 `remember()` 标的+结论以便 `find_relevant` 召回。
4. **治理**：记忆是用户数据——大小上限/定期 prune（§7 磁盘）、laicai 提供"查看/删除我的来财AI 记忆"、
   注销 `/forget` 级联、纳入备份。

---

## 7. 资源、并发与磁盘模型（现网实测推导）

实测：总 7418 MB，现有 invest-web(node ~122M)+market-data(uvicorn ~180M)+caddy/journal/系统 ≈ 共
~1.3 GB；**单 Vibe 空闲 RSS ≈ 437 MB**；无 torch/tf。

| 量 | 估值 | 依据 / 评审 |
|---|---|---|
| 单实例空闲 | ~0.45 GB | 实测 437 MB |
| **首次查询后（warm，m1/nit）** | ~0.7–1.1 GB | akshare/factor 缓存 + 编译模块入 `sys.modules` 后台阶 |
| 单回测峰值 | ~1.2–1.5 GB | pandas/numpy/scipy/sklearn/duckdb |
| swarm 峰值（**一期禁用**） | ~1.7–3.3 GB | `SWARM_MAX_WORKERS=4` 线程各自工作集；故一期排除，重启用须 `=1`+relocation |
| 池预算 | ~5 GB | 7.4 − (1.3 现有 + 1.0 余量) |
| `MAX_INSTANCES`（多空闲） | **4** | 保守 |
| `MAX_CONCURRENT_ACTIVE` | **2** | swarm 关后按 warm/回测峰值；2×~1.5=3 GB ≤ 预算，留余量 |
| 每实例 cgroup `MemoryMax` | **1.6 GB** | 兜底；超即 cgroup 内杀 |
| **冷启动延迟（m1）** | ~5–15 s | 重 import + 首次 CJK 字体下载(`fonts.py:80-88`,~10MB)+matplotlib 缓存重建 |

**冷启动优化（m1）**：把 NotoSansCJK 字体随镜像/安装预置（首渲染不联网）；`MPLCONFIGDIR` 指向**共享
只读**预热字体缓存（字体缓存与租户无关，可安全共享只读）；单活跃用户下保留 1 个 warm-spare 或抬高
`IDLE_TTL`，避免每次首消息吃全量 import。

**磁盘模型（M8）**：148 GB 盘，但 `runs/sessions/sessions.db/uploads(50MB/个)/memory` **随租户×线程×
回测单调增、无 governor**（代码仅 per-entry 截断、无条数/保留期）。控制：每租户目录配额（ext4/XFS
project quota 或 router `du` 拒超）、保留期清扫器（systemd timer 删 N 天前 runs/旧 session）、
`sessions.db` 定期 `VACUUM`、uploads TTL、`/healthz` 暴露磁盘用量。

**稳定性结论**：一期（swarm 关、cgroup 限额）常态内存 ~0.5–2 GB（按活跃数），并发受 `active_sem`+cgroup
双钳制，**不 OOM 且不连累 web**；~1 用户负载下配额是护栏非瓶颈。

---

## 8. Vibe 侧改动清单（最小集）

1. `api_server.py`：`RUNS/SESSIONS/UPLOADS_DIR` 经 `_data_root()`；`VIBE_MULTITENANT` fail-loud；
   启动日志打印解析目录（§4.2）。**（评审 nit：行号 43-45）**
2. `swarm/store.py:72` `swarm_runs_root()` 认 `VIBE_DATA_DIR` 并成唯一真源；`api_server.py:2320`、
   `tools/swarm_tool.py:717` 改 import 调用（B1）。
3. `tools/__init__.py`：`VIBE_TRADING_TENANT_SAFE=1` → `build_filtered_registry` 排除 SwarmTool /
   session_search / background / trading_* / propose_mandate / 连接器（B2/M2）。
4. `goal/store.py`：默认 db 文件名改 `goals.db`（或确认与 search 不冲突），消 `sessions.db` 撞名（M7）。
5. 新增 `ops/vibe-router/`：编排器（§5）+ `vibe-router.service`（systemd，`KillMode=control-group`）。
6. （可选）`serve` 增 `--host` 已存在；确认多租户实例显式传 `127.0.0.1`（M1）。

**不改**核心 agent loop / 记忆 / 搜索 / 因子实现——隔离由"进程 + HOME"达成。

---

## 9. 可选增强（后续）
- Docker 版（资源硬限额 + 更强爆炸半径；需装 Docker）。
- 每用户独立 OS uid（§5.3 已用 `--uid=vibe` 单用户；可扩为每租户 uid + 目录 700 属主隔离，缓解
  共享 FS 越权，评审 security-major）。

## 10. 上线与回滚
1. 合 Vibe 补丁 + router，本地起 2 租户做隔离/连续/资源测试（§11）。
2. laicai `askVibeTrading` 切 router（灰度内部账号）。
3. 观察内存/CPU/磁盘/孤儿/health。
4. 回滚：laicai `VIBE_API_URL` 指回旧 `:8899`。**注意（B4）**：旧单实例**不要**带 `VIBE_MULTITENANT=1`，
   否则 fail-loud；它走旧默认安装目录，仅单机/单用户用。

## 11. 测试矩阵（验收）
- **隔离**：A `remember`"持有美团 22 万"后，B 会话**召不回/搜不到**；A 的 `uploads`/`shadow`/
  `sessions.db`/`goals`/`.swarm/runs` B 不可见。
- **swarm/ trading 关**：tenant-safe 下工具列表**无** `run_swarm`/`trading_place_order`/`session_search`。
- **跨租户 session 拒绝（完整性-3）**：B 用 A 的 `vibe_session_id` 发消息 → 该实例 404/拒绝，不串答。
- **连续性**：同线程两轮 → 第 2 轮答案 ≠ 第 1 轮（B3 回归）；跨线程长期记忆本人可召回；
  `vibe_session_id` 指向已删 session → 透明新建并回传新 id。
- **资源**：并发 > `MAX_CONCURRENT_ACTIVE` 重请求 → 排队、RSS 峰值不破预算、cgroup 限额生效、不 OOM；
  空闲 > TTL 回收；**在途长回测不被 reaper/LRU 误杀**（refcount）。
- **故障**：杀实例→下次自动重启；**router 重启→孤儿被清理**、用户记忆/会话不丢；冷启动延迟达标。
- **注销**：`/forget` 后目录删（路径校验通过 `../../etc` 被拒）、实例停。

## 12. 网络出口与提示注入（评审完整性补充——最关键的"片上隔离之外"风险）

进程+HOME 只隔离**片上**数据；对个人理财应用，**离机的网络出口与注入**才是最该防的：

1. **查询载荷把用户真实持仓送往 LLM 提供方**：laicai 把"真实持仓"注入 query（`chat-tools.ts`），
   经深度引擎转给 Anthropic/代理（`ANTHROPIC_BASE_URL`/`ANTHROPIC_AUTH_TOKEN`）或 Vibe 自有 LLM key。
   **要求**：① 明确深度引擎用哪套 LLM 凭据、存哪（建议每实例从 router 注入、落该用户 HOME，不混用）；
   ② 一个租户写入 `os.environ` 的 `TUSHARE_TOKEN`/LLM key **绝不能**经记忆/搜索召回进另一租户上下文
   （进程隔离已保证，但严禁回退单进程）；③ 评估发往第三方的数据最小化（必要时脱敏标的/数量）。
2. **经上传交割单 / web_reader 内容的提示注入 → 本租户记忆投毒**：恶意对账单/网页可诱导 agent
   `remember()` 一条会在**该用户**后续会话复现的内容（跨轮投毒，survive reap，非"跨用户"故漏检）。
   **要求**：对深度引擎写记忆设审阅/白名单或来源标注；laicai 的"记忆管理"入口让用户可见可删。
3. **`threadId→vibe_session_id` 绑定必须按属主用户作用域**：若 A 的 `vibe_session_id`/`threadId` 被
   误打到 B 的实例（router bug 或 B4 未落地前 session-id 命名空间共享），连续性与隔离双破。
   §11 已加"跨租户 session 拒绝"用例。

## 13. 评审追溯（blocker/major/minor → 本文落点）
- B1 swarm 路径统一 `_data_root()` → §4.2、§8.2、§1#6。
- B2 tenant-safe 排除 SwarmTool/session_search/background → §4.3、§8.3、§11。
- B3 `attempt_id` 轮询修复 stale-read → §5.2、§6、§11、laicai §6.1。
- B4 落地补丁 + 单一数据根 + fail-loud → §4.2、§5.3、§10。
- M1 API_AUTH_KEY 非边界 + 强制 `--host 127.0.0.1` → §0、§4.4、§5.1。
- M2 真正关 trading 工具 → §4.3、§1#7、§8.3、§11。
- M3 在途 refcount 反误杀 → §5.3。
- M4 cgroup MemoryMax 一期必做 → §5.3、§7。
- M5 /forget 调用方（descope/实现）+ 先 SIGTERM 后删 → §5.2、laicai §8。
- M6 双开/分脑合流 → §5.3。
- M7 GoalStore 入表 + sessions.db 撞名 → §1#10、§8.4。
- M8 磁盘配额/清扫 → §7。
- m1 冷启动 → §7。 m2 os.environ 进程隔离声明 → §1。 m3 /forget 路径校验 → §5.2。
- m4 12K 历史/压缩 → §6。 m5 router 孤儿/env 卫生/强制 token → §5.2、§5.3。 nit 行号 → §1#4。
- 完整性补充（出口/注入/跨租户 session）→ §12、§11。

---

## 14. 落地实现与实测（2026-06 实施）

本节是**最终落地的权威记录**，与上文设计（§0–§13）的差异以本节为准。

### 14.1 生产部署形态
- **vibe-router**：systemd `vibe-router`，FastAPI uvicorn loopback `127.0.0.1:8990`，`User=vibe`，
  `WorkingDirectory=/opt/invest/vibe-router`，`EnvironmentFile=/opt/invest/vibe-router.env`（`ROUTER_SECRET`/
  `VIBE_ROUTER_TOKEN`/LLM 凭据等）。unit 设 **`MemoryMax=5G`** + `KillMode=control-group` + `Delegate=yes`
  → router 重启连带回收其启动的子实例。
- **租户目录**：`/srv/vibe/users/<64-hex tenant_key>/`（owner `vibe`），每用户独立 `HOME`；`VIBE_DATA_DIR=
  $HOME/.vibe-trading`。实例端口从 **8901** 起。
- **legacy 单实例**：`vibe-trading.service`（源码 `/opt/vibe-trading`，`serve --port 8899`）**保留作回退**，勿停。
  laicai `web.env` **同时**配 `VIBE_ROUTER_URL`(+`VIBE_ROUTER_TOKEN`) 与 `VIBE_API_URL`，**优先走 router**，
  `VIBE_API_URL` 仅 rollback（`askVibeTrading` 内 router 优先、direct 回退）。

### 14.2 与设计的关键偏差
1. **cgroup 限额 = 池级 `MemoryMax=5G`**（非 per-instance 1.6G）。原因：router 以非 root `vibe` 运行，
   `systemd-run --scope -p MemoryMax` 需要 root；故默认不用 per-instance scope，靠 router unit 自身 cgroup
   （`Delegate=yes`，子实例继承）兜底整池。`VIBE_USE_SYSTEMD_RUN=1`（以 root 跑时）可恢复 per-instance scope。
2. **测试阶段租户安全档位放宽**（§4.3）：只裁 `trading_*` + `propose_mandate_profiles`，放开
   `run_swarm`/`session_search`/`background_run`/`check_background`；router `_spawn` 额外注入
   `VIBE_TRADING_ENABLE_SHELL_TOOLS=1`（使 `background_run` 可用，**连带前台 `bash`**）。多用户上量前须收紧。
3. **python 软链坑（部署必踩）**：agent venv 与 router venv 的 python 原本软链到 `/root/.local/share/uv/
   python`（uv 管、root 私有），`vibe` 用户 exec 不到 → `rc=203`。修法：`readlink -f` 取真实**带版本号**的
   python 目录 `cp -aL` 到 `/opt/vibe-py312`（`chmod -R a+rX`），把两个 venv 的 `bin/python` 与
   `bin/python3.12` 软链都重指到它（勿 cp 那个无版本号的中间软链，否则仍指回 /root）。

### 14.3 laicai 侧最终实现
- `chat_threads.vibe_session_id`（迁移自 `db:push`）绑定 laicai 对话线程 ↔ Vibe 深度会话。
- **真实 threadId 经 URL `?lt=` 查询参数到达** `/api/chat`：`useChat` 会用自己的内部 id 覆盖请求 body 里的
  threadId，故走 URL query 把 laicai 真实 `chat_threads.id` 传到服务端（`ai-chat.tsx` 连接 `() =>
  /api/chat?lt=<activeId>`；`chat.ts` 读 `searchParams.get('lt')`）。
- **深度引擎双触发**（`chat.ts`）：① **显式点名**「用/让/请… 来财AI」(`VIBE_TRIGGER`) → 强制必调
  `ask_vibe_trading`（强提示：拿到返回前不得自行作答）；② **线程已绑定**深度会话（`vibe_session_id` 非空）
  → 后续追问即使无动词也挂上工具（`getThreadVibeSessionId` 判定，软提示让模型按需续连），杜绝续聊时
  「引擎没接上」。并加诚实约束：未真调引擎不得谎称已交给来财AI。
- `chat-tools.ts` 的 `ask_vibe_trading` 处理器按 (threadId,userId) 取旧 `vibe_session_id` → 调
  `askVibeTrading`（经 router）→ 回写新 session（复用则跳过冗余 UPDATE）。

### 14.4 实测验收（VPS 4 vCPU/7.4G + Playwright 自测真实账号）
- **隔离**：bob 召不回 alice `remember` 的秘密（Stage 1 e2e）。✓
- **触发门**：真实 nanoid 到达服务端；`explicit`/`bound` 双触发均生效。✓
- **会话连续性**：同线程追问 router **复用既有 session**（`POST /sessions/<sid>/messages`，非新建）；引擎
  记得上一轮华夏银行三笔加仓价位并校准（答复明确写「第1笔 6.60-6.62（原6.55上移）」「止盈 7.05-7.15（原
  7.10-7.20下移）」）——只有复用 session 才知道「原来那三笔」。✓
- **效率**：复用 session 约 1 min 出结果 vs 冷启约 4.5 min。
- **冷启 4.5min 拆解**（trace 实测）：LLM 多步往返 **252s（88%）**、工具执行 **34s（12%）**、进程冷启 ≈ 几秒
  （trace 外）。即慢在 agent 多步推理（17 步 thinking + 25 次工具调用，含 4 次 `read_url` 抓全文推高后续
  token），**非多租户架构开销**——单实例 legacy 跑同样问题一样慢。另见两个可优化 error：`get_market_data`
  取个股历史失败致绕路 `read_url`、`update_research_goal_status` 连错 5 次各多一轮 LLM 往返。
- **工具放开实证**：① `build_registry`（box）shell ON 共 **39** 工具，`session_search`/`run_swarm`/
  `check_background`/`background_run`/`bash` 在册、`trading_*`/`propose_mandate_profiles` 缺席、两道门
  （tenant-safe / shell-tools）正交；② 端到端引擎实际调 **`bash`×13 全 ok**、`run_dir` 隔离在租户目录、
  `pip install akshare` 跑 pandas → 放开+shell 门+隔离全生效；③ `session_search` 在册可用、隔离正确，但
  引擎倾向**现场重算**而未主动调（行为倾向，非故障）。

### 14.5 已知遗留 / 待办
- **测试档位收紧**：`bash`/`background_run` 是 host 任意命令执行、`run_swarm` 无每请求配额，仅靠池级
  cgroup 兜底；真正多用户上量前恢复裁剪 / 加每请求配额 / 去 shell env。
- **「来财AI 回顾历史」未打通**：`session_search` 工具已放开且隔离正确，但功能层未闭环——外层 Opus 会拒答
  「检索历史对话」、引擎倾向现场重算。需要时补：外层提示转交 + 引擎 preamble 提示优先 `session_search`。
- **代码落点**：Vibe-Trading 分支 `feat/vibe-multitenancy`（`acb9570` 进程隔离+router、`736b4b4` 测试阶段
  放开工具）；laicai 分支 `feat/vibe-multitenancy`（`612bb2e`）。两分支已 push。

## 15. v2 落地：CubeSandbox 沙箱化（2026-07-22 实施并切流）

§14.5 第一条红线（`bash`/`background_run` = host 任意命令执行，仅靠 cgroup 兜底）的正解落地：
**把每租户实例从「同机进程」搬进「KVM MicroVM 沙箱」**，用
[TencentCloud/CubeSandbox](https://github.com/TencentCloud/CubeSandbox)（Apache-2.0，E2B 兼容 API）承载。
shell 工具的任意命令执行从此落在独立 guest 内核里，host 不再暴露；§4 的 HOME/DATA_DIR 隔离机制原样沿用
（只是搬进了沙箱盘）。

### 15.1 部署形态

- **宿主**：阿里云 ECS `182.92.217.17`（ecs.u1-c1m2.xlarge 4C8G，北京，Ubuntu 22.04）。普通 ECS 无嵌套虚拟
  化（无 `/dev/kvm`、CPU 无 vmx），装 CubeSandbox 的 **PVM 宿主内核**（OpenCloudOS
  `6.6.69-*.cubesandbox.pvm.host`，cnb.cool Releases 预编译 DEB）+ `modprobe kvm_pvm` 后由 PVM 提供 KVM。
  100G 数据盘 XFS(reflink) 挂 `/data/cubelet`（CoW 快照硬要求）。
- **CubeSandbox one-click v0.5.1**：`CUBE_PVM_ENABLE=1 ./install.sh`，全栈 systemd
  `cube-sandbox-control.target`；CubeAPI(E2B) `:3000`、WebUI `:12088`。
- **引擎模板**（`ops/cube-engine/`）：`python:3.12-slim` + 本仓库（pip install -e）+ **in-guest launcher**
  （`launcher.py`，`:8898`）。launcher 是模板探针目标（`GET /health`），并提供 `POST /boot {env}`——按租户
  env 拉起/重启 `vibe-trading serve :8899`。镜像推本机 `registry:2`（`127.0.0.1:5000/vibe-engine`），
  `cubemastercli tpl create-from-image --probe 8898 --probe-path /health` 制模板。
- **编排器 cube-router**（`ops/cube-router/`）：§5 vibe-router 的后继，对 laicai 的 `/ask`(NDJSON)/`
  /forget`/`/healthz`/Bearer/`tenant_key` 派生完全兼容（laicai 只改 `VIBE_ROUTER_URL` 指向）。生命周期映射：

  | v1（进程） | v2（沙箱） |
  |---|---|
  | spawn 进程 + 端口池 | `POST /sandboxes`（**不带 timeout** → 永不过期）+ launcher `/boot` |
  | idle 回收 = kill | idle 20min = **pause**（盘+内存保留；resume 秒级） |
  | LLM/BYOK 切换 = kill+respawn | 仅 launcher `/boot` 重启引擎（沙箱不动、sessions 不丢） |
  | forget = kill + rm -rf HOME | delete sandbox |
  | cgroup MemoryMax | 沙箱规格（默认 2C/2G）+ MicroVM 硬隔离 |

  租户映射持久化在 `/var/lib/cube-router/state.json`（tk → sandbox_id/llm_fp/api_key），router 重启后重挂
  既有沙箱不泄漏。

### 15.2 实测坑（必读）

1. **引擎鉴权**：数据面走 cube-proxy（`http://<port>-<sandboxID>.cube.app`，宿主 split-DNS），到引擎已**不是
   loopback**，Vibe 的 loopback 信任不生效 → router 必须带 `Authorization: Bearer <API_AUTH_KEY>`（router
   生成、经 /boot 注入、持久化）。
2. **pause 后代理流量不会自动 resume**（one-click 形态），需显式 `POST /sandboxes/<id>/resume`。
3. **E2B SDK 默认给 5min TTL**（endAt 后沙箱被杀）；裸 API 创建不带 timeout 才是永不过期
   （one-click `default_timeout_insec=-1`）。
4. 阿里云北京出网：Docker Hub 直连超时（配镜像加速）、镜像内 apt 换 mirrors.aliyun.com；
   google/yahoo 不可达 → 引擎境外搜索/美股数据退化（已知接受）；A 股链路 akshare/tushare/mootdx 可能抖动，
   引擎会自动回落腾讯财经日线。sub2api LLM 代理可达。
5. PVM 内核 GRUB 参数含 `net.ifnames=0`/`console=ttyS0`，与阿里云 Ubuntu 镜像默认一致，换内核不破网。

### 15.3 验收与性能（2026-07-22 生产实测）

- 创建沙箱 → 引擎 healthy ≈ **12s**（vs v1 进程冷启分钟级预热依赖）；pause→resume 秒级。
- 生产全链路：laicai `/api/chat`「用来财AI…」→ ask_vibe_trading → cube-router 新建租户沙箱 → 5 帧
  vibe_progress → 真实行情结论 → `ai_token_usage` deep_engine 行入账。
- 内存：宿主 OS+CubeSandbox 控制面 ≈2.5G，余 ~4.5G ≈ 3 个并发 RUNNING 沙箱（`VIBE_MAX_INSTANCES=3`）；
  上量前建议升 16G。

### 15.4 回滚与遗留

- **回滚**：Vultr `web.env.bak-cube-cutover` 恢复 + restart invest-web → 秒级回到 v1 进程版（Vultr 上
  vibe-router/legacy 均保留未动）。
- 遗留：老租户数据（/srv/vibe/users）未迁移，老线程首问 `_SessionGone` 自动重建会话；CubeEgress 出网白名单
  未启用（沙箱可全量出网，比 v1 host 出网风险低但仍可收紧）；§14.5 的 `session_search` 功能层未闭环问题
  与本次无关、依旧存在。
