"""cube-router — multi-tenant orchestrator for Vibe-Trading on CubeSandbox.

Public API (Bearer auth on every endpoint): `/ask` (NDJSON stream),
`/forget`, `/sessions/delete`, `/healthz`, `/tenants/usage`, the read-only
`/obs/*` tail endpoints and `/memory` + `/memory/delete`. Each tenant gets a
KVM MicroVM sandbox created from a CubeSandbox template (image: python +
vibe-trading + in-guest launcher).

Per-tenant layout inside the sandbox (template default):
  - launcher on :8898 — `GET /health`, `POST /boot {env}`, `POST /stop`
  - engine   on :8899 — `vibe-trading serve`, spawned by the launcher with the
    per-tenant env the router sends (LLM creds / BYOK / tenant flags)
  - HOME=/home/vibe, VIBE_DATA_DIR=/home/vibe/.vibe-trading — a host-mount of
    DATA_ROOT/<tenant_key> (default /data/shared/vibe/<tk>), so sessions /
    memory / trace / uploads live on the host and survive pause/resume AND
    sandbox rebuilds (template switch). The sandbox's writable layer holds
    nothing tenant-specific.

Lifecycle:
  first ask       → create sandbox (E2B-compatible CubeAPI) + POST /boot
  kill idle       → pause sandbox (disk + memory state kept; resume is fast)
  LLM switch      → POST /boot with new env (engine restart inside the guest;
                    no sandbox respawn, sessions untouched)
  template switch → delete sandbox, recreate from the new template on the
                    next /ask (data dir is host-mounted: lossless)
  forget          → tombstone the tenant, delete sandbox + rmtree the host
                    data dir + drop the sandbox mapping (500 {ok:false} if
                    either fails so laicai retries)
  session delete  → POST /sessions/delete: DELETE on the engine when the
                    sandbox is up, else remove the host session dir

Sandbox data-plane access goes through cube-proxy's E2B-style host routing:
  http://<port>-<sandbox_id>.<SANDBOX_DOMAIN>/   (host DNS resolves *.cube.app
  to the node; plain HTTP on the proxy's HTTP port).

State (tenant_key → sandbox / template / engine key / fingerprint, or a
/forget tombstone) lives in a JSON file so a router restart re-attaches to
existing sandboxes instead of leaking them.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import fcntl
import hashlib
import hmac
import json
import logging
import os
import re
import shutil
import stat as stat_mod
import subprocess
import time
import urllib.parse
from collections import deque
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
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
# An /ask beyond MAX_CONCURRENT_ACTIVE queues for a slot, at most this long;
# past it the ask ends with the same 503 busy frame as a full RUNNING cap.
ACTIVE_QUEUE_WAIT_S = float(os.environ.get("VIBE_ACTIVE_QUEUE_WAIT_S", "120"))
IDLE_TTL_S = int(os.environ.get("VIBE_IDLE_TTL_S", str(20 * 60)))     # pause after idle
READY_TIMEOUT_S = int(os.environ.get("VIBE_READY_TIMEOUT_S", "180"))  # create+boot budget
POLL_INTERVAL_S = float(os.environ.get("VIBE_POLL_INTERVAL_S", "3"))
# Answer-poll tolerance: a failing poll (transport error, non-JSON body,
# non-200 from cube-proxy or the engine) is retried until the failures have
# lasted POLL_FAIL_MAX_S seconds or POLL_FAIL_MAX_CONSECUTIVE polls in a row,
# whichever comes first. A launcher that reports the engine process stopped
# ends the wait at once — that attempt is gone.
POLL_FAIL_MAX_CONSECUTIVE = int(os.environ.get("VIBE_POLL_FAIL_MAX", "10"))
POLL_FAIL_MAX_S = float(os.environ.get("VIBE_POLL_FAIL_MAX_S", "120"))
# The message list is a cheap read; a poll that hangs longer than this is a
# transport problem, not a slow engine.
POLL_HTTP_TIMEOUT = httpx.Timeout(10.0, read=30.0)
DEFAULT_ASK_TIMEOUT_S = int(os.environ.get("VIBE_ASK_TIMEOUT_S", str(15 * 60)))
# Swarm committees legitimately run tens of minutes to hours (multi-layer DAG ×
# multi-iteration workers).
SWARM_ASK_TIMEOUT_S = int(os.environ.get("VIBE_SWARM_ASK_TIMEOUT_S", str(2 * 60 * 60)))
# THE single place a caller's budget tier is decided. Callers declare a
# structured `intent` and the number is derived here — writing 7200 out in
# several places (laicai chat-tools, laicai warlab-engine, the SWARM_TIMEOUT
# env below, the engine's swarm_tool) lets them drift apart. An explicit
# `timeoutS` still
# wins so laicai can be rolled back on its own without touching the router.
BUDGET_BY_INTENT = {
    "standard": DEFAULT_ASK_TIMEOUT_S,
    "deep_team": SWARM_ASK_TIMEOUT_S,
}


def budget_for(intent: Optional[str], explicit: Optional[int]) -> int:
    """Resolve one ask's wall-clock budget. `explicit` (body.timeoutS) wins."""
    if explicit:
        return explicit
    return BUDGET_BY_INTENT.get(intent or "standard", DEFAULT_ASK_TIMEOUT_S)
# /forget leaves a tombstone (``forgotten_at``) in the tenant's state row; for
# this long an /ask for the tenant is refused with 410 instead of recreating
# its data dir and sandbox. Expired tombstones are pruned at startup.
FORGET_TOMBSTONE_S = int(os.environ.get("VIBE_FORGET_TOMBSTONE_S", str(30 * 24 * 3600)))
# /forget waits this long for the tenant lock (held by an in-flight cold
# start of the same tenant). Past it the purge goes ahead: the tombstone is
# already written, and the cold start aborts on it and removes what it made.
FORGET_LOCK_WAIT_S = float(os.environ.get("VIBE_FORGET_LOCK_WAIT_S", "10"))
# Per-ask observability: one JSONL line per /ask (segment timings, outcome,
# attempt_id) so slow/failed asks can be traced without any extra infra.
ASK_LOG = Path(os.environ.get("VIBE_ASK_LOG", "/var/lib/cube-router/ask_log.jsonl"))
ASK_LOG_MAX_BYTES = 20 * 1024 * 1024
# LLM / data-source env forwarded into each tenant engine (via launcher /boot):
# the explicit names below plus every router env var carrying one of the
# FORWARD_ENV_PREFIXES. The engine reads its LLM knobs (thinking mode, output
# cap, usage block, reasoning effort, …) from the LANGCHAIN_* / VIBE_ANTHROPIC_*
# families, so the prefix rule is what lets router.env tune them without a
# router code change; the explicit list carries the credentials and the
# single-name knobs (VIBE_MAX_OUTPUT_TOKENS, VIBE_LENGTH_CONTINUATIONS,
# VIBE_CONTEXT_WINDOW_TOKENS — the model's context window the engine sizes
# its compaction thresholds from, …).
FORWARD_ENV = [
    "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_API_BASE", "OPENAI_MODEL",
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    "VIBE_MAX_OUTPUT_TOKENS", "VIBE_LENGTH_CONTINUATIONS", "VIBE_MEMORY_TTL_DAYS",
    "VIBE_CONTEXT_WINDOW_TOKENS",
    "TUSHARE_TOKEN", "VIBE_TRADING_SEARCH_BACKENDS", "JINA_API_KEY",
    "IFIND_MCP_TOKEN", "TICKFLOW_API_KEY", "TICKFLOW_BASE_URL",
]
FORWARD_ENV_PREFIXES = ("LANGCHAIN_", "VIBE_ANTHROPIC_")
# LangSmith shares the LANGCHAIN_ namespace: with these set, langchain-core in
# the tenant engine would upload every prompt (holdings included) to a
# third-party tracing service, so they never ride the prefix rule — whatever
# router.env contains. Names from langchain-core / langsmith's own env lookups.
FORWARD_ENV_DENY = frozenset({
    "LANGCHAIN_API_KEY", "LANGCHAIN_TRACING_V2", "LANGCHAIN_TRACING",
    "LANGCHAIN_ENDPOINT", "LANGCHAIN_BASE_URL", "LANGCHAIN_PROJECT",
    "LANGCHAIN_SESSION", "LANGCHAIN_HANDLER", "LANGCHAIN_ENV",
    "LANGCHAIN_CUSTOM_HEADERS", "LANGCHAIN_REVISION_ID",
    "LANGCHAIN_HUB_API_URL", "LANGCHAIN_HUB_API_KEY",
})
FORWARD_ENV_DENY_PREFIXES = ("LANGSMITH_",)


def forwarded_env_names(environ: "dict[str, str] | os._Environ[str]" = os.environ) -> list[str]:
    """Names in ``environ`` that engine_env() forwards (explicit list + prefixes − deny list)."""
    return sorted(
        k for k in environ
        if (k in FORWARD_ENV or k.startswith(FORWARD_ENV_PREFIXES))
        and k not in FORWARD_ENV_DENY
        and not k.startswith(FORWARD_ENV_DENY_PREFIXES)
    )

# Router → launcher authentication (opt-in). Each sandbox's launcher token
# is derived from the router secret and the sandbox id (nothing to store,
# survives router restarts). The header is always sent — launchers that
# predate it ignore it. With VIBE_LAUNCHER_AUTH=1 the token also rides in
# the /boot env; a launcher that supports it takes the token only from its
# first /boot (i.e. in sandboxes created while the flag is on) and then
# refuses /boot and /stop without it — the guest's own shell can otherwise
# reach the launcher over loopback. The flag is part of the boot env, so
# switching it off changes every engine fingerprint and each tenant re-boots
# on its next ask, which drops the token again. Off by default: a launcher
# holding a token cannot be /booted by a router build without this code.
LAUNCHER_AUTH = os.environ.get("VIBE_LAUNCHER_AUTH", "0").strip().lower() in {"1", "true", "yes"}


def launcher_token(sandbox_id: str) -> str:
    return hmac.new(
        ROUTER_SECRET.encode(), f"launcher:{sandbox_id}".encode(), hashlib.sha256
    ).hexdigest()


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


class _AccessLogUidRedactor(logging.Filter):
    """Replace ``uid=<userId>`` in uvicorn access-log lines with the tenant's tk8.

    The GET endpoints (/memory, /obs/*) take the raw laicai userId as a query
    parameter, and uvicorn logs the full path with its query string; the raw
    id must not reach journald. tk8 is what every other router log line and
    the ask log use, so correlation still works.
    """

    _UID_RE = re.compile(r"([?&]uid=)([^&\s]*)")

    @classmethod
    def _sub(cls, text: str) -> str:
        return cls._UID_RE.sub(
            lambda m: m.group(1) + "tk8:" + tenant_key(urllib.parse.unquote_plus(m.group(2)))[:8],
            text,
        )

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and any(
            isinstance(a, str) and "uid=" in a for a in record.args
        ):
            record.args = tuple(
                self._sub(a) if isinstance(a, str) else a for a in record.args
            )
        elif isinstance(record.msg, str) and "uid=" in record.msg:
            record.msg = self._sub(record.msg)
        return True


logging.getLogger("uvicorn.access").addFilter(_AccessLogUidRedactor())


MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:\-\[\]]{0,99}")
BYOK_PROVIDERS = {
    "openai": "openai",
    "claude": "openai",
    "gemini": "gemini",
    "deepseek": "deepseek",
    "kimi": "kimi",
    "glm": "glm",
}


