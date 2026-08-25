"""cube-router — multi-tenant orchestrator for Vibe-Trading on CubeSandbox.

Successor of ops/vibe-router/router.py. Same public API (`/ask` NDJSON,
`/forget`, `/healthz`, same bearer auth), but tenant instances are no longer
host processes: each tenant gets a KVM MicroVM sandbox created from a
CubeSandbox template (image: python + vibe-trading + in-guest launcher).

Per-tenant layout inside the sandbox (template default):
  - launcher on :8898 — `GET /health`, `POST /boot {env}`, `POST /stop`
  - engine   on :8899 — `vibe-trading serve`, spawned by the launcher with the
    per-tenant env the router sends (LLM creds / BYOK / tenant flags)
  - HOME=/home/vibe, VIBE_DATA_DIR=/home/vibe/.vibe-trading → sessions/memory
    persist on the sandbox's writable layer across pause/resume.

Lifecycle mapping (v1 → v2):
  spawn process   → create sandbox (E2B-compatible CubeAPI) + POST /boot
  kill idle       → pause sandbox (disk + memory state kept; resume is fast)
  LLM switch      → POST /boot with new env (engine restart inside the guest;
                    no sandbox respawn, sessions untouched)
  forget          → delete sandbox + drop state row

Sandbox data-plane access goes through cube-proxy's E2B-style host routing:
  http://<port>-<sandbox_id>.<SANDBOX_DOMAIN>/   (host DNS resolves *.cube.app
  to the node; plain HTTP on the proxy's HTTP port).

State (tenant_key → sandbox_id / llm fingerprint) lives in a JSON file so a
router restart re-attaches to existing sandboxes instead of leaking them.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import shutil
import time
from collections import deque
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

log = logging.getLogger("cube-router")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

# ── Config (env) ──────────────────────────────────────────────────────────────
ROUTER_SECRET = os.environ.get("VIBE_ROUTER_SECRET", "")   # HMAC key → tenant id (NOT rotatable)
ROUTER_TOKEN = os.environ.get("VIBE_ROUTER_TOKEN", "")     # Bearer required from laicai
CUBE_API_URL = os.environ.get("CUBE_API_URL", "http://127.0.0.1:3000").rstrip("/")
CUBE_API_KEY = os.environ.get("CUBE_API_KEY", "e2b_000000")
TEMPLATE_ID = os.environ.get("VIBE_CUBE_TEMPLATE_ID", "")
SANDBOX_DOMAIN = os.environ.get("VIBE_SANDBOX_DOMAIN", "cube.app")
SANDBOX_HTTP_PORT = os.environ.get("VIBE_SANDBOX_HTTP_PORT", "80")  # cube-proxy HTTP port
ENGINE_PORT = 8899
LAUNCHER_PORT = 8898
STATE_FILE = Path(os.environ.get("VIBE_STATE_FILE", "/var/lib/cube-router/state.json"))
# Tenant engine data (sessions.db, runs/, memory/) lives on the HOST, one dir per
# tenant, bind-mounted into the sandbox at GUEST_DATA_DIR via CubeSandbox's
# host-mount. The sandbox's own writable layer then holds nothing worth keeping,
# so a Vibe-Trading upgrade is "delete sandbox, create from the new template" —
# no in-place patching of live sandboxes, no data loss. The host path must sit
# under cubemaster's allowed_host_mount_prefixes (conf.yaml extra_conf).
DATA_ROOT = Path(os.environ.get("VIBE_HOST_DATA_ROOT", "/data/shared/vibe"))
GUEST_DATA_DIR = "/home/vibe/.vibe-trading"
GUEST_UID = GUEST_GID = 1000  # the image's `vibe` user
MAX_RUNNING = int(os.environ.get("VIBE_MAX_INSTANCES", "3"))          # concurrent RUNNING sandboxes
MAX_CONCURRENT_ACTIVE = int(os.environ.get("VIBE_MAX_CONCURRENT_ACTIVE", "2"))
IDLE_TTL_S = int(os.environ.get("VIBE_IDLE_TTL_S", str(20 * 60)))     # pause after idle
READY_TIMEOUT_S = int(os.environ.get("VIBE_READY_TIMEOUT_S", "180"))  # create+boot budget
POLL_INTERVAL_S = float(os.environ.get("VIBE_POLL_INTERVAL_S", "3"))
DEFAULT_ASK_TIMEOUT_S = int(os.environ.get("VIBE_ASK_TIMEOUT_S", str(15 * 60)))
# Per-ask observability: one JSONL line per /ask (segment timings, outcome,
# attempt_id) so slow/failed asks can be traced without any extra infra.
ASK_LOG = Path(os.environ.get("VIBE_ASK_LOG", "/var/lib/cube-router/ask_log.jsonl"))
ASK_LOG_MAX_BYTES = 20 * 1024 * 1024
# LLM / data-source env forwarded into each tenant engine (via launcher /boot).
FORWARD_ENV = [
    "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_API_BASE", "OPENAI_MODEL",
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    "LANGCHAIN_PROVIDER", "LANGCHAIN_MODEL_NAME", "LANGCHAIN_TEMPERATURE",
    "LANGCHAIN_NO_TEMPERATURE_MODELS",
    "TUSHARE_TOKEN", "VIBE_TRADING_SEARCH_BACKENDS",
]

# In-guest egress tunnel credentials (optional): private key file on the host
# + ssh destination (server B). Injected into each sandbox via launcher /boot.
EGRESS_KEY_FILE = os.environ.get("VIBE_EGRESS_KEY_FILE", "")
EGRESS_SSH_DEST = os.environ.get("VIBE_EGRESS_SSH_DEST", "")
_EGRESS_KEY_B64 = ""
if EGRESS_KEY_FILE:
    try:
        _EGRESS_KEY_B64 = base64.b64encode(Path(EGRESS_KEY_FILE).read_bytes()).decode()
    except OSError as e:
        logging.getLogger("cube-router").warning("egress key unreadable: %s", e)

if not ROUTER_SECRET or not ROUTER_TOKEN:
    raise SystemExit("cube-router requires VIBE_ROUTER_SECRET and VIBE_ROUTER_TOKEN")
if not TEMPLATE_ID:
    raise SystemExit("cube-router requires VIBE_CUBE_TEMPLATE_ID")


def tenant_key(uid: str) -> str:
    """Stable, irreversible per-tenant id. Same derivation as vibe-router v1 so
    existing laicai thread↔session bindings keep their tenant identity."""
    return hmac.new(ROUTER_SECRET.encode(), uid.encode(), hashlib.sha256).hexdigest()


MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:\-\[\]]{0,99}")
BYOK_PROVIDERS = {
    "openai": "openai",
    "claude": "openai",
    "gemini": "gemini",
    "deepseek": "deepseek",
    "kimi": "kimi",
    "glm": "glm",
}


def llm_fingerprint(model: Optional[str], llm: Optional["LlmOverride"]) -> Optional[str]:
    if llm is not None:
        raw = "|".join([llm.provider, llm.model, llm.apiKey, llm.baseUrl])
        return "byok:" + hashlib.sha256(raw.encode()).hexdigest()[:16]
    if model:
        return f"builtin:{model}"
    return None


def engine_env(model: Optional[str], llm: Optional["LlmOverride"]) -> tuple[dict, str]:
    """Env the launcher passes to `vibe-trading serve` inside the guest.
    Returns (env, api_key): the engine validates `Authorization: Bearer
    <API_AUTH_KEY>` on every non-loopback call, so the router must keep the
    key it minted for the instance."""
    env = {k: os.environ[k] for k in FORWARD_ENV if k in os.environ}
    if llm is not None:
        for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
            env.pop(k, None)
        env["LANGCHAIN_PROVIDER"] = BYOK_PROVIDERS[llm.provider]
        env["LANGCHAIN_MODEL_NAME"] = llm.model
        env["OPENAI_API_KEY"] = llm.apiKey
        env["OPENAI_BASE_URL"] = llm.baseUrl
        env["OPENAI_API_BASE"] = llm.baseUrl
    elif model:
        env["LANGCHAIN_MODEL_NAME"] = model
    env.update(
        {
            "VIBE_DATA_DIR": "/home/vibe/.vibe-trading",
            "VIBE_MULTITENANT": "1",
            "VIBE_TRADING_TENANT_SAFE": "1",
            # Shell tools are fair game now: "arbitrary commands" land inside a
            # hardware-isolated MicroVM, not on the host.
            "VIBE_TRADING_ENABLE_SHELL_TOOLS": "1",
        }
    )
    # Tenant performance/reliability tier (batches 2+3). Router env overrides;
    # incident 2026-08-24 showed the engine defaults (50 iters, 1800s tool and
    # swarm timeouts) let a run outlive every caller budget.
    for key, default in (
        ("VIBE_MAX_ITERATIONS", "25"),
        ("VIBE_TRADING_DATA_CACHE", "1"),
        ("VIBE_TRADING_TOOL_TIMEOUT_SECONDS", "300"),
        # Swarm committees legitimately run tens of minutes to hours (multi-
        # layer DAG × multi-iteration workers). The wait is still clamped to
        # the attempt's remaining budget (cap_timeout), so no inversion — the
        # two hours only materialize when laicai grants a matching timeoutS
        # for swarm asks.
        ("SWARM_TIMEOUT", "7200"),
        # LLM streaming read timeout (httpx). The engine default of 120s is
        # too tight for long-context opus-class calls: incident 2026-08-24, a
        # swarm worker's stream went silent >120s twice in a row (ReadTimeout
        # at iteration 10 and again on the task retry), failing the whole
        # investment_committee run. 300s rides out thinking pauses while a
        # genuinely dead upstream still fails within one worker iteration.
        ("TIMEOUT_SECONDS", "300"),
        # ddgs 9.x has no google/bing; "auto" rotates every engine it has.
        ("VIBE_TRADING_SEARCH_BACKENDS", "auto"),
    ):
        env[key] = os.environ.get(key, default)
    # Whitelisted foreign egress: the launcher builds an in-guest SSH tunnel
    # to server B's loopback tinyproxy (domain filter there); web_search and
    # the yfinance loader then use VIBE_TRADING_EGRESS_PROXY. Key material is
    # consumed by the launcher and never enters the engine process env.
    if _EGRESS_KEY_B64 and EGRESS_SSH_DEST:
        env["VIBE_EGRESS_SSH_KEY_B64"] = _EGRESS_KEY_B64
        env["VIBE_EGRESS_SSH_DEST"] = EGRESS_SSH_DEST
        env.setdefault("VIBE_TRADING_EGRESS_PROXY", "http://127.0.0.1:8118")
    api_key = hashlib.sha256(os.urandom(16)).hexdigest()
    env["API_AUTH_KEY"] = api_key
    return env, api_key


# ── Persistent tenant→sandbox map ─────────────────────────────────────────────
def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def _save_state() -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1))
    tmp.replace(STATE_FILE)


state: dict = {}  # tk -> {"sandbox_id": str, "llm_fp": str|None}


# ── CubeAPI (E2B-compatible control plane) ───────────────────────────────────
api = httpx.AsyncClient(
    base_url=CUBE_API_URL,
    headers={"X-API-Key": CUBE_API_KEY},
    timeout=httpx.Timeout(30.0, read=120.0),
)
http = httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=120.0))


def tenant_data_dir(tk: str) -> Path:
    """Host dir bind-mounted at the engine's VIBE_DATA_DIR for this tenant."""
    d = DATA_ROOT / tk
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chown(d, GUEST_UID, GUEST_GID)  # engine runs as uid 1000 in the guest
    except PermissionError:
        log.warning("cannot chown %s to %d:%d", d, GUEST_UID, GUEST_GID)
    return d


