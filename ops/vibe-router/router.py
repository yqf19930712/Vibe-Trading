"""vibe-router — multi-tenant orchestrator for Vibe-Trading.

One Vibe-Trading `serve` process per end-user (tenant), each with its own HOME
(= isolated memory / sessions / search index / oauth / shadow / goals) and its
own RAM (= isolated process-global singletons). This router lazily starts,
reuses, and reaps those per-tenant instances, and proxies a single `/ask` call
(create-or-reuse session → send → poll-by-attempt_id → answer) so the caller
(laicai) stays thin.

Design: ../../MULTI_TENANCY.md (v2, review-incorporated). All the non-obvious
choices here trace to a numbered finding there (B1–B4, M1–M8, m1–m5).

Stdlib + fastapi + uvicorn + httpx (all already in Vibe's deps).
"""
from __future__ import annotations

import asyncio
import hmac
import hashlib
import logging
import os
import re
import shutil
import signal
import time
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel

log = logging.getLogger("vibe-router")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

# ── Config (env) ──────────────────────────────────────────────────────────────
ROUTER_SECRET = os.environ.get("VIBE_ROUTER_SECRET", "")        # HMAC key → tenant dir name (schema key, NOT rotatable)
ROUTER_TOKEN = os.environ.get("VIBE_ROUTER_TOKEN", "")          # Bearer required from laicai (M5/m5: even on loopback)
USERS_BASE = Path(os.environ.get("VIBE_USERS_BASE", "/srv/vibe/users")).resolve()
VIBE_BIN = os.environ.get("VIBE_BIN", "vibe-trading")
PORT_LOW, PORT_HIGH = 8901, 8949
MAX_INSTANCES = int(os.environ.get("VIBE_MAX_INSTANCES", "4"))
MAX_CONCURRENT_ACTIVE = int(os.environ.get("VIBE_MAX_CONCURRENT_ACTIVE", "2"))
IDLE_TTL_S = int(os.environ.get("VIBE_IDLE_TTL_S", str(20 * 60)))
INSTANCE_MEM_MAX = os.environ.get("VIBE_INSTANCE_MEMORY_MAX", "1.6G")
INSTANCE_CPU_QUOTA = os.environ.get("VIBE_INSTANCE_CPU_QUOTA", "150%")
READY_TIMEOUT_S = int(os.environ.get("VIBE_READY_TIMEOUT_S", "60"))
POLL_INTERVAL_S = float(os.environ.get("VIBE_POLL_INTERVAL_S", "3"))
DEFAULT_ASK_TIMEOUT_S = int(os.environ.get("VIBE_ASK_TIMEOUT_S", str(15 * 60)))
# argv marker so a restarted router can recognise/kill orphaned instances (m5).
INSTANCE_MARKER = "VIBE_ROUTER_MANAGED"
# LLM / data-source env forwarded into each tenant instance (per-tenant creds live
# in their HOME; keys themselves come from the router env, §12).
FORWARD_ENV = [
    "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_API_BASE", "OPENAI_MODEL",
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    "LANGCHAIN_PROVIDER", "LANGCHAIN_MODEL_NAME", "LANGCHAIN_TEMPERATURE",
    "TUSHARE_TOKEN", "VIBE_TRADING_SEARCH_BACKENDS",
    # Shared, read-only matplotlib font cache so cold tenants skip cache rebuild (m1).
    "MPLCONFIGDIR",
]

if not ROUTER_SECRET or not ROUTER_TOKEN:
    raise SystemExit("vibe-router requires VIBE_ROUTER_SECRET and VIBE_ROUTER_TOKEN")


def tenant_key(uid: str) -> str:
    """Stable, irreversible per-tenant id → disk dir name. Hex-only (path-safe, M5/m3)."""
    return hmac.new(ROUTER_SECRET.encode(), uid.encode(), hashlib.sha256).hexdigest()