# Env names minted afresh on every boot; everything else a tenant engine
# receives is part of its identity.
_FP_VOLATILE_ENV = frozenset({"API_AUTH_KEY"})


def env_digest(env: dict) -> str:
    """Short hash over the names and values of a boot env (volatile keys excluded)."""
    items = sorted((str(k), str(v)) for k, v in env.items() if k not in _FP_VOLATILE_ENV)
    return hashlib.sha256(json.dumps(items, ensure_ascii=False).encode()).hexdigest()[:16]


def llm_fingerprint(
    model: Optional[str], llm: Optional["LlmOverride"], env: Optional[dict] = None,
) -> Optional[str]:
    """Identity of the engine configuration an ask needs.

    ``byok:<sha16>`` / ``builtin:<model>`` / ``default`` names the request's
    LLM choice; with ``env`` (the boot env from :func:`engine_env`) an
    ``|env:<sha16>`` digest of every forwarded name and value is appended, so
    editing router.env (credentials, default model, tier knobs) and
    restarting the router makes every existing tenant engine reboot on its
    next ask. Only hashes are stored, never the values.
    """
    if llm is not None:
        raw = "|".join([llm.provider, llm.model, llm.apiKey, llm.baseUrl])
        base: Optional[str] = "byok:" + hashlib.sha256(raw.encode()).hexdigest()[:16]
    elif model:
        base = f"builtin:{model}"
    else:
        base = None
    if env is None:
        return base
    return f"{base or 'default'}|env:{env_digest(env)}"


def engine_env(model: Optional[str], llm: Optional["LlmOverride"]) -> tuple[dict, str]:
    """Env the launcher passes to `vibe-trading serve` inside the guest.
    Returns (env, api_key): the engine validates `Authorization: Bearer
    <API_AUTH_KEY>` on every non-loopback call, so the router must keep the
    key it minted for the instance."""
    env = {k: os.environ[k] for k in forwarded_env_names()}
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
    # Tenant performance/reliability tier. Router env overrides. The engine
    # defaults (1800s tool timeout) let a run outlive every caller budget;
    # iterations stay at 50 because 25 starves swarm-intent runs whose
    # data-collection phase alone eats ~20 iterations — wall-clock deadlines
    # are the hard stop, not the iteration count.
    for key, default in (
        ("VIBE_MAX_ITERATIONS", "50"),
        ("VIBE_TRADING_DATA_CACHE", "1"),
        ("VIBE_TRADING_TOOL_TIMEOUT_SECONDS", "300"),
        # The engine's own swarm wait budget. Derived from BUDGET_BY_INTENT so
        # the two hours are stated once, not copied. The wait is still clamped
        # to the attempt's remaining budget (cap_timeout), so no inversion —
        # the two hours only materialize when the ask itself was granted a
        # matching budget (intent="deep_team", or an explicit timeoutS).
        ("SWARM_TIMEOUT", str(SWARM_ASK_TIMEOUT_S)),
        # LLM streaming read timeout (httpx). The engine default of 120s is
        # too tight for long-context opus-class calls: a swarm worker's
        # stream can go silent >120s twice in a row (ReadTimeout on the
        # iteration and again on the task retry), failing the whole run.
        # 300s rides out thinking pauses while a
        # genuinely dead upstream still fails within one worker iteration.
        ("TIMEOUT_SECONDS", "300"),
        # ddgs 9.x has no google/bing; "auto" rotates every engine it has.
        ("VIBE_TRADING_SEARCH_BACKENDS", "auto"),
        # Models habitually download files to /tmp then read_document them;
        # the sandbox is hardware-isolated so /tmp is safe to allow, and
        # refusing it only sends the run on a detour.
        ("VIBE_TRADING_ALLOWED_FILE_ROOTS", "/tmp"),
    ):
        env[key] = os.environ.get(key, default)
    # Whitelisted foreign egress: the launcher builds an in-guest SSH tunnel
    # to server B's loopback tinyproxy (domain filter there); web_search,
    # read_url (r.jina.ai) and the yfinance loader then use
    # VIBE_TRADING_EGRESS_PROXY. Key material is consumed by the launcher and
    # never enters the engine process env.
    if LAUNCHER_AUTH:
        # Consumed by the launcher (with the per-sandbox token _boot_engine
        # adds); here so that switching the flag changes the fingerprint.
        env["VIBE_LAUNCHER_AUTH"] = "1"
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


# tk -> {"sandbox_id", "template_id", "llm_fp", "api_key"} for a tenant with a
# sandbox; {"forgotten_at": epoch} (plus the sandbox fields while a sandbox
# delete is still pending) for a tenant purged by /forget.
state: dict = {}


class _TenantForgotten(HTTPException):
    """410: the tenant was purged by /forget; nothing may be recreated for it."""

    frame_code = "tenant_forgotten"

    def __init__(self) -> None:
        super().__init__(410, "tenant forgotten: its engine data was purged; asks are refused")


def _tombstoned(tk: str) -> bool:
    row = state.get(tk)
    ts = row.get("forgotten_at") if isinstance(row, dict) else None
    return isinstance(ts, (int, float)) and time.time() - ts < FORGET_TOMBSTONE_S


def _check_not_forgotten(tk: str) -> None:
    if _tombstoned(tk):
        raise _TenantForgotten()


def _set_tombstone(tk: str) -> None:
    row = state.setdefault(tk, {})
    if not _tombstoned(tk):
        row["forgotten_at"] = time.time()
        _save_state()


def _prune_tombstones() -> int:
    """Drop expired tombstone rows that no longer name a sandbox."""
    now = time.time()
    doomed = [
        tk for tk, row in state.items()
        if isinstance(row, dict) and isinstance(row.get("forgotten_at"), (int, float))
        and now - row["forgotten_at"] >= FORGET_TOMBSTONE_S and not row.get("sandbox_id")
    ]
    for tk in doomed:
        state.pop(tk, None)
    if doomed:
        _save_state()
    return len(doomed)


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
    _check_not_forgotten(tk)  # before tenant_data_dir() recreates the dir
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
    # Half-deleted sandbox: cubelet already reaped the
    # task but the CubeAPI/cubemaster record lingers, answering 500 with
    # "NotFoundAtCubelet". Treat it as gone so callers take the same
    # rebuild path as a clean 404 instead of erroring forever.
    if r.status_code >= 500 and "NotFoundAtCubelet" in r.text:
        log.warning("sandbox %s half-deleted (NotFoundAtCubelet); treating as gone",
                    sandbox_id[:12])
        return None
    r.raise_for_status()
    return r.json()


async def sbx_resume(sandbox_id: str) -> bool:
    r = await api.post(f"/sandboxes/{sandbox_id}/resume", json={})
    return r.status_code in (200, 201, 204, 409)  # 409 = already running


async def sbx_pause(sandbox_id: str) -> bool:
    """Pause a sandbox. Returns whether it is no longer running.

    CubeAPI answers a refused pause with a 5xx that httpx does not raise on;
    the caller must not count such a sandbox as paused (it still holds its
    memory against VIBE_MAX_INSTANCES). 409 = already paused, 404 = gone.
    """
    try:
        r = await api.post(f"/sandboxes/{sandbox_id}/pause")
    except Exception as e:  # noqa: BLE001
        log.warning("pause %s failed: %s", sandbox_id[:12], e)
        return False
    if r.status_code in (200, 201, 202, 204, 404, 409):
        return True
    log.warning("pause %s -> %s %s", sandbox_id[:12], r.status_code, r.text[:200])
    return False


async def sbx_delete(sandbox_id: str) -> bool:
    """Delete a sandbox, resuming it first if needed. Returns success.

    CubeAPI refuses to delete a paused sandbox ("sandbox not in normal state")
    and answers 500 — which httpx does not raise on, so without the status check
    below the failure is swallowed and the sandbox leaks forever, holding disk
    and a slot against VIBE_MAX_INSTANCES. Callers that must know (``/forget``)
    read the bool; the self-heal paths ignore it as before.
    """
    try:
        await sbx_resume(sandbox_id)
    except Exception as e:
        log.warning("resume-before-delete %s failed: %s", sandbox_id[:12], e)
    try:
        r = await api.delete(f"/sandboxes/{sandbox_id}")
        if r.status_code not in (200, 202, 204, 404):
            log.warning("delete %s -> %s %s", sandbox_id[:12], r.status_code, r.text[:200])
            return False
        return True
    except Exception as e:
        log.warning("delete %s failed: %s", sandbox_id[:12], e)
        return False


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
        # Sandbox being created / resumed / booted for an ask: already counts
        # as RUNNING for the capacity cap, never a pause victim, not yet
        # usable by /sessions/delete.
        self.booting = False

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
# Serialises "evict until there is room, then take the slot" so concurrent
# cold starts / resumes see each other's reservations and the RUNNING count
# never exceeds MAX_RUNNING.
capacity_lock = asyncio.Lock()

# Strong references for fire-and-forget tasks (engine cancel, reaper, sweep):
# the event loop only keeps weak ones.
_bg_tasks: set["asyncio.Task[Any]"] = set()


def _spawn(coro: Any) -> "asyncio.Task[Any]":
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    return task


class _Busy(HTTPException):
    """503 busy with a machine-readable reason (``busy_reason``)."""

    frame_code = "busy"

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(503, detail)
        self.busy_reason = reason


@contextlib.asynccontextmanager
async def _active_slot(wait_s: float, stats: dict):
    """Hold one of the MAX_CONCURRENT_ACTIVE processing slots, queueing at most
    ``wait_s``; the wait (granted or not) is recorded as ``queue_wait_ms``."""
    t0 = time.monotonic()
    try:
        await asyncio.wait_for(active_sem.acquire(), timeout=max(0.0, wait_s))
    except asyncio.TimeoutError:
        raise _Busy(
            "active_queue_full",
            "deep engine busy: every processing slot is taken; retry shortly",
        ) from None
    finally:
        stats["queue_wait_ms"] = int((time.monotonic() - t0) * 1000)
    try:
        yield
    finally:
        active_sem.release()


async def _launcher_health(inst: Instance) -> Optional[dict]:
    try:
        r = await http.get(f"{inst.launcher_url}/health", timeout=8.0)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


# llm_fp values that never equal a real fingerprint:
#   boot-pending:<fp> — a /boot for <fp> was sent but its 200 never arrived
#                       (client gone, transport error, launcher boot timeout);
#                       the engine may be up with the new env and key, or not.
#   stale             — the engine rejected the router's key (it was restarted
#                       outside the router); reboot before the next use.
_BOOT_PENDING = "boot-pending:"
_FP_STALE = "stale"


def _pending_fp(fp: Optional[str]) -> str:
    return f"{_BOOT_PENDING}{fp or ''}"


def _persist_instance(inst: Instance) -> None:
    """Write the instance's sandbox, template, key and fingerprint to state.json.

    Refused (410) for a forgotten tenant, so a cold start that raced /forget
    cannot write the mapping back.
    """
    _check_not_forgotten(inst.tk)
    row = state.setdefault(inst.tk, {})
    row.update({
        "sandbox_id": inst.sandbox_id,
        "template_id": TEMPLATE_ID,
        "llm_fp": inst.llm_fp,
        "api_key": inst.api_key,
    })
    _save_state()


def _drop_state_row(tk: str, sandbox_id: Optional[str] = None) -> None:
    """Forget a tenant's sandbox mapping (only if it still names ``sandbox_id``).

    A /forget tombstone in the row survives.
    """
    row = state.get(tk)
    if row is None:
        return
    if sandbox_id is not None and row.get("sandbox_id") != sandbox_id:
        return
    ts = row.get("forgotten_at")
    if ts:
        state[tk] = {"forgotten_at": ts}
    else:
        state.pop(tk, None)
    _save_state()