async def sbx_create(tk: str) -> str:
    host_mount = json.dumps([{
        "hostPath": str(tenant_data_dir(tk)),
        "mountPath": GUEST_DATA_DIR,
        "readOnly": False,
    }])
    r = await api.post("/sandboxes", json={
        "templateID": TEMPLATE_ID,
        "metadata": {"host-mount": host_mount},
    })
    if r.status_code not in (200, 201):
        raise HTTPException(502, f"sandbox create failed: {r.status_code} {r.text[:200]}")
    sid = r.json().get("sandboxID") or r.json().get("sandboxId")
    if not sid:
        raise HTTPException(502, "sandbox create returned no id")
    return sid


async def sbx_info(sandbox_id: str) -> Optional[dict]:
    r = await api.get(f"/sandboxes/{sandbox_id}")
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


async def sbx_resume(sandbox_id: str) -> bool:
    r = await api.post(f"/sandboxes/{sandbox_id}/resume", json={})
    return r.status_code in (200, 201, 204, 409)  # 409 = already running


async def sbx_pause(sandbox_id: str) -> None:
    try:
        await api.post(f"/sandboxes/{sandbox_id}/pause")
    except Exception as e:
        log.warning("pause %s failed: %s", sandbox_id[:12], e)


async def sbx_delete(sandbox_id: str) -> None:
    """Delete a sandbox, resuming it first if needed.

    CubeAPI refuses to delete a paused sandbox ("sandbox not in normal state")
    and answers 500 — which httpx does not raise on, so without the status check
    below the failure is swallowed and the sandbox leaks forever, holding disk
    and a slot against VIBE_MAX_INSTANCES.
    """
    try:
        await sbx_resume(sandbox_id)
    except Exception as e:
        log.warning("resume-before-delete %s failed: %s", sandbox_id[:12], e)
    try:
        r = await api.delete(f"/sandboxes/{sandbox_id}")
        if r.status_code not in (200, 202, 204, 404):
            log.warning("delete %s -> %s %s", sandbox_id[:12], r.status_code, r.text[:200])
    except Exception as e:
        log.warning("delete %s failed: %s", sandbox_id[:12], e)


