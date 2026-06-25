# vibe-router — provisioning & rollout runbook

Multi-tenant orchestrator for Vibe-Trading. Design: `../../MULTI_TENANCY.md` (v2).
This is the **production runbook** for standing it up on the VPS. Every step that
mutates the production box is listed so it can be reviewed before running.

## What it does
Gives each laicai user an **isolated** Vibe instance (own `HOME` → own
memory/sessions/search/oauth/goals; own process → own RAM singletons), lazily
started, reused per-thread for continuity, idle-reaped, cgroup-capped. laicai
calls only `POST /ask` on the router; the router does session reuse + attempt_id
polling.

## Prereqs on the box
- The Vibe agent installed (it already is: `vibe-trading.service`) so the
  `vibe-trading` CLI is on PATH for the spawned instances.
- `systemd-run` available (default on the VPS) for per-instance cgroup caps.
- Python 3.11 for the router venv.

## Provisioning (run as root; review first)
```bash
# 1) dedicated user + data root (per-tenant homes live here)
useradd --system --create-home --home-dir /home/vibe --shell /usr/sbin/nologin vibe || true
install -d -o vibe -g vibe -m 700 /srv/vibe /srv/vibe/users
install -d -o vibe -g vibe -m 755 /srv/vibe/mplcache       # shared read-only matplotlib font cache (m1)

# 2) router code + venv (built from the box's mature 3.12 base, not system 3.14,
#    so fastapi/pydantic/psutil wheels resolve cleanly)
install -d -o vibe -g vibe /opt/invest/vibe-router
cp /opt/invest/vibe-router-src/router.py /opt/invest/vibe-router-src/requirements.txt /opt/invest/vibe-router/
/opt/vibe-trading/.venv/bin/python -m venv /opt/invest/vibe-venv
/opt/invest/vibe-venv/bin/pip install -q -r /opt/invest/vibe-router/requirements.txt
chown -R vibe:vibe /opt/invest/vibe-router /opt/invest/vibe-venv

# 3) env file (chmod 600, owned by vibe) — generate secrets ONCE
#    ROUTER_SECRET is a SCHEMA KEY (names every tenant dir) — never rotate.
umask 077
cat > /opt/invest/vibe-router.env <<EOF
VIBE_ROUTER_SECRET=$(openssl rand -hex 32)
VIBE_ROUTER_TOKEN=$(openssl rand -hex 32)
VIBE_USERS_BASE=/srv/vibe/users
# Full path — the installed CLI is in the agent venv, not on the vibe user's PATH.
VIBE_BIN=/opt/vibe-trading/.venv/bin/vibe-trading
VIBE_MAX_INSTANCES=4
VIBE_MAX_CONCURRENT_ACTIVE=2
VIBE_IDLE_TTL_S=1200
MPLCONFIGDIR=/srv/vibe/mplcache
EOF
# Reuse the box's existing Vibe LLM creds (Anthropic-compat proxy) — don't retype secrets:
grep -E '^(LANGCHAIN_|OPENAI_|TUSHARE_)' /opt/vibe-trading/agent/.env >> /opt/invest/vibe-router.env
chown vibe:vibe /opt/invest/vibe-router.env
# The vibe user must be able to execute the agent venv:
chmod -R a+rX /opt/vibe-trading/.venv /opt/vibe-trading/agent 2>/dev/null || true
# (Per-instance cgroup caps need root; phase-1 router runs as non-root vibe and
#  relies on the unit-level MemoryMax=5G in vibe-router.service to cap the pool.)

# 4) systemd unit
cp vibe-router.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now vibe-router
systemctl status vibe-router --no-pager
curl -s -H "Authorization: Bearer $(grep VIBE_ROUTER_TOKEN /opt/invest/vibe-router.env|cut -d= -f2)" \
     http://127.0.0.1:8990/healthz | jq .   # expect {"instances":0,...}

# 5) firewall the instance port range to loopback only (belt-and-suspenders, M1)
#    (instances already bind 127.0.0.1; add a drop rule for 8901-8949 from non-loopback)
```

## Wire laicai (in `/opt/invest/web.env`)
```bash
VIBE_ROUTER_URL=http://127.0.0.1:8990
VIBE_ROUTER_TOKEN=<same as router env>
# keep VIBE_API_URL=http://127.0.0.1:8899 for rollback (laicai prefers the router when both set)
```
Then apply the DB migration (`chat_threads.vibe_session_id`, additive/nullable) and deploy laicai.

## Smoke test (after wiring)
```bash
TOKEN=$(grep VIBE_ROUTER_TOKEN /opt/invest/vibe-router.env|cut -d= -f2)
curl -s -XPOST http://127.0.0.1:8990/ask -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"uid":"smoke-user","query":"用一句话说今天是几号","threadId":"t1","timeoutS":120}' | jq .
# → {"answer":"...","vibeSessionId":"..."}; /healthz now shows 1 instance.
# Continuity: POST again with the returned vibeSessionId → engine remembers prior turn.
# Isolation: a second uid gets a different /srv/vibe/users/<hmac> dir; session_search/run_swarm absent.
```

## Rollback
Set laicai `VIBE_ROUTER_URL` empty (keeps `VIBE_API_URL`) → laicai talks to the old
single shared instance again. `systemctl stop vibe-router` to free the pool.
The legacy single instance must NOT have `VIBE_MULTITENANT=1` (it would fail loud).

## Acceptance (maps to MULTI_TENANCY.md §11)
isolation (A's memory invisible to B) · continuity (turn2 ≠ turn1; cross-thread long-term recall) ·
tools (run_swarm/trading_*/session_search absent under tenant-safe) · resource (cgroup cap, no OOM,
in-flight not reaped) · failure (instance restart, router restart reaps orphans) · forget (dir removed).