async def _discard_sandbox(tk: str, sandbox_id: str) -> None:
    """Delete a sandbox that never became usable; its state row goes only if
    the delete succeeded, so a failed delete is found (and retried) again."""
    if await sbx_delete(sandbox_id):
        _drop_state_row(tk, sandbox_id)


def _mark_engine_stale(inst: Instance) -> None:
    """The engine answered 401 to the router's key: reboot it before next use."""
    if inst.llm_fp == _FP_STALE:
        return
    log.warning("tenant %s engine rejected the router key; reboot on next use", inst.tk[:8])
    inst.llm_fp = _FP_STALE
    if pool.get(inst.tk) is inst:
        try:
            _persist_instance(inst)
        except (OSError, HTTPException) as e:
            log.warning("state write failed while marking %s stale: %s", inst.tk[:8], e)


async def _boot_engine(inst: Instance, fp: Optional[str], env: dict, api_key: str) -> None:
    """(Re)start the tenant engine with ``env`` via the launcher.

    The new key is committed to the instance and state.json BEFORE the
    launcher is called, under a ``boot-pending:<fp>`` fingerprint. The
    launcher's /boot is synchronous and finishes whether or not the router
    is still listening (and on its own boot timeout it answers 500 while the
    engine may still come up), so a router that only recorded the key on a
    200 kept a key the engine no longer accepted. With the key written
    first, the worst case is an unconfirmed configuration: _ensure_ready
    adopts it when the engine turns out to be up with this key, and boots
    again otherwise.
    """
    inst.api_key = api_key
    inst.llm_fp = _pending_fp(fp)
    _persist_instance(inst)
    token = launcher_token(inst.sandbox_id)
    if env.get("VIBE_LAUNCHER_AUTH"):
        env = {**env, "VIBE_LAUNCHER_TOKEN": token}
    r = await http.post(
        f"{inst.launcher_url}/boot", json={"env": env},
        headers={"Authorization": f"Bearer {token}"},
        timeout=httpx.Timeout(30.0, read=float(READY_TIMEOUT_S)),
    )
    if r.status_code != 200:
        raise HTTPException(502, f"engine boot failed: {r.status_code} {r.text[:200]}")
    inst.llm_fp = fp
    _persist_instance(inst)


async def _engine_accepts_key(inst: Instance) -> bool:
    """Cheap authenticated probe: does the running engine take our key?"""
    try:
        r = await _vibe(inst, "GET", "/sessions/keyprobe", timeout=8.0)
    except Exception:  # noqa: BLE001 - unreachable counts as "no"
        return False
    return r.status_code in (200, 404)


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
            # Resume can report success while the VM never comes up; cubelet
            # then reaps the failed task, leaving a half-deleted record
            # (sbx_info 500 NotFoundAtCubelet) that a 404-only self-heal
            # never clears. Tear the sandbox down and drop
            # the mapping HERE so the tenant's next ask cold-rebuilds cleanly.
            log.warning("tenant %s sandbox %s unreachable after resume; discarding",
                        inst.tk[:8], inst.sandbox_id[:12])
            await sbx_delete(inst.sandbox_id)
            pool.pop(inst.tk, None)
            _drop_state_row(inst.tk)
            raise HTTPException(502, "sandbox unreachable after resume; rebuilt on next request")
    inst.paused = False
    if h.get("engine") == "running" and inst.api_key:
        if inst.llm_fp == fp:
            return
        if inst.llm_fp == _pending_fp(fp) and await _engine_accepts_key(inst):
            # The unconfirmed boot for this very configuration did land: the
            # engine is up and takes the key it was booted with.
            inst.llm_fp = fp
            _persist_instance(inst)
            if meta is not None:
                meta["boot_adopted"] = True
            return
    if inst.refcount > 0 and inst.llm_fp != fp:
        raise _Busy("model_switch", "instance busy; retry to switch model")
    if meta is not None:
        meta["booted"] = True
    await _boot_engine(inst, fp, env, api_key)


async def _reboot_engine(
    inst: Instance, model: Optional[str], llm: Optional["LlmOverride"],
) -> None:
    """Boot the engine again with a fresh key (after it rejected ours)."""
    env, api_key = engine_env(model, llm)
    await _boot_engine(inst, llm_fingerprint(model, llm, env), env, api_key)


async def get_or_create(
    tk: str, model: Optional[str] = None, llm: Optional["LlmOverride"] = None,
    meta: Optional[dict] = None,
) -> Instance:
    t0 = time.monotonic()
    _check_not_forgotten(tk)
    async with pool_mutex:
        lock = uid_locks.setdefault(tk, asyncio.Lock())
    async with lock:
        _check_not_forgotten(tk)
        inst = pool.get(tk)
        if inst is None:
            st = state.get(tk)
            if st and st.get("sandbox_id"):
                info = await sbx_info(st["sandbox_id"])
                if info is None:
                    # Best-effort cleanup of a possibly half-deleted record
                    # (NotFoundAtCubelet residue) before rebuilding; a plain
                    # 404 delete is a harmless no-op.
                    await sbx_delete(st["sandbox_id"])
                    _drop_state_row(tk)
                elif st.get("template_id") != TEMPLATE_ID:
                    # Engine code is baked into the image, so a new template only
                    # reaches a tenant by rebuilding its sandbox. Lossless: the
                    # data dir is host-mounted, not in the writable layer.
                    log.info("tenant %s template %s -> %s; rebuilding sandbox",
                             tk[:8], st.get("template_id"), TEMPLATE_ID)
                    await sbx_delete(st["sandbox_id"])
                    _drop_state_row(tk)
                else:
                    # Re-attached from state.json after a router restart: the
                    # sandbox may be running or paused, either way it is not
                    # counted yet.
                    inst = Instance(tk, st["sandbox_id"], st.get("llm_fp"), st.get("api_key"))
                    inst.paused = True
        fresh = inst is None
        if fresh:
            inst = Instance(tk, "", None)
        # Anything not currently counted as RUNNING (new, re-attached, paused)
        # takes its slot BEFORE the sandbox is created/resumed, so the cap
        # holds while READY_TIMEOUT_S of boot is still in flight.
        if fresh or inst.paused:
            await _reserve_running_slot(inst)
        try:
            if fresh:
                inst.sandbox_id = await sbx_create(tk)
                if meta is not None:
                    meta["cold_start"] = True
                log.info("tenant %s -> new sandbox %s", tk[:8], inst.sandbox_id[:12])
            env, api_key = engine_env(model, llm)
            fp = llm_fingerprint(model, llm, env)
            await _ensure_ready(inst, fp, env, api_key, meta=meta)
        except BaseException as exc:
            forgotten = _tombstoned(tk)
            if fresh or forgotten:
                # Never became a usable tenant instance (or the tenant was
                # purged meanwhile): give the slot back and drop the sandbox
                # instead of leaking it. On cancellation (client gone
                # mid-boot) the delete runs detached so the cancel is not
                # blocked on CubeAPI; the sandbox is then in neither pool nor
                # state (the row the boot pre-wrote goes too), so nothing
                # else would ever reap it.
                if pool.get(tk) is inst:
                    pool.pop(tk, None)
                if inst.sandbox_id:
                    if isinstance(exc, Exception):
                        await _discard_sandbox(tk, inst.sandbox_id)
                    else:
                        _drop_state_row(tk, inst.sandbox_id)
                        _spawn(sbx_delete(inst.sandbox_id))
                if forgotten:
                    # A cold start that raced /forget may have recreated the
                    # tenant's data dir before it saw the tombstone.
                    purge = asyncio.to_thread(_rmtree_tenant_dir, DATA_ROOT / tk)
                    if isinstance(exc, Exception):
                        await purge
                    else:
                        _spawn(purge)
            raise
        finally:
            inst.booting = False
        if meta is not None:
            meta["sandbox_ready_ms"] = int((time.monotonic() - t0) * 1000)
        return inst


async def _reserve_running_slot(inst: Instance) -> None:
    """Make room under MAX_RUNNING and count ``inst`` as RUNNING (booting)."""
    async with capacity_lock:
        await _evict_for_capacity()
        inst.paused = False
        inst.booting = True
        inst.last_activity = time.monotonic()
        async with pool_mutex:
            pool[inst.tk] = inst


async def _evict_for_capacity() -> None:
    """Pause LRU idle sandboxes until RUNNING (booting included) < MAX_RUNNING.

    Raises 503 when the cap is reached and nothing idle is left to pause; a
    booting instance is never a victim (it is not idle, it is on its way up),
    nor is one whose per-instance lock is held (a request is inside the
    engine — e.g. a session delete — even though its refcount is 0).
    """
    refused: set[int] = set()  # victims CubeAPI would not pause, this round
    while True:
        running = [i for i in pool.values() if not i.paused]
        if len(running) < MAX_RUNNING:
            return
        idle = sorted(
            (
                i for i in running
                if i.refcount == 0 and not i.booting and not i.lock.locked()
                and id(i) not in refused
            ),
            key=lambda i: i.last_activity,
        )
        if not idle:
            raise _Busy("instances_full", "all instances busy; retry shortly")
        victim = idle[0]
        log.info("pausing LRU tenant %s (%s)", victim.tk[:8], victim.sandbox_id[:12])
        # Counted out before the pause call yields, so a concurrent count
        # cannot hand the same slot to two callers; counted back in if the
        # pause is refused (the sandbox is still running).
        victim.paused = True
        if not await sbx_pause(victim.sandbox_id):
            victim.paused = False
            refused.add(id(victim))


# ── Vibe session helpers ─────────────────────────────────────────────────────
def _engine_headers(inst: Instance) -> dict:
    return {"Authorization": f"Bearer {inst.api_key}"} if inst.api_key else {}


async def _vibe(inst: Instance, method: str, path: str, **kw):
    headers = {**_engine_headers(inst), **kw.pop("headers", {})}
    r = await http.request(method, f"{inst.base_url}{path}", headers=headers, **kw)
    if r.status_code == 401:
        _mark_engine_stale(inst)
    return r


class _EngineUnauthorized(HTTPException):
    """The engine rejected the router's key for this instance."""

    def __init__(self) -> None:
        super().__init__(502, "deep engine rejected the router key")


async def _cancel_attempt_bg(
    inst: Instance, sid: str, tk: str, stats: dict, finalize: Any = None
) -> None:
    """Fire-and-forget engine cancel, detached from the (possibly dying) ask
    generator. Called from the unanswered path of _ask_stream: on client
    disconnect uvicorn *cancels* the generator task, so any `await` in its
    finally raises CancelledError before the HTTP request goes out — the
    engine keeps grinding, gets frozen by pause, and resumes as a zombie that
    422s new asks. A separate task survives that
    cancellation and reliably delivers the cancel.

    Owns the ask-log line for this ask: ``engine_cancelled`` means the engine
    confirmed a loop or attempt received the signal (``status=cancelled``),
    ``engine_cancel_status`` carries the raw answer (``no_active_loop``,
    ``http_<code>``, ``unreachable``) so "sent" and "took effect" stay
    distinguishable in the log.
    """
    try:
        r = await _vibe(inst, "POST", f"/sessions/{sid}/cancel", timeout=10.0)
        status = f"http_{r.status_code}"
        if r.status_code == 200:
            try:
                status = str((r.json() or {}).get("status") or "unknown")
            except Exception:  # noqa: BLE001 - non-JSON body
                status = "unknown"
        stats["engine_cancel_status"] = status
        stats["engine_cancelled"] = status == "cancelled"
        log.info("cancel unfinished attempt (tenant %s, sid %s) -> %s", tk[:8], sid, status)
    except Exception as e:  # noqa: BLE001
        stats["engine_cancel_status"] = "unreachable"
        stats["engine_cancelled"] = False
        log.warning("background cancel failed (tenant %s, sid %s): %s", tk[:8], sid, e)
    finally:
        if finalize is not None:
            finalize()
        _record_ask(stats)