def guest_url(sandbox_id: str, port: int) -> str:
    host = f"{port}-{sandbox_id}.{SANDBOX_DOMAIN}"
    if SANDBOX_HTTP_PORT not in ("80", ""):
        return f"http://{host}:{SANDBOX_HTTP_PORT}"
    return f"http://{host}"


# ── Instance pool (in-memory runtime state over the persistent map) ──────────
class Instance:
    def __init__(self, tk: str, sandbox_id: str, llm_fp: Optional[str], api_key: Optional[str] = None):
        self.tk = tk
        self.sandbox_id = sandbox_id
        self.llm_fp = llm_fp
        self.api_key = api_key
        self.refcount = 0
        self.last_activity = time.monotonic()
        self.lock = asyncio.Lock()
        self.paused = False

    @property
    def base_url(self) -> str:
        return guest_url(self.sandbox_id, ENGINE_PORT)

    @property
    def launcher_url(self) -> str:
        return guest_url(self.sandbox_id, LAUNCHER_PORT)


pool: dict[str, Instance] = {}
pool_mutex = asyncio.Lock()
uid_locks: dict[str, asyncio.Lock] = {}
active_sem = asyncio.Semaphore(MAX_CONCURRENT_ACTIVE)


async def _launcher_health(inst: Instance) -> Optional[dict]:
    try:
        r = await http.get(f"{inst.launcher_url}/health", timeout=8.0)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