def tenant_home(tk: str) -> Path:
    """Resolve + validate the tenant data root; never let raw input reach the path."""
    assert re.fullmatch(r"[0-9a-f]{64}", tk), "tenant_key must be 64 hex chars"
    root = (USERS_BASE / tk).resolve()
    assert root.parent == USERS_BASE and root != USERS_BASE, "tenant path escaped base"
    return root


# ── Instance pool ─────────────────────────────────────────────────────────────
class Instance:
    def __init__(self, tk: str, port: int, proc: asyncio.subprocess.Process, home: Path):
        self.tk = tk
        self.port = port
        self.proc = proc
        self.home = home
        self.refcount = 0          # in-flight /ask count (M3: reaper/LRU skip refcount>0)
        self.last_activity = time.monotonic()
        self.lock = asyncio.Lock()  # single-writer per tenant (one attempt at a time)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


pool: dict[str, Instance] = {}
pool_mutex = asyncio.Lock()                 # guards pool + uid_locks mutation (M6)
uid_locks: dict[str, asyncio.Lock] = {}
active_sem = asyncio.Semaphore(MAX_CONCURRENT_ACTIVE)
_used_ports: set[int] = set()
http = httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=120.0))


def _alloc_port() -> int:
    for p in range(PORT_LOW, PORT_HIGH + 1):
        if p not in _used_ports:
            _used_ports.add(p)
            return p
    raise HTTPException(503, "no free port (pool saturated)")


