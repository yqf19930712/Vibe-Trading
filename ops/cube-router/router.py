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
import hashlib
import hmac
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

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
MAX_RUNNING = int(os.environ.get("VIBE_MAX_INSTANCES", "3"))          # concurrent RUNNING sandboxes
MAX_CONCURRENT_ACTIVE = int(os.environ.get("VIBE_MAX_CONCURRENT_ACTIVE", "2"))
IDLE_TTL_S = int(os.environ.get("VIBE_IDLE_TTL_S", str(20 * 60)))     # pause after idle
READY_TIMEOUT_S = int(os.environ.get("VIBE_READY_TIMEOUT_S", "180"))  # create+boot budget
POLL_INTERVAL_S = float(os.environ.get("VIBE_POLL_INTERVAL_S", "3"))
DEFAULT_ASK_TIMEOUT_S = int(os.environ.get("VIBE_ASK_TIMEOUT_S", str(15 * 60)))
# LLM / data-source env forwarded into each tenant engine (via launcher /boot).
FORWARD_ENV = [
    "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_API_BASE", "OPENAI_MODEL",
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    "LANGCHAIN_PROVIDER", "LANGCHAIN_MODEL_NAME", "LANGCHAIN_TEMPERATURE",
    "TUSHARE_TOKEN", "VIBE_TRADING_SEARCH_BACKENDS",
]

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


async def sbx_create() -> str:
    r = await api.post("/sandboxes", json={"templateID": TEMPLATE_ID})
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
    try:
        await api.delete(f"/sandboxes/{sandbox_id}")
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
    _save_state()


async def _ensure_ready(inst: Instance, fp: Optional[str], env: dict, api_key: str) -> None:
    """Make the sandbox reachable and the engine running with the wanted LLM env."""
    h = await _launcher_health(inst)
    if h is None:
        # Paused (or proxy lost it) — explicit resume, then retry.
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
    await _boot_engine(inst, fp, env, api_key)


async def get_or_create(
    tk: str, model: Optional[str] = None, llm: Optional["LlmOverride"] = None
) -> Instance:
    fp = llm_fingerprint(model, llm)
    async with pool_mutex:
        lock = uid_locks.setdefault(tk, asyncio.Lock())
    async with lock:
        inst = pool.get(tk)
        if inst is None:
            st = state.get(tk)
            if st and st.get("sandbox_id"):
                info = await sbx_info(st["sandbox_id"])
                if info is not None:
                    inst = Instance(tk, st["sandbox_id"], st.get("llm_fp"), st.get("api_key"))
                else:
                    state.pop(tk, None)
                    _save_state()
        if inst is None:
            await _evict_for_capacity()
            sandbox_id = await sbx_create()
            inst = Instance(tk, sandbox_id, None)
            log.info("tenant %s -> new sandbox %s", tk[:8], sandbox_id[:12])
        env, api_key = engine_env(model, llm)
        await _ensure_ready(inst, fp, env, api_key)
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


async def _post_turn(inst: Instance, sid: str, query: str) -> Optional[str]:
    r = await _vibe(inst, "POST", f"/sessions/{sid}/messages", json={"content": query})
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


async def _ask_stream(body: AskBody, timeout_s: int):
    tk = tenant_key(body.uid)
    async with active_sem:
        inst = await get_or_create(tk, body.model, body.llm)
        inst.refcount += 1
        try:
            async with inst.lock:
                sid = await _ensure_session(inst, body.vibeSessionId)
                try:
                    attempt_id = await _post_turn(inst, sid, body.query)
                except _SessionGone:
                    sid = await _ensure_session(inst, None)
                    attempt_id = await _post_turn(inst, sid, body.query)

                q: "asyncio.Queue[dict]" = asyncio.Queue()
                pump = asyncio.create_task(_pump_events(inst, sid, q))
                waiter = asyncio.create_task(_wait_answer(inst, sid, attempt_id, timeout_s))
                try:
                    while not waiter.done():
                        try:
                            ev = await asyncio.wait_for(q.get(), timeout=1.0)
                        except asyncio.TimeoutError:
                            continue
                        yield _frame({"t": "progress", **ev})
                    while not q.empty():
                        yield _frame({"t": "progress", **q.get_nowait()})
                    answer = await waiter
                    inst.last_activity = time.monotonic()
                    yield _frame({"t": "answer", "answer": answer, "vibeSessionId": sid})
                except HTTPException as e:
                    yield _frame({"t": "error", "status": e.status_code, "detail": str(e.detail)})
                finally:
                    pump.cancel()
                    waiter.cancel()
        finally:
            inst.refcount -= 1


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
    state.pop(tk, None)
    _save_state()
    return {"ok": True}


@app.get("/healthz")
async def healthz():
    return {
        "instances": len(pool),
        "running": sum(1 for i in pool.values() if not i.paused),
        "active": MAX_CONCURRENT_ACTIVE - active_sem._value,  # noqa: SLF001
        "max_running": MAX_RUNNING,
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