async def _boot_engine(inst: Instance, fp: Optional[str], env: dict, api_key: str) -> None:
    r = await http.post(
        f"{inst.launcher_url}/boot", json={"env": env},
        timeout=httpx.Timeout(30.0, read=float(READY_TIMEOUT_S)),
    )
    if r.status_code != 200:
        raise HTTPException(502, f"engine boot failed: {r.status_code} {r.text[:200]}")
    inst.llm_fp = fp
    inst.api_key = api_key
    state.setdefault(inst.tk, {})["llm_fp"] = fp
    state[inst.tk]["sandbox_id"] = inst.sandbox_id
    state[inst.tk]["api_key"] = api_key
    state[inst.tk]["template_id"] = TEMPLATE_ID
    _save_state()


async def _ensure_ready(
    inst: Instance, fp: Optional[str], env: dict, api_key: str,
    meta: Optional[dict] = None,
) -> None:
    """Make the sandbox reachable and the engine running with the wanted LLM env."""
    h = await _launcher_health(inst)
    if h is None:
        # Paused (or proxy lost it) — explicit resume, then retry.
        if meta is not None:
            meta["resumed"] = True
        await sbx_resume(inst.sandbox_id)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and h is None:
            await asyncio.sleep(1.5)
            h = await _launcher_health(inst)
        if h is None:
            raise HTTPException(502, "sandbox unreachable after resume")
    inst.paused = False
    if h.get("engine") == "running" and inst.llm_fp == fp and inst.api_key:
        return
    if inst.refcount > 0 and inst.llm_fp != fp:
        raise HTTPException(503, "instance busy; retry to switch model")
    if meta is not None:
        meta["booted"] = True
    await _boot_engine(inst, fp, env, api_key)


async def get_or_create(
    tk: str, model: Optional[str] = None, llm: Optional["LlmOverride"] = None,
    meta: Optional[dict] = None,
) -> Instance:
    fp = llm_fingerprint(model, llm)
    t0 = time.monotonic()
    async with pool_mutex:
        lock = uid_locks.setdefault(tk, asyncio.Lock())
    async with lock:
        inst = pool.get(tk)
        if inst is None:
            st = state.get(tk)
            if st and st.get("sandbox_id"):
                info = await sbx_info(st["sandbox_id"])
                if info is None:
                    state.pop(tk, None)
                    _save_state()
                elif st.get("template_id") != TEMPLATE_ID:
                    # Engine code is baked into the image, so a new template only
                    # reaches a tenant by rebuilding its sandbox. Lossless: the
                    # data dir is host-mounted, not in the writable layer.
                    log.info("tenant %s template %s -> %s; rebuilding sandbox",
                             tk[:8], st.get("template_id"), TEMPLATE_ID)
                    await sbx_delete(st["sandbox_id"])
                    state.pop(tk, None)
                    _save_state()
                else:
                    inst = Instance(tk, st["sandbox_id"], st.get("llm_fp"), st.get("api_key"))
        if inst is None:
            await _evict_for_capacity()
            sandbox_id = await sbx_create(tk)
            inst = Instance(tk, sandbox_id, None)
            if meta is not None:
                meta["cold_start"] = True
            log.info("tenant %s -> new sandbox %s", tk[:8], sandbox_id[:12])
        env, api_key = engine_env(model, llm)
        await _ensure_ready(inst, fp, env, api_key, meta=meta)
        if meta is not None:
            meta["sandbox_ready_ms"] = int((time.monotonic() - t0) * 1000)
        async with pool_mutex:
            pool[tk] = inst
        return inst


async def _evict_for_capacity() -> None:
    """Cap concurrently RUNNING sandboxes: pause the LRU idle one when full."""
    running = [i for i in pool.values() if not i.paused]
    if len(running) < MAX_RUNNING:
        return
    idle = sorted((i for i in running if i.refcount == 0), key=lambda i: i.last_activity)
    if not idle:
        raise HTTPException(503, "all instances busy; retry shortly")
    victim = idle[0]
    log.info("pausing LRU tenant %s (%s)", victim.tk[:8], victim.sandbox_id[:12])
    await sbx_pause(victim.sandbox_id)
    victim.paused = True