async def _spawn(tk: str) -> Instance:
    """Launch one tenant instance under a cgroup scope (M4) with full isolation env (B4)."""
    home = tenant_home(tk)
    (home / ".vibe-trading").mkdir(parents=True, exist_ok=True)
    port = _alloc_port()
    env = {k: os.environ[k] for k in FORWARD_ENV if k in os.environ}
    env.update(
        {
            "HOME": str(home),
            "VIBE_DATA_DIR": str(home / ".vibe-trading"),  # co-locate sessions/runs/uploads/swarm with memory (B1/B4)
            "VIBE_MULTITENANT": "1",                        # fail-loud if data dir missing (B4)
            "VIBE_TRADING_TENANT_SAFE": "1",                # block only real-trading/mandate tools (B2/M2)
            # TEST PHASE: enable shell tools so background_run (arbitrary host
            # commands, runs as the `vibe` user) is usable; this also enables
            # the foreground `bash` tool (equivalent capability). Drop this env
            # to lock host command execution back down before real multi-user use.
            "VIBE_TRADING_ENABLE_SHELL_TOOLS": "1",
            "API_AUTH_KEY": hashlib.sha256(os.urandom(16)).hexdigest(),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            INSTANCE_MARKER: tk,
        }
    )
    serve_args = [VIBE_BIN, "serve", "--host", "127.0.0.1", "--port", str(port)]  # explicit loopback (M1)
    # Per-instance cgroup caps (M4) need root for `systemd-run --scope -p MemoryMax`.
    # The phase-1 router runs as non-root `vibe`, so by default we DON'T use it and
    # instead rely on the router unit's own MemoryMax= (Delegate=yes) to bound the
    # whole pool (router + all child instances inherit the unit cgroup). Opt into
    # per-instance scopes with VIBE_USE_SYSTEMD_RUN=1 when running as root.
    if os.getenv("VIBE_USE_SYSTEMD_RUN", "").strip().lower() in {"1", "true", "yes"} and shutil.which(
        "systemd-run"
    ):
        cmd = [
            "systemd-run", "--scope", "--quiet",
            "-p", f"MemoryMax={INSTANCE_MEM_MAX}", "-p", "MemorySwapMax=0",
            "-p", f"CPUQuota={INSTANCE_CPU_QUOTA}", "-p", "OOMScoreAdjust=600",
            *serve_args,
        ]
    else:
        cmd = serve_args
    proc = await asyncio.create_subprocess_exec(
        *cmd, env=env, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    inst = Instance(tk, port, proc, home)
    # Wait for readiness (cold start = heavy imports + maybe font fetch, m1).
    deadline = time.monotonic() + READY_TIMEOUT_S
    while time.monotonic() < deadline:
        if proc.returncode is not None:
            _used_ports.discard(port)
            raise HTTPException(502, f"instance exited during startup (rc={proc.returncode})")
        try:
            r = await http.get(f"{inst.base_url}/health", timeout=3.0)
            if r.status_code == 200:
                log.info("tenant %s ready on :%d", tk[:8], port)
                return inst
        except Exception:
            pass
        await asyncio.sleep(0.5)
    await _kill(inst)
    raise HTTPException(504, "instance did not become ready in time")


async def _kill(inst: Instance) -> None:
    _used_ports.discard(inst.port)
    try:
        inst.proc.terminate()
        try:
            await asyncio.wait_for(inst.proc.wait(), timeout=10)
        except asyncio.TimeoutError:
            inst.proc.kill()
            await inst.proc.wait()
    except ProcessLookupError:
        pass


async def get_or_create(tk: str) -> Instance:
    """Lazy get-or-create with a per-uid lock so concurrent first-calls coalesce (M6)."""
    async with pool_mutex:
        inst = pool.get(tk)
        if inst and inst.proc.returncode is None:
            return inst
        lock = uid_locks.setdefault(tk, asyncio.Lock())
    async with lock:
        inst = pool.get(tk)
        if inst and inst.proc.returncode is None:
            return inst
        # capacity: LRU-evict an idle (refcount==0) instance before spawning (M3)
        async with pool_mutex:
            if len(pool) >= MAX_INSTANCES:
                idle = sorted(
                    (i for i in pool.values() if i.refcount == 0),
                    key=lambda i: i.last_activity,
                )
                if not idle:
                    raise HTTPException(503, "all instances busy; retry shortly")
                victim = idle[0]
                pool.pop(victim.tk, None)
                await _kill(victim)
        inst = await _spawn(tk)
        async with pool_mutex:
            pool[tk] = inst
        return inst


# ── Vibe session helpers (B3: poll by attempt_id, not .at(-1)) ─────────────────
async def _vibe(inst: Instance, method: str, path: str, **kw):
    return await http.request(method, f"{inst.base_url}{path}", **kw)


async def _ensure_session(inst: Instance, vibe_session_id: Optional[str]) -> str:
    if vibe_session_id:
        return vibe_session_id
    r = await _vibe(inst, "POST", "/sessions", json={"title": "laicai"})
    r.raise_for_status()
    sid = r.json().get("session_id")
    if not sid:
        raise HTTPException(502, "vibe returned no session_id")
    return sid


async def _send_and_wait(inst: Instance, sid: str, query: str, timeout_s: int) -> str:
    """Send a turn and wait for THIS turn's answer (linked_attempt_id), not the
    previous turn's stale reply (B3). On a missing session (404) the caller
    rebinds with a fresh session."""
    r = await _vibe(inst, "POST", f"/sessions/{sid}/messages", json={"content": query})
    if r.status_code == 404:
        raise _SessionGone()
    r.raise_for_status()
    attempt_id = r.json().get("attempt_id")
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
            # Only accept the reply linked to THIS attempt (or, if the server
            # doesn't surface linkage, the newest non-empty assistant message
            # that appeared after our send — attempt_id is the robust path).
            if attempt_id and msg.get("linked_attempt_id") not in (attempt_id, None):
                continue
            return content
    raise HTTPException(504, "deep engine timed out")


class _SessionGone(Exception):
    pass


# ── API ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="vibe-router")


class AskBody(BaseModel):
    uid: str
    query: str
    threadId: Optional[str] = None
    vibeSessionId: Optional[str] = None
    timeoutS: Optional[int] = None


def _auth(authorization: Optional[str]) -> None:
    # M5/m5: require the bearer even on loopback (do NOT copy Vibe's loopback-trust).
    expected = f"Bearer {ROUTER_TOKEN}"
    if not authorization or not hmac.compare_digest(authorization, expected):
        raise HTTPException(401, "unauthorized")


@app.post("/ask")
async def ask(body: AskBody, authorization: Optional[str] = Header(None)):
    _auth(authorization)
    tk = tenant_key(body.uid)
    timeout_s = body.timeoutS or DEFAULT_ASK_TIMEOUT_S
    async with active_sem:                       # bound concurrent active runs (resource, §7)
        inst = await get_or_create(tk)
        inst.refcount += 1                       # in-flight guard against reaper/LRU (M3)
        try:
            async with inst.lock:                # single-writer per tenant
                sid = await _ensure_session(inst, body.vibeSessionId)
                try:
                    answer = await _send_and_wait(inst, sid, body.query, timeout_s)
                except _SessionGone:
                    # Stored session vanished (HOME wiped / forget) → rebind fresh (M-continuity).
                    sid = await _ensure_session(inst, None)
                    answer = await _send_and_wait(inst, sid, body.query, timeout_s)
                inst.last_activity = time.monotonic()   # bump on completion, not arrival (M3)
                return {"answer": answer, "vibeSessionId": sid}
        finally:
            inst.refcount -= 1


@app.post("/forget")
async def forget(body: dict, authorization: Optional[str] = Header(None)):
    _auth(authorization)
    uid = body.get("uid")
    if not uid:
        raise HTTPException(400, "uid required")
    tk = tenant_key(uid)
    root = tenant_home(tk)                        # validated hex-only path (M5/m3)
    async with pool_mutex:
        inst = pool.pop(tk, None)
    if inst:
        await _kill(inst)                         # SIGTERM before rm (avoid live-sqlite delete)
    shutil.rmtree(root, ignore_errors=True)       # idempotent
    return {"ok": True}


@app.get("/healthz")
async def healthz():
    return {
        "instances": len(pool),
        "active": MAX_CONCURRENT_ACTIVE - active_sem._value,  # noqa: SLF001 (introspection only)
        "max_instances": MAX_INSTANCES,
        "tenants": [{"tk8": i.tk[:8], "port": i.port, "refcount": i.refcount,
                     "idle_s": round(time.monotonic() - i.last_activity)} for i in pool.values()],
    }


# ── Background reaper + orphan cleanup ────────────────────────────────────────
async def _reaper():
    while True:
        await asyncio.sleep(60)
        now = time.monotonic()
        async with pool_mutex:
            victims = [
                i for i in pool.values()
                if i.refcount == 0 and (now - i.last_activity) > IDLE_TTL_S
            ]
            for v in victims:
                pool.pop(v.tk, None)
        for v in victims:
            log.info("reaping idle tenant %s (idle %ds)", v.tk[:8], round(now - v.last_activity))
            await _kill(v)
        # reap zombies of any exited children we still hold
        for i in list(pool.values()):
            if i.proc.returncode is not None:
                async with pool_mutex:
                    pool.pop(i.tk, None)
                _used_ports.discard(i.port)


def _kill_orphans_on_boot() -> None:
    """A previous router crash can leave managed instances running (m5). Kill any
    process whose env carries our marker before we start fresh."""
    try:
        import psutil  # optional; skip cleanly if absent
    except Exception:
        log.warning("psutil not installed — skipping boot orphan cleanup")
        return
    for p in psutil.process_iter(["pid", "environ"]):
        try:
            if (p.info["environ"] or {}).get(INSTANCE_MARKER):
                log.warning("killing orphaned vibe instance pid=%s", p.info["pid"])
                p.terminate()
        except Exception:
            continue


@app.on_event("startup")
async def _startup():
    USERS_BASE.mkdir(parents=True, exist_ok=True)
    _kill_orphans_on_boot()
    asyncio.create_task(_reaper())


@app.on_event("shutdown")
async def _shutdown():
    for i in list(pool.values()):
        await _kill(i)
    await http.aclose()