async def _ensure_session(inst: Instance, vibe_session_id: Optional[str]) -> str:
    if vibe_session_id:
        return vibe_session_id
    r = await _vibe(inst, "POST", "/sessions", json={"title": "laicai"})
    if r.status_code == 401:
        raise _EngineUnauthorized()
    r.raise_for_status()
    sid = r.json().get("session_id")
    if not sid:
        raise HTTPException(502, "vibe returned no session_id")
    return sid


async def _post_turn(
    inst: Instance,
    sid: str,
    query: str,
    deadline_s: Optional[float] = None,
    intent: Optional[str] = None,
    swarm_preset: Optional[str] = None,
) -> Optional[str]:
    payload: dict = {"content": query}
    if deadline_s is not None:
        # The engine finalizes with what it has before this budget runs out
        # instead of grinding past the caller's timeout.
        payload["deadline_s"] = round(deadline_s, 1)
    # Structured intent rides alongside deadline_s. Engines that don't know
    # these fields yet ignore them (the request model tolerates extras), so the
    # router can ship ahead of the engine — that is the whole point of staging
    # the cross-repo contract: the budget lands first, the prompt branch later.
    if intent is not None:
        payload["intent"] = intent
    if swarm_preset is not None:
        payload["swarm_preset"] = swarm_preset
    r = await _vibe(inst, "POST", f"/sessions/{sid}/messages", json=payload)
    if r.status_code == 404:
        raise _SessionGone()
    if r.status_code == 401:
        raise _EngineUnauthorized()
    if r.status_code == 422:
        # Pydantic validation on the engine side. The one users actually hit
        # is the input length cap (portfolio context + question); say so
        # instead of surfacing a bare 422.
        raise _QueryRejected(r)
    r.raise_for_status()
    return r.json().get("attempt_id")


ENGINE_QUERY_MAX_CHARS = 20000  # mirrors SendMessageRequest.content max_length


def _engine_422_is_length_cap(text: str) -> bool:
    return "content" in text and (
        "string_too_long" in text or "max_length" in text or "too_long" in text
    )


def _engine_422_detail(r: "httpx.Response") -> str:
    """Human-readable detail for an engine 422 (length cap vs other)."""
    text = r.text or ""
    if _engine_422_is_length_cap(text):
        return (
            f"问题过长，请精简后重试（引擎单次输入上限 {ENGINE_QUERY_MAX_CHARS} 字符，"
            "含注入的持仓上下文）"
        )
    return f"引擎拒绝了请求参数: {text[:200]}"


class _QueryRejected(HTTPException):
    """400 for an engine 422; ``frame_code`` tells the length cap apart."""

    def __init__(self, r: "httpx.Response") -> None:
        super().__init__(400, _engine_422_detail(r))
        self.frame_code = (
            "query_too_long" if _engine_422_is_length_cap(r.text or "") else "query_rejected"
        )


class _EngineFailed(HTTPException):
    """The engine finished the attempt with status=failed (not a timeout)."""

    def __init__(self, error: str) -> None:
        super().__init__(502, f"deep engine failed: {error}")
        self.engine_error = error


def _classify_answer_message(msg: dict, attempt_id: Optional[str]) -> tuple[str, str]:
    """Classify one stored engine message for ``_wait_answer``.

    Returns ``(kind, text)`` with kind ∈ {"skip", "answer", "failed"}. Pure
    so the router test can pin it without an engine.

    A failed attempt must not be forwarded as the answer: the engine writes
    an assistant message "Execution failed: …" linked to the attempt, so
    accepting any non-empty linked assistant text would hand that prose to
    the user. The engine stamps ``metadata.ok`` / ``metadata.error``;
    ``metadata.status == "failed"`` (which older engines already wrote)
    is honoured as well.
    """
    if msg.get("role") != "assistant":
        return "skip", ""
    content = (msg.get("content") or "").strip()
    if not content:
        return "skip", ""
    if attempt_id and msg.get("linked_attempt_id") not in (attempt_id, None):
        return "skip", ""
    meta = msg.get("metadata") or {}
    if isinstance(meta, dict) and (meta.get("ok") is False or meta.get("status") == "failed"):
        return "failed", str(meta.get("error") or content)[:500]
    return "answer", content


class _FailSignal:
    """``attempt.failed`` seen on the engine event stream for this attempt.

    ``_wait_answer`` sleeps on it between message polls, so an attempt that
    dies before writing anything (disk full in the engine's preparation
    segment, registry build error) ends the ask at once instead of after
    the whole budget.
    """

    def __init__(self) -> None:
        self.event = asyncio.Event()
        self.error = ""

    def fire(self, error: str) -> None:
        self.error = error
        self.event.set()


async def _poll_messages(inst: Instance, sid: str) -> tuple[Optional[list], str]:
    """One answer poll. Returns ``(messages, "")`` or ``(None, problem)``."""
    try:
        m = await _vibe(
            inst, "GET", f"/sessions/{sid}/messages",
            params={"limit": 50}, timeout=POLL_HTTP_TIMEOUT,
        )
    except (httpx.HTTPError, OSError) as e:
        return None, type(e).__name__
    if m.status_code == 401:
        # The engine no longer accepts the key this attempt was posted with:
        # it was restarted underneath us, so the attempt is gone.
        raise HTTPException(502, "deep engine restarted during the attempt (key rejected)")
    if m.status_code != 200:
        return None, f"http_{m.status_code}"
    try:
        msgs = m.json()
    except ValueError:
        return None, "invalid_json"
    if not isinstance(msgs, list):
        return None, "invalid_body"
    return msgs, ""


async def _wait_answer(
    inst: Instance, sid: str, attempt_id: Optional[str], timeout_s: int,
    failed: Optional[_FailSignal] = None,
    deadline: Optional[float] = None,
    stats: Optional[dict] = None,
) -> str:
    """Poll the engine's message list until this attempt's answer appears.

    ``deadline`` (monotonic) overrides ``timeout_s`` so the caller can anchor
    the window to when the ask arrived. Poll failures are tolerated per
    POLL_FAIL_MAX_CONSECUTIVE / POLL_FAIL_MAX_S; while they last the launcher
    is probed so a stopped engine fails the ask immediately instead of after
    the tolerance window. ``stats["poll_errors"]`` counts failed polls.
    """
    if deadline is None:
        deadline = time.monotonic() + timeout_s
    streak = 0
    streak_t0 = 0.0
    while time.monotonic() < deadline:
        if failed is None:
            await asyncio.sleep(POLL_INTERVAL_S)
        else:
            try:
                await asyncio.wait_for(failed.event.wait(), timeout=POLL_INTERVAL_S)
            except asyncio.TimeoutError:
                pass
            if failed.event.is_set():
                raise _EngineFailed(failed.error or "attempt failed")
        msgs, problem = await _poll_messages(inst, sid)
        if msgs is None:
            now = time.monotonic()
            if streak == 0:
                streak_t0 = now
            streak += 1
            if stats is not None:
                stats["poll_errors"] = int(stats.get("poll_errors") or 0) + 1
            h = await _launcher_health(inst)
            engine_state = (h or {}).get("engine")
            log.warning("answer poll failed (sid %s, %s, streak %d, engine %s)",
                        sid, problem, streak, engine_state or "unreachable")
            if engine_state == "stopped":
                raise HTTPException(502, "deep engine process stopped during the attempt")
            if streak >= POLL_FAIL_MAX_CONSECUTIVE or now - streak_t0 >= POLL_FAIL_MAX_S:
                raise HTTPException(
                    502, f"deep engine unreachable ({problem}; {streak} failed polls)"
                )
            continue
        streak = 0
        for msg in reversed(msgs):
            kind, text = _classify_answer_message(msg, attempt_id)
            if kind == "answer":
                return text
            if kind == "failed":
                raise _EngineFailed(text)
    raise HTTPException(504, "deep engine timed out")


# Event stream reconnect: the engine sends a heartbeat every 30s of silence,
# so a read that stays silent for PUMP_READ_TIMEOUT_S is a dead connection.
PUMP_READ_TIMEOUT_S = float(os.environ.get("VIBE_PUMP_READ_TIMEOUT_S", "90"))
PUMP_RECONNECT_MIN_DELAY_S = 0.5
PUMP_RECONNECT_MAX_DELAY_S = 10.0
_PUMP_SEEN_IDS = 4096


async def _pump_events(
    inst: Instance, sid: str, q: "asyncio.Queue[dict]", stats: Optional[dict] = None,
) -> None:
    """Forward the engine's session SSE stream into ``q`` until cancelled.

    A dropped stream is reopened with ``Last-Event-ID`` set to the last event
    id seen, so the engine replays only what was missed (its per-session
    buffer); before any id has been seen it reopens with ``replay=active``
    exactly like the first connect. Event ids already forwarded are skipped,
    so a replay never duplicates a metered event. The pump runs until the
    ask cancels it; reconnects back off up to PUMP_RECONNECT_MAX_DELAY_S.
    ``stats["pump_reconnects"]`` counts reopened streams.
    """
    last_id: Optional[str] = None
    seen: set[str] = set()
    seen_order: "deque[str]" = deque()
    delay = PUMP_RECONNECT_MIN_DELAY_S
    first = True
    while True:
        if not first:
            if stats is not None:
                stats["pump_reconnects"] = int(stats.get("pump_reconnects") or 0) + 1
            await asyncio.sleep(delay)
        first = False
        headers = _engine_headers(inst)
        if last_id:
            headers["Last-Event-ID"] = last_id
        delivered = False
        try:
            async with http.stream(
                "GET",
                f"{inst.base_url}/sessions/{sid}/events",
                params={"replay": "active"},
                headers=headers,
                timeout=httpx.Timeout(30.0, read=PUMP_READ_TIMEOUT_S),
            ) as r:
                if r.status_code != 200:
                    if r.status_code == 401:
                        _mark_engine_stale(inst)
                    raise RuntimeError(f"events http {r.status_code}")
                ev_type: Optional[str] = None
                ev_id: Optional[str] = None
                async for line in r.aiter_lines():
                    if line.startswith("id:"):
                        ev_id = line[3:].strip() or None
                    elif line.startswith("event:"):
                        ev_type = line[6:].strip()
                    elif line.startswith("data:"):
                        raw = line[5:].strip()
                        try:
                            payload = json.loads(raw)
                        except Exception:
                            payload = raw
                        this_id, ev_id = ev_id, None
                        name, ev_type = ev_type or "message", None
                        if this_id:
                            if this_id in seen:
                                continue
                            seen.add(this_id)
                            seen_order.append(this_id)
                            if len(seen_order) > _PUMP_SEEN_IDS:
                                seen.discard(seen_order.popleft())
                            last_id = this_id
                        q.put_nowait({"ev": name, "data": payload})
                        delivered = True
            reason = "stream closed"
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - any failure means reconnect
            reason = f"{type(e).__name__}: {e}"
        delay = (
            PUMP_RECONNECT_MIN_DELAY_S if delivered
            else min(delay * 2, PUMP_RECONNECT_MAX_DELAY_S)
        )
        log.info("event pump for %s dropped (%s); reconnecting in %.1fs", sid, reason, delay)