# ── Vibe session helpers (unchanged semantics from v1) ───────────────────────
def _engine_headers(inst: Instance) -> dict:
    return {"Authorization": f"Bearer {inst.api_key}"} if inst.api_key else {}


async def _vibe(inst: Instance, method: str, path: str, **kw):
    headers = {**_engine_headers(inst), **kw.pop("headers", {})}
    return await http.request(method, f"{inst.base_url}{path}", headers=headers, **kw)


async def _ensure_session(inst: Instance, vibe_session_id: Optional[str]) -> str:
    if vibe_session_id:
        return vibe_session_id
    r = await _vibe(inst, "POST", "/sessions", json={"title": "laicai"})
    r.raise_for_status()
    sid = r.json().get("session_id")
    if not sid:
        raise HTTPException(502, "vibe returned no session_id")
    return sid


async def _post_turn(
    inst: Instance, sid: str, query: str, deadline_s: Optional[float] = None
) -> Optional[str]:
    payload: dict = {"content": query}
    if deadline_s is not None:
        # The engine finalizes with what it has before this budget runs out
        # (batch 3) instead of grinding past the caller's timeout.
        payload["deadline_s"] = round(deadline_s, 1)
    r = await _vibe(inst, "POST", f"/sessions/{sid}/messages", json=payload)
    if r.status_code == 404:
        raise _SessionGone()
    r.raise_for_status()
    return r.json().get("attempt_id")


async def _wait_answer(
    inst: Instance, sid: str, attempt_id: Optional[str], timeout_s: int
) -> str:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        await asyncio.sleep(POLL_INTERVAL_S)
        m = await _vibe(inst, "GET", f"/sessions/{sid}/messages", params={"limit": 50})
        if m.status_code != 200:
            continue
        msgs = m.json()
        for msg in reversed(msgs):
            if msg.get("role") != "assistant":
                continue
            content = (msg.get("content") or "").strip()
            if not content:
                continue
            if attempt_id and msg.get("linked_attempt_id") not in (attempt_id, None):
                continue
            return content
    raise HTTPException(504, "deep engine timed out")


async def _pump_events(inst: Instance, sid: str, q: "asyncio.Queue[dict]") -> None:
    try:
        async with http.stream(
            "GET",
            f"{inst.base_url}/sessions/{sid}/events",
            params={"replay": "active"},
            headers=_engine_headers(inst),
            timeout=None,
        ) as r:
            ev_type: Optional[str] = None
            async for line in r.aiter_lines():
                if line.startswith("event:"):
                    ev_type = line[6:].strip()
                elif line.startswith("data:"):
                    raw = line[5:].strip()
                    try:
                        payload = json.loads(raw)
                    except Exception:
                        payload = raw
                    q.put_nowait({"ev": ev_type or "message", "data": payload})
                    ev_type = None
    except Exception as e:
        log.info("event pump ended (%s): %s", sid, e)


class _SessionGone(Exception):
    pass


# ── Ask metrics (in-process; reset on router restart) ────────────────────────
metrics: dict[str, Any] = {
    "asks_total": 0,
    "asks_ok": 0,
    "asks_timeout": 0,
    "asks_busy": 0,
    "asks_error": 0,
    "started_at": time.time(),
}
recent_ask_ms: "deque[int]" = deque(maxlen=100)


def _percentile(values: list[int], pct: float) -> Optional[int]:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round(pct * (len(ordered) - 1))))
    return ordered[idx]


def _record_ask(stats: dict) -> None:
    """Update counters and append one JSONL line to the ask log. Best-effort."""
    outcome = stats.get("outcome")
    metrics["asks_total"] += 1
    if outcome == "ok":
        metrics["asks_ok"] += 1
    elif outcome == "timeout":
        metrics["asks_timeout"] += 1
    elif outcome == "busy":
        metrics["asks_busy"] += 1
    else:
        metrics["asks_error"] += 1
    total_ms = stats.get("total_ms")
    if isinstance(total_ms, int) and outcome == "ok":
        recent_ask_ms.append(total_ms)
    try:
        ASK_LOG.parent.mkdir(parents=True, exist_ok=True)
        if ASK_LOG.exists() and ASK_LOG.stat().st_size > ASK_LOG_MAX_BYTES:
            ASK_LOG.replace(ASK_LOG.with_suffix(".jsonl.1"))
        with ASK_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.time(), **stats}, ensure_ascii=False) + "\n")
    except OSError as e:
        log.warning("ask log write failed: %s", e)


# ── API ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="cube-router")


class LlmOverride(BaseModel):
    provider: str
    model: str
    apiKey: str
    baseUrl: str


