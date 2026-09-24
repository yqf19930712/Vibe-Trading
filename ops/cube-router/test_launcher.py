"""Tests for the in-guest launcher (ops/cube-engine/launcher.py).

Run: python -m pytest test_launcher.py

The launcher runs a real ThreadingHTTPServer on an ephemeral loopback port;
engine spawning is replaced by a recorder, so no `vibe-trading` process and
no ssh tunnel are started.

  · /boot and /stop are open unless the FIRST /boot handed over
    VIBE_LAUNCHER_TOKEN; then both require it as a Bearer header, and an
    authenticated /boot without a token drops the requirement. A launcher
    that started without a token cannot be made to adopt one later (that
    caller could be guest code). /health stays open.
  · The token never reaches the engine env.
  · Engine lifecycle changes (/boot, /stop) are serialized.
"""
from __future__ import annotations

import importlib.util
import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

_LAUNCHER = Path(__file__).resolve().parent.parent / "cube-engine" / "launcher.py"


@pytest.fixture()
def launcher(monkeypatch):
    spec = importlib.util.spec_from_file_location("vibe_launcher_under_test", _LAUNCHER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    booted: list[dict] = []
    state = {"active": 0, "overlap": False}

    def fake_boot(extra_env):
        state["active"] += 1
        if state["active"] > 1:
            state["overlap"] = True
        time.sleep(0.05)
        booted.append(dict(extra_env))
        state["active"] -= 1
        return True, "ok"

    def fake_stop():
        state["active"] += 1
        if state["active"] > 1:
            state["overlap"] = True
        time.sleep(0.02)
        state["active"] -= 1

    monkeypatch.setattr(mod, "_boot_engine", fake_boot)
    monkeypatch.setattr(mod, "_stop_engine", fake_stop)
    monkeypatch.setattr(mod, "_ensure_tunnel", lambda: None)
    server = ThreadingHTTPServer(("127.0.0.1", 0), mod.Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield mod, base, booted, state
    finally:
        server.shutdown()
        server.server_close()


def _call(base, method, path, body=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_open_until_a_token_is_handed_over(launcher):
    mod, base, booted, _ = launcher
    assert _call(base, "POST", "/stop")[0] == 200
    assert _call(base, "POST", "/boot", {"env": {"A": "1"}})[0] == 200
    assert _call(base, "POST", "/stop")[0] == 200  # still no token adopted
    assert booted == [{"A": "1"}]


def test_adopted_token_is_required_and_never_reaches_the_engine(launcher):
    mod, base, booted, _ = launcher
    env = {"A": "1", "VIBE_LAUNCHER_AUTH": "1", "VIBE_LAUNCHER_TOKEN": "tok-123"}
    assert _call(base, "POST", "/boot", {"env": env}, token="anything")[0] == 200
    assert booted == [{"A": "1"}]

    # Guest shell over loopback, no / wrong token: refused.
    assert _call(base, "POST", "/stop")[0] == 401
    assert _call(base, "POST", "/stop", token="guess")[0] == 401
    assert _call(base, "POST", "/boot", {"env": {"EVIL": "1"}})[0] == 401
    assert len(booted) == 1
    # Health stays open for the template probe.
    assert _call(base, "GET", "/health")[0] == 200
    # The router, with the token.
    assert _call(base, "POST", "/stop", token="tok-123")[0] == 200
    assert _call(base, "POST", "/boot", {"env": {**env, "B": "2"}}, token="tok-123")[0] == 200
    assert booted[-1] == {"A": "1", "B": "2"}


def test_a_token_offered_after_the_first_boot_is_ignored(launcher):
    mod, base, booted, _ = launcher
    assert _call(base, "POST", "/boot", {"env": {"A": "1"}})[0] == 200
    # Guest code trying to lock the router out of its own launcher.
    assert _call(base, "POST", "/boot", {"env": {"VIBE_LAUNCHER_TOKEN": "evil"}})[0] == 200
    assert _call(base, "POST", "/stop")[0] == 200
    assert booted[-1] == {}  # the key was still stripped from the engine env


def test_authenticated_boot_without_token_drops_the_requirement(launcher):
    mod, base, booted, _ = launcher
    _call(base, "POST", "/boot", {"env": {"VIBE_LAUNCHER_TOKEN": "tok"}})
    assert _call(base, "POST", "/stop")[0] == 401
    assert _call(base, "POST", "/boot", {"env": {"A": "1"}}, token="tok")[0] == 200
    assert _call(base, "POST", "/stop")[0] == 200


def test_boot_and_stop_do_not_interleave(launcher):
    mod, base, booted, state = launcher
    threads = [
        threading.Thread(target=_call, args=(base, "POST", "/boot", {"env": {"N": str(i)}}))
        for i in range(3)
    ] + [threading.Thread(target=_call, args=(base, "POST", "/stop"))]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert len(booted) == 3
    assert state["overlap"] is False