# Events the caller bills or reads as this ask's outcome (laicai sums every
# forwarded llm_usage into the user's token quota and stores attempt_stats as
# the run's engine stats).
_METERED_EVENTS = frozenset({"llm_usage", "attempt_stats"})


def _event_belongs_to_ask(ev: dict, attempt_id: Optional[str]) -> bool:
    """Whether one engine event may be forwarded as part of this ask.

    The engine stamps ``attempt_id`` on every attempt-scoped event, and its
    per-session buffer replays the whole buffer to a subscriber that joins
    while an attempt is running — on a continued session that buffer still
    holds the previous attempt's ``llm_usage`` / ``attempt_stats``. Only the
    current attempt's events go through. Session-level events carry no
    ``attempt_id`` (``heartbeat``, ``message.received``, …) and pass as
    before, except metered ones: usage that cannot be attributed to this
    attempt is never forwarded. An engine that returned no ``attempt_id``
    gives nothing to filter on, so everything passes.
    """
    if attempt_id is None:
        return True
    data = ev.get("data")
    owner = data.get("attempt_id") if isinstance(data, dict) else None
    if owner is None:
        return ev.get("ev") not in _METERED_EVENTS
    return owner == attempt_id


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
    # Structured research-depth intent (cross-repo contract with laicai).
    # "deep_team" = multi-agent swarm committee. The router derives the budget
    # from it (BUDGET_BY_INTENT) instead of every caller hardcoding 7200.
    # Optional on purpose: an older laicai that sends only `timeoutS` behaves
    # exactly as before.
    intent: Optional[str] = None
    # Swarm preset name for deep_team asks. Forwarded to the engine as-is; the
    # authoritative enum is the engine's agent/src/swarm/presets/*.yaml listing,
    # so the router deliberately does NOT validate it against a copied list
    # (a stale copy here would reject presets the engine actually supports).
    swarmPreset: Optional[str] = None
    timeoutS: Optional[int] = None


def _auth(authorization: Optional[str]) -> None:
    expected = f"Bearer {ROUTER_TOKEN}"
    if not authorization or not hmac.compare_digest(authorization, expected):
        raise HTTPException(401, "unauthorized")


# In-flight attempts (attempt_id → (inst, sid)) so a graceful router shutdown
# can tell engines to stop: a deploy restart that kills the stream mid-run
# never runs the generator's finally, and the orphaned attempt would burn
# half an hour writing an answer nobody will read.
_INFLIGHT: dict[str, tuple[Instance, str]] = {}


@app.on_event("shutdown")
async def _cancel_inflight_on_shutdown() -> None:
    for attempt_id, (inst, sid) in list(_INFLIGHT.items()):
        try:
            await _vibe(inst, "POST", f"/sessions/{sid}/cancel", timeout=5.0)
            log.info("shutdown: cancelled in-flight attempt %s (sid %s)", attempt_id, sid)
        except Exception as e:  # noqa: BLE001 - best-effort during teardown
            log.warning("shutdown cancel failed for %s: %s", attempt_id, e)
    _INFLIGHT.clear()


def _frame(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False) + "\n"


def _classify_status(status_code: int, exc: Optional[HTTPException] = None) -> str:
    if isinstance(exc, _EngineFailed):
        return "engine_failed"
    return {
        503: "busy", 504: "timeout", 502: "upstream_failed", 410: "forgotten",
    }.get(status_code, "error")


async def _ask_stream(body: AskBody, timeout_s: int):
    tk = tenant_key(body.uid)
    t_req = time.monotonic()
    # The whole ask — slot queue, cold start, lock wait, engine work — lives
    # inside timeout_s from arrival. The caller's own clock started a moment
    # earlier and allows timeout_s + a small margin, so the router's 504 (with
    # stats) always lands before the caller gives up.
    answer_deadline = t_req + timeout_s
    stats: dict[str, Any] = {
        "tk8": tk[:8],
        "channel": "byok" if body.llm else "builtin",
        "model": (body.llm.model if body.llm else body.model) or None,
        "timeout_s": timeout_s,
        # Which tier this ask got and why — the ask log is where a "why did my
        # swarm only get 15 minutes" question gets answered.
        "intent": body.intent or "standard",
        "budget_source": "explicit" if body.timeoutS else "intent",
        "outcome": "incomplete",
    }
    engine_stats: Optional[dict] = None
    # Set when the unanswered path handed the engine cancel to a detached
    # task; that task then also writes the ask-log line (with the engine's
    # answer to the cancel), so the line is written exactly once.
    cancel_task: Optional["asyncio.Task[Any]"] = None

    def _grab_engine_stats(ev: dict) -> None:
        nonlocal engine_stats
        if ev.get("ev") == "attempt_stats" and isinstance(ev.get("data"), dict):
            engine_stats = ev["data"]

    def _finalize_stats() -> None:
        stats.setdefault("total_ms", int((time.monotonic() - t_req) * 1000))
        if engine_stats:
            stats["engine_status"] = engine_stats.get("status")
            stats["iterations"] = engine_stats.get("iterations")

    try:
        _check_not_forgotten(tk)
        async with _active_slot(min(ACTIVE_QUEUE_WAIT_S, float(timeout_s)), stats):
            meta: dict[str, Any] = {}
            try:
                inst = await get_or_create(tk, body.model, body.llm, meta=meta)
            finally:
                stats.update(meta)
            inst.refcount += 1
            try:
                # Per-tenant serialization point: a second ask for the same
                # tenant queues HERE while the first is running — unmeasured,
                # a long silent lock wait reads as a giant first_progress.
                # Measure it explicitly.
                lock_t0 = time.monotonic()
                async with inst.lock:
                    stats["lock_wait_ms"] = int((time.monotonic() - lock_t0) * 1000)
                    sess_t0 = time.monotonic()

                    async def _open_turn() -> tuple[str, Optional[str]]:
                        # Engine-side budget = what's left of the caller's
                        # timeout after queueing/boot, minus a margin for the
                        # answer poll.
                        engine_deadline_s = max(
                            60.0, timeout_s - (time.monotonic() - t_req) - 10.0
                        )
                        stats["engine_deadline_s"] = round(engine_deadline_s, 1)
                        sid_ = await _ensure_session(inst, body.vibeSessionId)
                        turn_kwargs = {
                            "deadline_s": engine_deadline_s,
                            "intent": body.intent,
                            "swarm_preset": body.swarmPreset,
                        }
                        try:
                            return sid_, await _post_turn(
                                inst, sid_, body.query, **turn_kwargs
                            )
                        except _SessionGone:
                            stats["session_recovered"] = True
                            sid_ = await _ensure_session(inst, None)
                            return sid_, await _post_turn(
                                inst, sid_, body.query, **turn_kwargs
                            )

                    try:
                        sid, attempt_id = await _open_turn()
                    except _EngineUnauthorized:
                        # The engine was restarted outside the router (its
                        # key is not ours): boot it once with a fresh key and
                        # retry. Safe here — inst.lock is held, so no other
                        # ask of this tenant is in flight.
                        stats["auth_reboot"] = True
                        await _reboot_engine(inst, body.model, body.llm)
                        sid, attempt_id = await _open_turn()
                    stats["session_ms"] = int((time.monotonic() - sess_t0) * 1000)
                    stats["attempt_id"] = attempt_id
                    _INFLIGHT[attempt_id] = (inst, sid)
                    # Early meta frame: laicai uses it to stamp attempt_id /
                    # session id onto its status=running placeholder row, so
                    # the admin detail page can tail engine logs/trace while
                    # the run is still in flight (not only after the terminal
                    # frame). Consumers ignore unknown ev names, so this is
                    # backward-compatible. ``answer_deadline_s`` = seconds from
                    # this frame until the router answers or 504s;
                    # ``engine_deadline_s`` = the budget handed to the engine.
                    yield _frame({
                        "t": "progress", "ev": "attempt_meta",
                        "data": {
                            "attempt_id": attempt_id,
                            "vibe_session_id": sid,
                            "answer_deadline_s": round(
                                max(0.0, answer_deadline - time.monotonic()), 1
                            ),
                            "engine_deadline_s": stats.get("engine_deadline_s"),
                        },
                    })

                    answered = False
                    q: "asyncio.Queue[dict]" = asyncio.Queue()
                    failed = _FailSignal()

                    def _admit_event(ev: dict) -> bool:
                        """Attribution filter + the bookkeeping of an admitted event."""
                        if not _event_belongs_to_ask(ev, attempt_id):
                            stats["stale_events_dropped"] = (
                                int(stats.get("stale_events_dropped") or 0) + 1
                            )
                            return False
                        if ev.get("ev") != "heartbeat":
                            stats.setdefault(
                                "first_progress_ms", int((time.monotonic() - t_req) * 1000)
                            )
                        _grab_engine_stats(ev)
                        return True

                    def _note_attempt_failed(ev: dict) -> None:
                        if ev.get("ev") != "attempt.failed":
                            return
                        data = ev.get("data")
                        if not isinstance(data, dict):
                            return
                        if data.get("attempt_id") in (attempt_id, None):
                            failed.fire(str(data.get("error") or "attempt failed")[:500])

                    pump = asyncio.create_task(_pump_events(inst, sid, q, stats=stats))
                    waiter = asyncio.create_task(
                        _wait_answer(
                            inst, sid, attempt_id, timeout_s, failed=failed,
                            deadline=answer_deadline, stats=stats,
                        )
                    )
                    try:
                        while not waiter.done():
                            try:
                                ev = await asyncio.wait_for(q.get(), timeout=1.0)
                            except asyncio.TimeoutError:
                                continue
                            inst.last_activity = time.monotonic()
                            if not _admit_event(ev):
                                continue
                            _note_attempt_failed(ev)
                            yield _frame({"t": "progress", **ev})
                        while not q.empty():
                            ev = q.get_nowait()
                            if _admit_event(ev):
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
                        _INFLIGHT.pop(attempt_id, None)
                        pump.cancel()
                        waiter.cancel()
                        if not answered:
                            # Timeout / client gone / internal error: tell the
                            # engine to stop burning tokens on an answer nobody
                            # will receive (a 504'd attempt that keeps grinding
                            # starves the tenant's retry). MUST be a detached
                            # task, not an await — on client disconnect this
                            # generator is being cancelled and an await here
                            # dies before sending, leaving a zombie attempt. It
                            # stamps
                            # engine_cancelled from the engine's answer and
                            # writes the ask-log line.
                            cancel_task = _spawn(
                                _cancel_attempt_bg(inst, sid, tk, stats, _finalize_stats)
                            )
            finally:
                inst.refcount -= 1
    except HTTPException as e:
        stats["outcome"] = _classify_status(e.status_code, e)
        stats["error"] = str(e.detail)[:300]
        stats["total_ms"] = int((time.monotonic() - t_req) * 1000)
        frame: dict[str, Any] = {"t": "error", "status": e.status_code, "detail": str(e.detail)}
        # Machine-readable reason for the errors a caller can act on
        # (busy / query_too_long / query_rejected / tenant_forgotten).
        code = getattr(e, "frame_code", None)
        if code:
            frame["code"] = code
        if isinstance(e, _Busy):
            stats["busy_reason"] = e.busy_reason
            frame["busy_reason"] = e.busy_reason
        frame["stats"] = {"router": dict(stats), "engine": engine_stats}
        yield _frame(frame)
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
        _finalize_stats()
        if cancel_task is None:
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
    if body.intent is not None and body.intent not in BUDGET_BY_INTENT:
        raise HTTPException(400, "invalid intent")
    if body.swarmPreset is not None and not re.fullmatch(r"[a-z0-9_]{3,64}", body.swarmPreset):
        raise HTTPException(400, "invalid swarmPreset")
    timeout_s = budget_for(body.intent, body.timeoutS)
    return StreamingResponse(_ask_stream(body, timeout_s), media_type="application/x-ndjson")