class AskBody(BaseModel):
    uid: str
    query: str
    threadId: Optional[str] = None
    vibeSessionId: Optional[str] = None
    model: Optional[str] = None
    llm: Optional[LlmOverride] = None
    timeoutS: Optional[int] = None


def _auth(authorization: Optional[str]) -> None:
    expected = f"Bearer {ROUTER_TOKEN}"
    if not authorization or not hmac.compare_digest(authorization, expected):
        raise HTTPException(401, "unauthorized")


def _frame(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False) + "\n"


def _classify_status(status_code: int) -> str:
    return {503: "busy", 504: "timeout", 502: "upstream_failed"}.get(status_code, "error")


async def _ask_stream(body: AskBody, timeout_s: int):
    tk = tenant_key(body.uid)
    t_req = time.monotonic()
    stats: dict[str, Any] = {
        "tk8": tk[:8],
        "channel": "byok" if body.llm else "builtin",
        "model": (body.llm.model if body.llm else body.model) or None,
        "timeout_s": timeout_s,
        "outcome": "incomplete",
    }
    engine_stats: Optional[dict] = None

    def _grab_engine_stats(ev: dict) -> None:
        nonlocal engine_stats
        if ev.get("ev") == "attempt_stats" and isinstance(ev.get("data"), dict):
            engine_stats = ev["data"]

    try:
        sem_t0 = time.monotonic()
        async with active_sem:
            stats["queue_wait_ms"] = int((time.monotonic() - sem_t0) * 1000)
            meta: dict[str, Any] = {}
            try:
                inst = await get_or_create(tk, body.model, body.llm, meta=meta)
            finally:
                stats.update(meta)
            inst.refcount += 1
            try:
                async with inst.lock:
                    sess_t0 = time.monotonic()
                    # Engine-side budget = what's left of the caller's timeout
                    # after queueing/boot, minus a margin for the answer poll.
                    engine_deadline_s = max(
                        60.0, timeout_s - (time.monotonic() - t_req) - 10.0
                    )
                    sid = await _ensure_session(inst, body.vibeSessionId)
                    try:
                        attempt_id = await _post_turn(
                            inst, sid, body.query, deadline_s=engine_deadline_s
                        )
                    except _SessionGone:
                        stats["session_recovered"] = True
                        sid = await _ensure_session(inst, None)
                        attempt_id = await _post_turn(
                            inst, sid, body.query, deadline_s=engine_deadline_s
                        )
                    stats["session_ms"] = int((time.monotonic() - sess_t0) * 1000)
                    stats["attempt_id"] = attempt_id
                    # Early meta frame: laicai uses it to stamp attempt_id /
                    # session id onto its status=running placeholder row, so
                    # the admin detail page can tail engine logs/trace while
                    # the run is still in flight (not only after the terminal
                    # frame). Consumers ignore unknown ev names, so this is
                    # backward-compatible.
                    yield _frame({
                        "t": "progress", "ev": "attempt_meta",
                        "data": {"attempt_id": attempt_id, "vibe_session_id": sid},
                    })

                    answered = False
                    q: "asyncio.Queue[dict]" = asyncio.Queue()
                    pump = asyncio.create_task(_pump_events(inst, sid, q))
                    waiter = asyncio.create_task(_wait_answer(inst, sid, attempt_id, timeout_s))
                    try:
                        while not waiter.done():
                            try:
                                ev = await asyncio.wait_for(q.get(), timeout=1.0)
                            except asyncio.TimeoutError:
                                continue
                            stats.setdefault(
                                "first_progress_ms", int((time.monotonic() - t_req) * 1000)
                            )
                            _grab_engine_stats(ev)
                            yield _frame({"t": "progress", **ev})
                        while not q.empty():
                            ev = q.get_nowait()
                            _grab_engine_stats(ev)
                            yield _frame({"t": "progress", **ev})
                        answer = await waiter
                        answered = True
                        inst.last_activity = time.monotonic()
                        stats["outcome"] = "ok"
                        stats["total_ms"] = int((time.monotonic() - t_req) * 1000)
                        yield _frame({
                            "t": "answer",
                            "answer": answer,
                            "vibeSessionId": sid,
                            "stats": {"router": dict(stats), "engine": engine_stats},
                        })
                    finally:
                        pump.cancel()
                        waiter.cancel()
                        if not answered:
                            # Timeout / client gone / internal error: tell the
                            # engine to stop burning tokens on an answer nobody
                            # will receive (incident 2026-08-24: a 504'd attempt
                            # kept grinding and starved the tenant's retry).
                            try:
                                await _vibe(
                                    inst, "POST", f"/sessions/{sid}/cancel",
                                    timeout=10.0,
                                )
                                stats["engine_cancelled"] = True
                                log.info(
                                    "cancelled unfinished attempt (tenant %s, sid %s)",
                                    tk[:8], sid,
                                )
                            except Exception as e:  # noqa: BLE001
                                log.warning("cancel after unanswered ask failed: %s", e)
            finally:
                inst.refcount -= 1
    except HTTPException as e:
        stats["outcome"] = _classify_status(e.status_code)
        stats["error"] = str(e.detail)[:300]
        stats["total_ms"] = int((time.monotonic() - t_req) * 1000)
        yield _frame({
            "t": "error", "status": e.status_code, "detail": str(e.detail),
            "stats": {"router": dict(stats), "engine": engine_stats},
        })
    except Exception as e:  # noqa: BLE001 - surface as an error frame, not a broken stream
        log.exception("ask failed (tenant %s)", tk[:8])
        stats["outcome"] = "exception"
        stats["error"] = f"{type(e).__name__}: {e}"[:300]
        stats["total_ms"] = int((time.monotonic() - t_req) * 1000)
        yield _frame({
            "t": "error", "status": 502,
            "detail": f"router internal error: {type(e).__name__}",
            "stats": {"router": dict(stats), "engine": engine_stats},
        })
    finally:
        stats.setdefault("total_ms", int((time.monotonic() - t_req) * 1000))
        if engine_stats:
            stats["engine_status"] = engine_stats.get("status")
            stats["iterations"] = engine_stats.get("iterations")
        _record_ask(stats)


