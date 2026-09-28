"""VIBE_LAUNCHER_AUTH=1 end to end: router ↔ real launcher over HTTP.

Run: python -m pytest test_launcher_auth_chain.py

Every sandbox gets its own freshly loaded ``ops/cube-engine/launcher.py``
serving on a loopback port (engine spawn replaced by a recorder); CubeAPI is
an in-memory fake (create / info / pause / resume / delete). The router's own
code drives the lifecycle, so the chain checked is the one production runs:

  · a new sandbox's first /boot hands over the token; guest code on loopback
    is refused from then on;
  · pause → resume keeps the launcher's token, and the router still boots it;
  · a router restart re-attaches from state.json and derives the same token;
  · a template switch rebuilds the sandbox, whose launcher adopts its own;
  · /forget deletes the sandbox without touching the launcher;
  · switching the flag off makes the next /boot drop the requirement (the
    rollback step), and a router that sends no token is refused (why a
    rollback must switch the flag off first).
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

os.environ.setdefault("VIBE_ROUTER_SECRET", "test-secret")
os.environ.setdefault("VIBE_ROUTER_TOKEN", "test-token")
os.environ.setdefault("VIBE_CUBE_TEMPLATE_ID", "tpl-test")

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

import router  # noqa: E402

_LAUNCHER = Path(__file__).resolve().parent.parent / "cube-engine" / "launcher.py"
AUTH = f"Bearer {os.environ['VIBE_ROUTER_TOKEN']}"
UID = "user-launcher-auth"


class _Guest:
    """One sandbox: its own launcher module + HTTP server, and a paused flag."""

    def __init__(self, sandbox_id: str, template_id: str) -> None:
        spec = importlib.util.spec_from_file_location(f"launcher_{sandbox_id}", _LAUNCHER)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.mod = mod
        self.template_id = template_id
        self.paused = False
        self.boots: list[dict] = []
        engine = {"up": False}

        def fake_boot(extra_env):
            self.boots.append(dict(extra_env))
            engine["up"] = True
            return True, "ok"

        mod._boot_engine = fake_boot
        mod._stop_engine = lambda: engine.update(up=False)
        mod._ensure_tunnel = lambda: None
        mod._engine_alive = lambda: engine["up"]
        mod._engine_healthy = lambda: engine["up"]
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), mod.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def guest_call(self, path: str, body: dict | None = None) -> int:
        """A caller inside the guest (the engine's shell tools): no token."""
        data = json.dumps(body or {}).encode()
        req = urllib.request.Request(self.base + path, data=data, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code


class _Cube:
    """In-memory CubeAPI."""

    def __init__(self) -> None:
        self.guests: dict[str, _Guest] = {}
        self.deleted: list[str] = []
        self._n = 0

    async def create(self, tk: str) -> str:
        self._n += 1
        sid = f"sbx-{self._n:04d}"
        self.guests[sid] = _Guest(sid, router.TEMPLATE_ID)
        return sid

    async def info(self, sid: str):
        g = self.guests.get(sid)
        return None if g is None else {"sandboxID": sid, "templateID": g.template_id}

    async def pause(self, sid: str) -> bool:
        self.guests[sid].paused = True
        return True

    async def resume(self, sid: str) -> bool:
        if sid in self.guests:
            self.guests[sid].paused = False
        return True

    async def delete(self, sid: str) -> bool:
        g = self.guests.pop(sid, None)
        if g is not None:
            g.close()
            self.deleted.append(sid)
        return True

    def url(self, sid: str, port: int) -> str:
        g = self.guests.get(sid)
        if g is None or g.paused or port != router.LAUNCHER_PORT:
            return "http://127.0.0.1:9"  # discard port: unreachable
        return g.base

    def close(self) -> None:
        for g in self.guests.values():
            g.close()


@pytest.fixture()
def chain(monkeypatch, tmp_path):
    cube = _Cube()
    router.pool.clear()
    router.state.clear()
    router.uid_locks.clear()
    monkeypatch.setattr(router, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(router, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(router, "pool_mutex", asyncio.Lock())
    monkeypatch.setattr(router, "capacity_lock", asyncio.Lock())
    monkeypatch.setattr(router, "LAUNCHER_AUTH", True)
    monkeypatch.setattr(router, "sbx_create", cube.create)
    monkeypatch.setattr(router, "sbx_info", cube.info)
    monkeypatch.setattr(router, "sbx_pause", cube.pause)
    monkeypatch.setattr(router, "sbx_resume", cube.resume)
    monkeypatch.setattr(router, "sbx_delete", cube.delete)
    monkeypatch.setattr(router, "guest_url", cube.url)
    monkeypatch.setattr(router.asyncio, "sleep", _no_sleep)
    yield cube
    cube.close()
    router.pool.clear()
    router.state.clear()
    router.uid_locks.clear()


_real_sleep = asyncio.sleep


async def _no_sleep(_s: float, *a, **kw) -> None:
    await _real_sleep(0)


def _restart_router(monkeypatch, template_id=None) -> None:
    """What a router restart leaves: no pool, the state reloaded from disk.

    A template switch is a router.env edit, so it always comes with one.
    """
    if template_id is not None:
        monkeypatch.setattr(router, "TEMPLATE_ID", template_id)
    router.pool.clear()
    router.state.clear()
    router.state.update(router._load_state())


def _drive(monkeypatch, steps):
    """Run ``steps(tk)`` on one loop with a live httpx client for the router."""

    async def main():
        async with httpx.AsyncClient(trust_env=False) as client:
            monkeypatch.setattr(router, "http", client)
            return await steps(router.tenant_key(UID))

    return asyncio.run(main())


def test_new_sandbox_adopts_the_token_and_guest_code_is_refused(chain, monkeypatch):
    async def steps(tk):
        return await router.get_or_create(tk)

    inst = _drive(monkeypatch, steps)
    guest = chain.guests[inst.sandbox_id]

    assert guest.mod._auth["token"] == router.launcher_token(inst.sandbox_id)
    assert "VIBE_LAUNCHER_TOKEN" not in guest.boots[0]  # never reaches the engine
    assert guest.guest_call("/stop") == 401
    assert guest.guest_call("/boot", {"env": {"API_AUTH_KEY": "mine"}}) == 401
    assert len(guest.boots) == 1


def test_pause_resume_restart_template_switch_and_forget(chain, monkeypatch):
    async def steps(tk):
        seen = {}
        inst = await router.get_or_create(tk)
        first = inst.sandbox_id
        seen["first"] = first

        # LRU pause → resume on the next ask; then a model switch re-boots
        # the resumed launcher, which still holds its token.
        await router.sbx_pause(first)
        inst.paused = True
        await router.get_or_create(tk)
        await router.get_or_create(tk, model="model-b")
        seen["after_resume_boots"] = len(chain.guests[first].boots)

        # Router restart: the pool is gone, state.json re-attaches the
        # sandbox; the token is derived again from the secret and the id.
        _restart_router(monkeypatch)
        again = await router.get_or_create(tk, model="model-c")
        seen["reattached_same_sandbox"] = again.sandbox_id == first
        seen["after_restart_boots"] = len(chain.guests[first].boots)

        # Template switch: rebuilt sandbox, fresh launcher adopts its own token.
        _restart_router(monkeypatch, "tpl-next")
        rebuilt = await router.get_or_create(tk, model="model-c")
        seen["rebuilt"] = rebuilt.sandbox_id

        # /forget: sandbox gone, the launcher is never called.
        boots_before = len(chain.guests[rebuilt.sandbox_id].boots)
        out = await router.forget({"uid": UID}, authorization=AUTH)
        seen["forget"] = out
        seen["forget_boots"] = boots_before
        return seen

    seen = _drive(monkeypatch, steps)

    assert seen["after_resume_boots"] == 2
    assert seen["reattached_same_sandbox"] and seen["after_restart_boots"] == 3
    first, rebuilt = seen["first"], seen["rebuilt"]
    assert rebuilt != first and first in chain.deleted
    assert seen["forget"] == {"ok": True}
    assert rebuilt in chain.deleted


def test_rebuilt_sandbox_has_its_own_token(chain, monkeypatch):
    async def steps(tk):
        a = await router.get_or_create(tk)
        token_a = chain.guests[a.sandbox_id].mod._auth["token"]
        _restart_router(monkeypatch, "tpl-next")
        b = await router.get_or_create(tk)
        return token_a, b

    token_a, b = _drive(monkeypatch, steps)
    guest_b = chain.guests[b.sandbox_id]
    assert guest_b.mod._auth["token"] == router.launcher_token(b.sandbox_id) != token_a
    assert guest_b.guest_call("/stop") == 401


def test_switching_the_flag_off_drops_the_requirement(chain, monkeypatch):
    async def steps(tk):
        inst = await router.get_or_create(tk)
        monkeypatch.setattr(router, "LAUNCHER_AUTH", False)
        # The flag is in the fingerprint: the next ask re-boots, authenticated
        # by the header, with no token in the env.
        await router.get_or_create(tk)
        return inst

    inst = _drive(monkeypatch, steps)
    guest = chain.guests[inst.sandbox_id]
    assert len(guest.boots) == 2
    assert guest.mod._auth["token"] is None
    assert guest.guest_call("/stop") == 200


def test_a_router_without_the_token_cannot_boot_a_tokened_launcher(chain, monkeypatch):
    async def steps(tk):
        inst = await router.get_or_create(tk)
        # An older router build (no Authorization header) or a changed
        # VIBE_ROUTER_SECRET: the launcher does not get its token.
        monkeypatch.setattr(router, "launcher_token", lambda sid: "not-the-token")
        try:
            await router.get_or_create(tk, model="model-b")
        except HTTPException as exc:
            return inst, exc
        return inst, None

    inst, exc = _drive(monkeypatch, steps)
    assert exc is not None and exc.status_code == 502 and "401" in str(exc.detail)
    assert len(chain.guests[inst.sandbox_id].boots) == 1


def test_startup_warns_when_multitenant_runs_without_launcher_auth(monkeypatch):
    monkeypatch.setattr(router, "LAUNCHER_AUTH", False)
    msg = router._launcher_auth_warning()
    assert msg and "VIBE_LAUNCHER_AUTH" in msg
    monkeypatch.setattr(router, "LAUNCHER_AUTH", True)
    assert router._launcher_auth_warning() is None