@app.post("/forget")
async def forget(body: dict, authorization: Optional[str] = Header(None)):
    _auth(authorization)
    uid = body.get("uid")
    if not uid:
        raise HTTPException(400, "uid required")
    tk = tenant_key(uid)
    # Tombstone first: from here on no ask can create a sandbox, write the
    # mapping back or recreate the data dir for this tenant — including a
    # cold start already in flight, which aborts on it and removes what it
    # made. Then take the tenant lock (bounded) so an in-flight cold start
    # normally finishes aborting before the purge runs.
    _set_tombstone(tk)
    async with pool_mutex:
        lock = uid_locks.setdefault(tk, asyncio.Lock())
    locked = False
    try:
        await asyncio.wait_for(lock.acquire(), timeout=FORGET_LOCK_WAIT_S)
        locked = True
    except asyncio.TimeoutError:
        log.warning("forget tenant %s: tenant lock busy for %.0fs; purging anyway",
                    tk[:8], FORGET_LOCK_WAIT_S)
    try:
        return await _forget_locked(tk)
    finally:
        if locked:
            lock.release()


async def _forget_locked(tk: str):
    async with pool_mutex:
        inst = pool.pop(tk, None)
    sandbox_id = (inst.sandbox_id if inst else "") or (state.get(tk) or {}).get("sandbox_id")
    errors: list[str] = []
    sandbox_ok = True
    if sandbox_id:
        try:
            sandbox_ok = await sbx_delete(sandbox_id)
        except Exception as e:  # noqa: BLE001 - must reach the response body
            sandbox_ok = False
            log.warning("forget: sandbox delete %s raised: %s", sandbox_id[:12], e)
        if not sandbox_ok:
            errors.append(f"sandbox {sandbox_id[:12]} delete failed")
    # Tenant data now outlives the sandbox, so forgetting must remove it here too.
    rm_err = await asyncio.to_thread(_rmtree_tenant_dir, DATA_ROOT / tk)
    if rm_err:
        errors.append(rm_err)
    if sandbox_ok:
        # Keep the mapping while the sandbox still exists so the nightly
        # retry can find it again; a leftover dir alone needs no mapping.
        # The tombstone stays either way.
        _drop_state_row(tk)
    if errors:
        # laicai's engine-forget job keys on `res.ok` (engine-forget.ts): a
        # non-2xx keeps the job pending for the 23:30 retry instead of
        # marking a half-done purge as finished.
        detail = "; ".join(errors)
        log.warning("forget tenant %s incomplete: %s", tk[:8], detail)
        return JSONResponse(status_code=500, content={"ok": False, "error": detail})
    return {"ok": True}


def _rmtree_tenant_dir(path: Path) -> Optional[str]:
    """Remove a tenant dir; return an error string instead of raising.

    A symlinked tenant dir is refused (rmtree would follow nothing but the
    link itself is a sign of tampering, and the target may be host data).
    """
    if path.is_symlink():
        return f"tenant dir {path.name[:8]} is a symlink; refusing to remove"
    if not path.exists():
        return None
    failures: list[str] = []

    def _onerror(_fn: Any, p: Any, exc_info: Any) -> None:
        failures.append(f"{Path(str(p)).name}: {exc_info[1]}")

    shutil.rmtree(path, onerror=_onerror)
    if failures or path.exists():
        return f"tenant dir removal incomplete ({len(failures)} errors: {'; '.join(failures[:3])})"
    return None


# ── Per-session deletion (laicai "删除对话" → engine session) ────────────────
# laicai deletes a chat thread; the bound engine session (messages.jsonl,
# trace.jsonl with every prompt, transcript_*.jsonl compaction dumps,
# handoff.json) and the ``runs/<id>`` directories it produced (linked through
# ``req.json`` ``context.session_id``) go with it — otherwise the only purge
# path would be the whole-tenant /forget at account deletion.
#
# Two modes, chosen by the router, reported back in ``mode``:
#   engine  — the tenant's sandbox is up: DELETE /sessions/{id} on the engine,
#             which cancels a live loop, drops the dir, its runs AND its
#             sessions.db FTS rows (search would otherwise keep returning the
#             deleted conversation).
#   offline — no running sandbox (never created / paused / evicted): the
#             session dir and its runs are removed straight off the host
#             bind-mount. FTS / goal-ledger rows in sessions.db are NOT touched
#             from the host (the engine may hold WAL state in the frozen VM):
#             the engine drops them itself — at its next start
#             (``SessionService.reconcile_orphans``) and on sight during
#             ``session_search`` (a hit whose directory is gone is deleted,
#             not returned).
# Idempotent: a session that is already gone answers ok=true, deleted=false.


class SessionDeleteBody(BaseModel):
    uid: str
    session_id: str


def _session_run_dirs(root: Path, session_id: str) -> list[Path]:
    """``runs/<id>`` dirs under the tenant whose ``req.json`` names ``session_id``.

    Every candidate passes :func:`_safe_tenant_path` (a symlinked run dir or
    ``req.json`` is skipped, never followed). Cost is one small JSON read per
    run directory, paid only on a session delete.
    """
    runs = _safe_tenant_path(root, root / "runs")
    if runs is None or not runs.is_dir():
        return []
    out: list[Path] = []
    for d in runs.iterdir():
        if d.is_symlink() or not d.is_dir() or _safe_tenant_path(runs, d) is None:
            continue
        req = _safe_tenant_path(d, d / "req.json")
        if req is None or not req.is_file():
            continue
        try:
            data = json.loads(req.read_text("utf-8", "replace"))
        except (OSError, ValueError):
            continue
        ctx = data.get("context") if isinstance(data, dict) else None
        if isinstance(ctx, dict) and ctx.get("session_id") == session_id:
            out.append(d)
    return out


def _rmtree_collect(path: Path, failures: list[str]) -> None:
    def _onerror(_fn: Any, p: Any, exc_info: Any) -> None:
        failures.append(f"{Path(str(p)).name}: {exc_info[1]}")

    shutil.rmtree(path, onerror=_onerror)


def _remove_session_dir(uid: str, session_id: str) -> tuple[bool, Optional[str]]:
    """Remove ``DATA_ROOT/<tk>/sessions/<sid>`` and the session's run dirs.

    Returns:
        ``(removed, error)`` — ``removed`` is False when no session dir was
        there (its runs, if any, are still removed); ``error`` is set when a
        dir exists but could not be removed (or the session dir is a symlink,
        which is refused rather than followed).
    """
    root = _tenant_root(uid)
    candidate = root / "sessions" / session_id
    if candidate.is_symlink():
        return False, f"session dir {session_id} is a symlink; refusing to remove"
    failures: list[str] = []
    for run_dir in _session_run_dirs(root, session_id):
        _rmtree_collect(run_dir, failures)
        if run_dir.exists():
            failures.append(f"{run_dir.name}: still present")
    path = _safe_tenant_path(root, candidate)
    if path is None or not path.is_dir():
        if failures:
            return False, f"run dir removal incomplete ({'; '.join(failures[:3])})"
        return False, None
    _rmtree_collect(path, failures)
    if failures or path.exists():
        return False, f"session dir removal incomplete ({'; '.join(failures[:3])})"
    return True, None


@app.post("/sessions/delete")
async def sessions_delete(
    body: SessionDeleteBody, authorization: Optional[str] = Header(None)
):
    """Delete one engine session of a tenant (see the section comment).

    Response: ``{"ok": true, "mode": "engine"|"offline", "deleted": bool}``;
    ``deleted=false`` means the session was already gone. A host-side removal
    failure answers 500 ``{"ok": false, "mode": ..., "error": ...}`` so the
    caller can retry, mirroring ``/forget``.
    """
    _auth(authorization)
    sid = body.session_id
    if not _OBS_ID_RE.fullmatch(sid):
        raise HTTPException(400, "invalid session_id")
    tk = tenant_key(body.uid)
    inst = pool.get(tk)
    mode = "offline"
    engine_deleted = False
    if inst is not None and not inst.paused and not inst.booting:
        # Live sandbox: the engine owns sessions.db, so let it do the delete
        # (loop cancel + dir + FTS rows). Any failure falls through to the
        # host-side removal so the data still goes away.
        try:
            r = await _vibe(inst, "DELETE", f"/sessions/{sid}", timeout=15.0)
            if r.status_code in (200, 404):
                mode = "engine"
                engine_deleted = r.status_code == 200
                inst.last_activity = time.monotonic()
            else:
                log.warning("sessions/delete: engine %s -> %s %s; falling back to host",
                            sid, r.status_code, r.text[:200])
        except Exception as e:  # noqa: BLE001 - offline path takes over
            log.warning("sessions/delete: engine unreachable for %s (%s); host removal",
                        sid, e)
    removed, err = await asyncio.to_thread(_remove_session_dir, body.uid, sid)
    if err:
        log.warning("sessions/delete tenant %s sid %s: %s", tk[:8], sid, err)
        return JSONResponse(
            status_code=500, content={"ok": False, "mode": mode, "error": err}
        )
    return {"ok": True, "mode": mode, "deleted": engine_deleted or removed}


# ── Read-only tenant observability (laicai admin deep-run detail page) ───────
# Serves the tenant's engine.jsonl / trace.jsonl straight off the host
# bind-mount, so operators can inspect a run in the browser instead of SSH.
# Bearer-gated like everything else; ids are strictly validated so a caller
# can never traverse outside the tenant's data dir.

_OBS_ID_RE = re.compile(r"[A-Za-z0-9_-]{4,64}")
_OBS_TAIL_BYTES = 4_000_000


def _safe_tenant_path(base: Path, p: Path) -> Optional[Path]:
    """Return ``p`` only when it is a real file/dir INSIDE ``base``.

    The engine runs as uid 1000 inside the tenant's own bind-mount and can
    create symlinks there at will; the router runs as root on the host and
    would read/write whatever those paths point at (a tenant
    ``memory/x.md -> /etc/shadow`` readable through ``/memory``,
    ``MEMORY.md -> /root/.ssh/authorized_keys`` writable through
    ``/memory/delete``). Rules:

    * ``p`` itself must not be a symlink;
    * ``p.resolve()`` must be ``base.resolve()`` or below it (this also
      rejects a symlinked PARENT directory pointing outside the tenant).

    Args:
        base: The tenant's root (or a subdir of it) that ``p`` must stay in.
        p: Candidate path (need not exist).

    Returns:
        ``p`` unchanged when safe, ``None`` when it must be treated as absent.
    """
    try:
        if p.is_symlink():
            return None
        base_r = base.resolve()
        real = p.resolve()
    except OSError:
        return None
    if real != base_r and base_r not in real.parents:
        return None
    return p


def _tenant_root(uid: str) -> Path:
    return DATA_ROOT / tenant_key(uid)