@app.post("/ask")
async def ask(body: AskBody, authorization: Optional[str] = Header(None)):
    _auth(authorization)
    if body.model and not MODEL_RE.fullmatch(body.model):
        raise HTTPException(400, "invalid model")
    if body.llm is not None:
        l = body.llm
        if l.provider not in BYOK_PROVIDERS:
            raise HTTPException(400, "invalid llm.provider")
        if not MODEL_RE.fullmatch(l.model):
            raise HTTPException(400, "invalid llm.model")
        if not re.fullmatch(r"https?://[^\s\"']{1,500}", l.baseUrl):
            raise HTTPException(400, "invalid llm.baseUrl")
        if not (0 < len(l.apiKey) <= 500) or any(ord(c) < 32 or ord(c) == 127 for c in l.apiKey):
            raise HTTPException(400, "invalid llm.apiKey")
    timeout_s = body.timeoutS or DEFAULT_ASK_TIMEOUT_S
    return StreamingResponse(_ask_stream(body, timeout_s), media_type="application/x-ndjson")


@app.post("/forget")
async def forget(body: dict, authorization: Optional[str] = Header(None)):
    _auth(authorization)
    uid = body.get("uid")
    if not uid:
        raise HTTPException(400, "uid required")
    tk = tenant_key(uid)
    async with pool_mutex:
        inst = pool.pop(tk, None)
    sandbox_id = inst.sandbox_id if inst else (state.get(tk) or {}).get("sandbox_id")
    if sandbox_id:
        await sbx_delete(sandbox_id)
    # Tenant data now outlives the sandbox, so forgetting must remove it here too.
    shutil.rmtree(DATA_ROOT / tk, ignore_errors=True)
    state.pop(tk, None)
    _save_state()
    return {"ok": True}


# ── Read-only tenant observability (laicai admin deep-run detail page) ───────
# Serves the tenant's engine.jsonl / trace.jsonl straight off the host
# bind-mount, so operators can inspect a run in the browser instead of SSH.
# Bearer-gated like everything else; ids are strictly validated so a caller
# can never traverse outside the tenant's data dir.

_OBS_ID_RE = re.compile(r"[A-Za-z0-9_-]{4,64}")
_OBS_TAIL_BYTES = 4_000_000


def _obs_tail_lines(path: Path, max_bytes: int = _OBS_TAIL_BYTES) -> list[str]:
    with path.open("rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - max_bytes))
        data = f.read().decode("utf-8", "replace")
    lines = data.splitlines()
    if size > max_bytes and lines:
        lines = lines[1:]  # drop the partial first line
    return lines


def _obs_clip(entry: dict, max_chars: int = 600) -> dict:
    for k, v in list(entry.items()):
        if isinstance(v, str) and len(v) > max_chars:
            entry[k] = v[:max_chars] + "…"
    return entry


@app.get("/obs/engine-log")
async def obs_engine_log(
    uid: str,
    attempt_id: Optional[str] = None,
    limit: int = 500,
    authorization: Optional[str] = Header(None),
):
    _auth(authorization)
    if attempt_id and not _OBS_ID_RE.fullmatch(attempt_id):
        raise HTTPException(400, "invalid attempt_id")
    limit = max(1, min(limit, 2000))
    path = DATA_ROOT / tenant_key(uid) / "logs" / "engine.jsonl"
    if not path.exists():
        return {"lines": [], "truncated": False}
    raw = await asyncio.to_thread(_obs_tail_lines, path)
    out = []
    for line in raw:
        if attempt_id and attempt_id not in line:
            continue
        try:
            out.append(_obs_clip(json.loads(line)))
        except Exception:
            continue
    return {"lines": out[-limit:], "truncated": len(out) > limit}


@app.get("/obs/ask-log")
async def obs_ask_log(
    uid: str,
    attempt_id: Optional[str] = None,
    limit: int = 50,
    authorization: Optional[str] = Header(None),
):
    """This tenant's rows from the router ask log (segment timings/outcomes)."""
    _auth(authorization)
    if attempt_id and not _OBS_ID_RE.fullmatch(attempt_id):
        raise HTTPException(400, "invalid attempt_id")
    limit = max(1, min(limit, 200))
    tk8 = tenant_key(uid)[:8]
    if not ASK_LOG.exists():
        return {"lines": [], "truncated": False}
    raw = await asyncio.to_thread(_obs_tail_lines, ASK_LOG)
    out = []
    for line in raw:
        try:
            e = json.loads(line)
        except Exception:
            continue
        if e.get("tk8") != tk8:
            continue
        if attempt_id and e.get("attempt_id") != attempt_id:
            continue
        out.append(_obs_clip(e))
    return {"lines": out[-limit:], "truncated": len(out) > limit}


@app.get("/obs/trace")
async def obs_trace(
    uid: str,
    session_id: str,
    limit: int = 800,
    authorization: Optional[str] = Header(None),
):
    _auth(authorization)
    if not _OBS_ID_RE.fullmatch(session_id):
        raise HTTPException(400, "invalid session_id")
    limit = max(1, min(limit, 2000))
    path = DATA_ROOT / tenant_key(uid) / "sessions" / session_id / "trace.jsonl"
    if not path.exists():
        return {"entries": [], "truncated": False}
    raw = await asyncio.to_thread(_obs_tail_lines, path)
    out = []
    for line in raw:
        try:
            out.append(_obs_clip(json.loads(line)))
        except Exception:
            continue
    return {"entries": out[-limit:], "truncated": len(out) > limit}


@app.get("/obs/swarm-events")
async def obs_swarm_events(
    uid: str,
    run_id: str,
    limit: int = 800,
    authorization: Optional[str] = Header(None),
):
    """Tail a swarm run's internal event log (worker tool calls, retries,
    heartbeats) off the tenant bind-mount — the run_swarm counterpart of
    /obs/trace, so the laicai detail page can render per-worker execution
    without SSH. run_id comes from attempt_stats.swarm_runs[].run_id."""
    _auth(authorization)
    if not _OBS_ID_RE.fullmatch(run_id):
        raise HTTPException(400, "invalid run_id")
    limit = max(1, min(limit, 2000))
    path = DATA_ROOT / tenant_key(uid) / ".swarm" / "runs" / run_id / "events.jsonl"
    if not path.exists():
        return {"entries": [], "truncated": False}
    raw = await asyncio.to_thread(_obs_tail_lines, path)
    out = []
    for line in raw:
        try:
            out.append(_obs_clip(json.loads(line)))
        except Exception:
            continue
    return {"entries": out[-limit:], "truncated": len(out) > limit}


@app.get("/healthz")
async def healthz(authorization: Optional[str] = Header(None)):
    _auth(authorization)  # docs always said Bearer; the check was simply missing
    ok_ms = list(recent_ask_ms)
    return {
        "instances": len(pool),
        "running": sum(1 for i in pool.values() if not i.paused),
        "active": MAX_CONCURRENT_ACTIVE - active_sem._value,  # noqa: SLF001
        "max_running": MAX_RUNNING,
        "asks": {
            **{k: v for k, v in metrics.items() if k != "started_at"},
            "uptime_s": round(time.time() - metrics["started_at"]),
            "p50_ms": _percentile(ok_ms, 0.50),
            "p95_ms": _percentile(ok_ms, 0.95),
            "window": len(ok_ms),
        },
        "tenants": [
            {
                "tk8": i.tk[:8],
                "sandbox": i.sandbox_id[:12],
                "paused": i.paused,
                "refcount": i.refcount,
                "idle_s": round(time.monotonic() - i.last_activity),
            }
            for i in pool.values()
        ],
    }


# ── Background reaper: pause idle sandboxes ──────────────────────────────────
async def _reaper():
    while True:
        await asyncio.sleep(60)
        now = time.monotonic()
        victims = [
            i for i in pool.values()
            if i.refcount == 0 and not i.paused and (now - i.last_activity) > IDLE_TTL_S
        ]
        for v in victims:
            log.info("pausing idle tenant %s (idle %ds)", v.tk[:8], round(now - v.last_activity))
            await sbx_pause(v.sandbox_id)
            v.paused = True


@app.on_event("startup")
async def _startup():
    global state
    state = _load_state()
    log.info("loaded %d tenant mappings from %s", len(state), STATE_FILE)
    asyncio.create_task(_reaper())


@app.on_event("shutdown")
async def _shutdown():
    # Sandboxes survive a router restart by design; just close clients.
    await api.aclose()
    await http.aclose()