def _tenant_file(uid: str, *parts: str) -> Optional[Path]:
    """``DATA_ROOT/<tk>/<parts…>`` guarded by :func:`_safe_tenant_path`."""
    root = _tenant_root(uid)
    return _safe_tenant_path(root, root.joinpath(*parts))


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
    path = _tenant_file(uid, "logs", "engine.jsonl")
    if path is None or not path.is_file():
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
    path = _tenant_file(uid, "sessions", session_id, "trace.jsonl")
    if path is None or not path.is_file():
        return {"entries": [], "truncated": False}
    raw = await asyncio.to_thread(_obs_tail_lines, path)
    out = []
    for line in raw:
        try:
            out.append(_obs_clip(json.loads(line)))
        except Exception:
            continue
    return {"entries": out[-limit:], "truncated": len(out) > limit}


_OBS_PROMPT_CAP = 65536


@app.get("/obs/prompt")
async def obs_prompt(
    uid: str,
    session_id: str,
    authorization: Optional[str] = Header(None),
):
    """Full engine-input prompts of a session's attempts, UNCLIPPED.

    /obs/trace clips every field at 600 chars; the call-input viewer on the
    laicai trace page needs the whole prompt. A continuity session holds one
    ``start`` trace event per attempt — all are returned with their ts so the
    caller matches the right attempt by time (start events carry no
    attempt_id). Per-prompt cap 64KB.
    """
    _auth(authorization)
    if not _OBS_ID_RE.fullmatch(session_id):
        raise HTTPException(400, "invalid session_id")
    path = _tenant_file(uid, "sessions", session_id, "trace.jsonl")
    if path is None or not path.is_file():
        return {"starts": []}

    def _read_starts() -> list[dict[str, Any]]:
        starts: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"start"' not in line:
                    continue
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                if e.get("type") != "start":
                    continue
                prompt = str(e.get("prompt") or "")
                starts.append({
                    "ts": e.get("ts"),
                    "prompt": prompt[:_OBS_PROMPT_CAP],
                    "truncated": len(prompt) > _OBS_PROMPT_CAP,
                })
        return starts[-20:]

    return {"starts": await asyncio.to_thread(_read_starts)}


@app.get("/obs/swarm-events")
async def obs_swarm_events(
    uid: str,
    run_id: str,
    limit: int = 800,
    skip_heartbeats: int = 0,
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
    path = _tenant_file(uid, ".swarm", "runs", run_id, "events.jsonl")
    if path is None or not path.is_file():
        return {"entries": [], "truncated": False}
    raw = await asyncio.to_thread(_obs_tail_lines, path)
    out = []
    for line in raw:
        try:
            e = json.loads(line)
        except Exception:
            continue
        # Heartbeats are ~90% of a long run's event log; filtering BEFORE the
        # limit keeps early task_started/tool events inside the tail window
        # (the laicai gantt needs full-run coverage, not the last N ticks).
        if skip_heartbeats and e.get("type") == "task_heartbeat":
            continue
        out.append(_obs_clip(e))
    return {"entries": out[-limit:], "truncated": len(out) > limit}


# ── Tenant long-term memory (laicai「更多 → 来财AI → 深度引擎记忆」page) ─────
# The engine's remember/auto-recall store lives on the host bind-mount at
# <tenant>/memory/*.md (one markdown file per memory + a MEMORY.md index the
# engine maintains). Host-side read/delete needs no running sandbox. Deleting
# also drops the file's line from MEMORY.md under a bounded flock on the
# engine's .MEMORY.lock (see memory_delete: the lock does not reliably span
# the MicroVM boundary; the engine rebuilds the index from the entry files,
# so index drift heals on its own).

_MEM_FILE_MAX = 64_000


def _mem_dir(uid: str) -> Path:
    return DATA_ROOT / tenant_key(uid) / "memory"


def _safe_mem_name(name: str) -> bool:
    # Filenames are engine-generated slugs (may contain CJK); reject anything
    # that could traverse out of the memory dir instead of whitelisting chars.
    return (
        bool(name)
        and name.endswith(".md")
        and "/" not in name
        and "\\" not in name
        and ".." not in name
        and not name.startswith(".")
    )


@app.get("/memory")
async def memory_list(uid: str, authorization: Optional[str] = Header(None)):
    """List a tenant's long-term memories with full content (files are small,
    KB-scale), newest first. MEMORY.md (the index) is excluded — each file
    carries its own frontmatter title/description."""
    _auth(authorization)
    d = _mem_dir(uid)

    def _read() -> list[dict]:
        if _safe_tenant_path(_tenant_root(uid), d) is None or not d.is_dir():
            return []
        out = []
        for p in d.iterdir():
            # Symlink entries are skipped outright: is_file()
            # follows links, so without this a tenant-planted link read
            # arbitrary host files through the memory page.
            if _safe_tenant_path(d, p) is None:
                continue
            if not p.is_file() or not _safe_mem_name(p.name) or p.name == "MEMORY.md":
                continue
            try:
                st = p.stat()
                text = p.read_text("utf-8", "replace")
            except OSError:
                continue
            out.append({
                "name": p.name,
                "mtime": int(st.st_mtime),
                "size": st.st_size,
                "content": text[:_MEM_FILE_MAX],
                "truncated": len(text) > _MEM_FILE_MAX,
            })
        out.sort(key=lambda e: e["mtime"], reverse=True)
        return out

    return {"files": await asyncio.to_thread(_read)}


# Upper bound on waiting for the memory index lock in /memory/delete.
MEMORY_LOCK_TIMEOUT_S = float(os.environ.get("VIBE_MEMORY_LOCK_TIMEOUT_S", "5"))
_MEMORY_LOCK_RETRY_S = 0.05


def _flock_bounded(fd: int, timeout_s: float) -> bool:
    """Take an exclusive flock on ``fd`` within ``timeout_s`` (non-blocking + retries)."""
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(_MEMORY_LOCK_RETRY_S)


@app.post("/memory/delete")
async def memory_delete(body: dict, authorization: Optional[str] = Header(None)):
    """Permanently delete one memory file and its MEMORY.md index line."""
    _auth(authorization)
    uid = body.get("uid")
    name = body.get("name")
    if not uid:
        raise HTTPException(400, "uid required")
    if not isinstance(name, str) or not _safe_mem_name(name) or name == "MEMORY.md":
        raise HTTPException(400, "invalid name")
    d = _mem_dir(uid)
    if _safe_tenant_path(_tenant_root(uid), d) is None:
        raise HTTPException(404, "memory dir unavailable")
    path = _safe_tenant_path(d, d / name)
    if path is None:
        raise HTTPException(404, "not found")

    def _delete() -> bool:
        # Same lock file the engine takes around every index rewrite
        # (``memory/persistent.py``). flock is only guaranteed to be shared
        # between processes on the same kernel: the engine runs in a MicroVM
        # whose view of this directory is a host mount, so this lock excludes
        # concurrent host-side editors, not necessarily the guest. Taken with
        # a bound (non-blocking + retries) so a lock nobody on this side can
        # release never parks a worker thread; past the bound the delete
        # proceeds unlocked — the engine rebuilds the index from the entry
        # files, so index drift heals itself. Opened O_NOFOLLOW and guarded
        # like every other tenant path.
        lock_path = _safe_tenant_path(d, d / ".MEMORY.lock")
        lock_fd: Optional[int] = None
        if lock_path is not None:
            try:
                lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
                if not _flock_bounded(lock_fd, MEMORY_LOCK_TIMEOUT_S):
                    log.warning("memory/delete tenant %s: index lock busy for %.1fs; editing unlocked",
                                tenant_key(uid)[:8], MEMORY_LOCK_TIMEOUT_S)
                    os.close(lock_fd)
                    lock_fd = None
            except OSError:
                if lock_fd is not None:
                    os.close(lock_fd)
                lock_fd = None
        try:
            existed = path.is_file()
            if existed:
                path.unlink()
            # The index is rewritten in place: refuse when it is (or sits under)
            # a symlink, otherwise root would write through it.
            idx = _safe_tenant_path(d, d / "MEMORY.md")
            if idx is not None and idx.is_file():
                try:
                    lines = idx.read_text("utf-8", "replace").splitlines(keepends=True)
                    kept = [l for l in lines if f"({name})" not in l]
                    if len(kept) != len(lines):
                        tmp = idx.with_name(f".{idx.name}.{os.getpid()}.tmp")
                        tmp.write_text("".join(kept), encoding="utf-8")
                        tmp.replace(idx)
                except OSError:
                    pass  # index cleanup is best-effort; the engine tolerates drift
        finally:
            if lock_fd is not None:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                finally:
                    os.close(lock_fd)
        return existed

    existed = await asyncio.to_thread(_delete)
    # Audit line: the only record of "which memory was removed, for which
    # tenant, when" once the file is gone. The file name is a slug of the
    # memory's title (holdings, tickers…), so only its hash is logged —
    # enough to match a later question about one specific entry.
    log.info("memory/delete tenant %s entry %s existed=%s", tenant_key(uid)[:8],
             hashlib.sha256(name.encode()).hexdigest()[:12], existed)
    return {"ok": True, "deleted": existed}


# ── Tenant disk usage (read-only water-mark exposure) ────────────────────────
# Each tenant's writable data lives under DATA_ROOT/<tenant_key>/. There is no
# filesystem quota: TENANT_QUOTA_BYTES is only the denominator for the usage
# percentage and watermark reported here, so operators see a tenant (or the
# disk) filling up before engine writes start failing mid-attempt.
#
# This is deliberately the READ-ONLY half of the retention design. The actual
# sweeper (deleting old sessions/runs/uploads) is NOT here — deleting user data
# on a schedule is the highest-consequence change in the whole plan, and the
# right order is: expose usage first, watch real numbers for a couple of weeks,
# then decide the retention windows from evidence instead of from a guess.
#
# TODO(retention, after ~2 weeks of /tenants/usage data): add `retention.py`
#   with a 6-hourly sweeper — sessions/ and runs/ evicted by directory mtime
#   (age or count), uploads/ by age, **memory/ never** (it is the user's asset;
#   only the user or /forget removes it). Two hard requirements before it ships:
#     1. `--dry-run` listing reviewed by hand — no active session in it;
#     2. the engine's FTS index (sessions.db) must drop rows for deleted session
#        dirs, or search returns dead links.
#   laicai-bound session ids self-heal: a deleted session makes the engine 404,
#   the router creates a new one and reports the new sid back (already in use).
TENANT_QUOTA_BYTES = int(os.environ.get("VIBE_TENANT_QUOTA_BYTES", str(4 * 1024**3)))
# Warn (log + /healthz flag) at this fraction of the quota.
TENANT_WATERMARK = float(os.environ.get("VIBE_TENANT_WATERMARK", "0.8"))
# `du` over a multi-GB tree is not free, and /healthz is polled. Cache it.
_DU_TTL_S = 300.0
_du_cache: dict[str, tuple[float, int]] = {}


def _dir_bytes(path: Path) -> int:
    """Apparent size of one tenant dir. Returns 0 for a missing/unreadable dir.

    Walks with ``followlinks=False`` and skips symlinked files: a tenant can
    plant ``big -> /`` inside its bind-mount, and a walk that descends into
    symlinked directories would cover the whole host filesystem as root.
    """
    if path.is_symlink() or not path.is_dir():
        return 0
    total = 0
    try:
        for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
            for name in filenames:
                p = Path(dirpath) / name
                try:
                    st = p.lstat()  # never follow: a link's own size is what we count
                except OSError:
                    continue  # racing with the engine writing/rotating files
                if stat_mod.S_ISREG(st.st_mode):
                    total += st.st_size
    except OSError:
        return 0
    return total


def _disk_used_pct(path: Path) -> Optional[float]:
    """Host filesystem usage (percent) of the volume holding ``path``."""
    try:
        du = shutil.disk_usage(path if path.exists() else path.parent)
    except OSError:
        return None
    if not du.total:
        return None
    return round(du.used / du.total * 100, 1)


def _tenant_usage_sync() -> list[dict]:
    """(tk, bytes) for every tenant dir under DATA_ROOT, 5-minute cached."""
    now = time.monotonic()
    out: list[dict] = []
    try:
        dirs = [d for d in DATA_ROOT.iterdir() if d.is_dir() and not d.is_symlink()]
    except OSError as e:
        log.warning("usage: DATA_ROOT unreadable: %s", e)
        return out
    for d in dirs:
        hit = _du_cache.get(d.name)
        if hit and now - hit[0] < _DU_TTL_S:
            size = hit[1]
        else:
            size = _dir_bytes(d)
            _du_cache[d.name] = (now, size)
        out.append(
            {
                "tk8": d.name[:8],
                "disk_bytes": size,
                "quota_bytes": TENANT_QUOTA_BYTES,
                "pct": round(size / TENANT_QUOTA_BYTES * 100, 1) if TENANT_QUOTA_BYTES else 0.0,
                "over_watermark": size > TENANT_QUOTA_BYTES * TENANT_WATERMARK,
            }
        )
    # Drop cache entries for tenants that no longer exist (post-/forget).
    live = {d.name for d in dirs}
    for stale in [k for k in _du_cache if k not in live]:
        _du_cache.pop(stale, None)
    out.sort(key=lambda r: r["disk_bytes"], reverse=True)
    for r in out:
        if r["over_watermark"]:
            log.warning(
                "tenant %s over disk watermark: %d bytes (%.1f%% of quota)",
                r["tk8"], r["disk_bytes"], r["pct"],
            )
    return out


async def tenant_usage() -> list[dict]:
    return await asyncio.to_thread(_tenant_usage_sync)


def _over_watermark_tk8s(rows: list[dict]) -> list[str]:
    """tk8 of every tenant past the disk watermark (largest first)."""
    return [r["tk8"] for r in rows if r.get("over_watermark")]


@app.get("/tenants/usage")
async def tenants_usage(
    limit: int = 20, authorization: Optional[str] = Header(None)
):
    """Top tenants by writable-data size. Read-only; nothing is deleted here."""
    _auth(authorization)
    rows = await tenant_usage()
    return {
        "quota_bytes": TENANT_QUOTA_BYTES,
        "watermark": TENANT_WATERMARK,
        "data_root_bytes": sum(r["disk_bytes"] for r in rows),
        "disk_used_pct": _disk_used_pct(DATA_ROOT),
        "tenants_total": len(rows),
        # tk8 list, not a bare count: a consumer must see WHO is over the
        # line, not just THAT someone is, or nothing can act.
        "over_watermark": _over_watermark_tk8s(rows),
        "tenants": rows[: max(1, min(limit, 200))],
    }


@app.get("/healthz")
async def healthz(authorization: Optional[str] = Header(None)):
    _auth(authorization)
    ok_ms = list(recent_ask_ms)
    # Disk usage per tenant (5-min cached `du`; a cold cache costs one walk).
    # `tenants[]` only lists tenants with a live instance, so the disk totals
    # are computed over DATA_ROOT — paused-and-evicted tenants still occupy it.
    usage = await tenant_usage()
    by_tk8 = {u["tk8"]: u for u in usage}
    return {
        "instances": len(pool),
        "running": sum(1 for i in pool.values() if not i.paused),
        "booting": sum(1 for i in pool.values() if i.booting),
        "active": MAX_CONCURRENT_ACTIVE - active_sem._value,  # noqa: SLF001
        "max_running": MAX_RUNNING,
        "asks": {
            **{k: v for k, v in metrics.items() if k != "started_at"},
            "uptime_s": round(time.time() - metrics["started_at"]),
            "p50_ms": _percentile(ok_ms, 0.50),
            "p95_ms": _percentile(ok_ms, 0.95),
            "window": len(ok_ms),
        },
        "disk": {
            "data_root_bytes": sum(u["disk_bytes"] for u in usage),
            "quota_bytes": TENANT_QUOTA_BYTES,
            "watermark": TENANT_WATERMARK,
            "tenants_total": len(usage),
            # tk8 list of tenants past the watermark: who, not just how
            # many, so an operator / the laicai ops page can act on it.
            "over_watermark": _over_watermark_tk8s(usage),
            # Host filesystem fill level of the volume holding DATA_ROOT —
            # per-tenant quotas are meaningless once the disk itself is full.
            "disk_used_pct": _disk_used_pct(DATA_ROOT),
        },
        "tenants": [
            {
                "tk8": i.tk[:8],
                "sandbox": i.sandbox_id[:12],
                "paused": i.paused,
                "booting": i.booting,
                "refcount": i.refcount,
                "idle_s": round(time.monotonic() - i.last_activity),
                "disk_bytes": (by_tk8.get(i.tk[:8]) or {}).get("disk_bytes", 0),
                "over_watermark": (by_tk8.get(i.tk[:8]) or {}).get("over_watermark", False),
            }
            for i in pool.values()
        ],
    }


# ── Background reaper: pause idle sandboxes ──────────────────────────────────
async def _reap_idle_once() -> list[Instance]:
    """Pause every idle instance past IDLE_TTL_S; returns the ones paused.

    A booting instance is not idle, and neither is one whose lock is held.
    """
    now = time.monotonic()
    victims = [
        i for i in pool.values()
        if i.refcount == 0 and not i.paused and not i.booting
        and not i.lock.locked()
        and (now - i.last_activity) > IDLE_TTL_S
    ]
    paused: list[Instance] = []
    for v in victims:
        log.info("pausing idle tenant %s (idle %ds)", v.tk[:8], round(now - v.last_activity))
        v.paused = True
        if await sbx_pause(v.sandbox_id):
            paused.append(v)
        else:
            v.paused = False  # still running: keep counting it; retried next sweep
    return paused


async def _reaper():
    while True:
        await asyncio.sleep(60)
        await _reap_idle_once()


# ── Startup sweep: destroy stale-template sandboxes, then delete old templates ─
CUBEMASTERCLI = os.environ.get("VIBE_CUBEMASTERCLI", "/usr/local/bin/cubemastercli")
SWEEP_STALE = os.environ.get(
    "VIBE_SWEEP_STALE_TEMPLATES", "1"
).strip().lower() not in {"0", "false", "no"}


def _vibe_template_ids() -> set[str]:
    """Template ids whose image is a vibe-engine build (never touch others)."""
    try:
        out = subprocess.run(
            [CUBEMASTERCLI, "tpl", "list"],
            capture_output=True, text=True, timeout=60,
        ).stdout
    except Exception as e:  # noqa: BLE001 - sweep is best-effort
        log.warning("sweep: tpl list failed: %s", e)
        return set()
    ids: set[str] = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0].startswith("tpl-") and "vibe-engine" in parts[4]:
            ids.add(parts[0])
    return ids


async def _sweep_stale_templates() -> None:
    """One-shot cleanup after a template switch.

    A template switch always involves a router restart with no in-flight asks,
    so any old-template sandbox found here serves nobody. For each sandbox
    whose template is not the current one: running instances are paused first
    (graceful quiesce), then deleted (the delete helper resumes paused ones —
    a CubeAPI quirk); finally every superseded vibe-engine template is removed.
    Covers both state.json tenants and orphans state does not track (a stale
    sandbox can otherwise pin its template for days). Non-vibe templates (e.g. the
    sandbox-code base) are never touched. Disable with
    VIBE_SWEEP_STALE_TEMPLATES=0.
    """
    vibe_tpls = await asyncio.to_thread(_vibe_template_ids)
    doomed: list[str] = []

    # 1. state.json tenants pinned to superseded templates, and forgotten
    #    tenants whose sandbox delete is still pending (their row keeps the
    #    sandbox id until a delete succeeds).
    changed = False
    forgotten_rows: dict[str, str] = {}  # sandbox_id -> tk
    for tk, st in list(state.items()):
        sid = st.get("sandbox_id")
        if not sid:
            continue
        if st.get("forgotten_at"):
            log.info("sweep: forgotten tenant %s still maps sandbox %s", tk[:8], sid[:12])
            doomed.append(sid)
            forgotten_rows[sid] = tk
            continue
        if st.get("template_id") == TEMPLATE_ID:
            continue
        log.info("sweep: tenant %s sandbox %s on stale template %s",
                 tk[:8], sid[:12], st.get("template_id"))
        doomed.append(sid)
        state.pop(tk, None)
        changed = True
    if changed:
        _save_state()

    # 2. Orphan sandboxes unknown to state (only ones built from vibe images).
    try:
        r = await api.get("/sandboxes")
        payload = r.json() if r.status_code == 200 else []
        items = payload if isinstance(payload, list) else (
            payload.get("sandboxes") or payload.get("data") or []
        )
        known = {st.get("sandbox_id") for st in state.values()}
        for s in items:
            sid = s.get("sandboxID") or s.get("sandboxId") or s.get("id")
            tpl = s.get("templateID") or s.get("templateId")
            if not sid or sid in known or sid in doomed:
                continue
            if tpl == TEMPLATE_ID or tpl not in vibe_tpls:
                continue
            log.info("sweep: orphan sandbox %s on stale template %s", sid[:12], tpl)
            doomed.append(sid)
    except Exception as e:  # noqa: BLE001
        log.warning("sweep: sandbox enumeration failed: %s", e)

    # 3. Pause running instances, then destroy.
    for sid in doomed:
        try:
            info = await sbx_info(sid)
            status = str((info or {}).get("status") or (info or {}).get("state") or "").lower()
            if status == "running":
                await sbx_pause(sid)
            if await sbx_delete(sid):
                log.info("sweep: destroyed sandbox %s", sid[:12])
                if sid in forgotten_rows:
                    _drop_state_row(forgotten_rows[sid], sid)
            else:
                log.warning("sweep: destroy %s refused; retried next start", sid[:12])
        except Exception as e:  # noqa: BLE001
            log.warning("sweep: destroy %s failed: %s", sid[:12], e)

    # 4. Delete every superseded vibe-engine template ("still in use" failures
    #    are left for the next sweep once their sandboxes are gone).
    for tpl in sorted(vibe_tpls - {TEMPLATE_ID}):
        try:
            res = await asyncio.to_thread(
                subprocess.run,
                [CUBEMASTERCLI, "tpl", "delete", "--template-id", tpl],
                capture_output=True, text=True, timeout=120,
            )
            msg = (res.stdout + res.stderr).strip().splitlines()
            log.info("sweep: tpl delete %s -> %s", tpl, msg[-1] if msg else res.returncode)
        except Exception as e:  # noqa: BLE001
            log.warning("sweep: tpl delete %s failed: %s", tpl, e)


@app.on_event("startup")
async def _startup():
    global state
    state = _load_state()
    log.info("loaded %d tenant mappings from %s", len(state), STATE_FILE)
    pruned = _prune_tombstones()
    if pruned:
        log.info("pruned %d expired /forget tombstones", pruned)
    _spawn(_reaper())
    if SWEEP_STALE:
        _spawn(_sweep_stale_templates())


@app.on_event("shutdown")
async def _shutdown():
    # Sandboxes survive a router restart by design; just close clients.
    await api.aclose()
    await http.aclose()
