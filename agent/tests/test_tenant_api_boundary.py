"""Hosted-tenant API boundary: no loopback trust, closed control-plane
entry points, and a default deadline for attempts started without one.

Inside a tenant MicroVM the only loopback callers are the model's own shell
subprocesses, so the engine must not treat them as an authenticated
operator (they could otherwise start attempts / swarm runs outside the
router's budget and metering, or reach the money endpoints).
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

import api_server


def _local() -> TestClient:
    return TestClient(api_server.app, client=("127.0.0.1", 50000))


def _remote() -> TestClient:
    return TestClient(api_server.app, client=("203.0.113.10", 50000))


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "API_AUTH_KEY",
        "VIBE_MULTITENANT",
        "VIBE_TRADING_TENANT_SAFE",
        "VIBE_DEFAULT_DEADLINE_S",
        "VIBE_TRADING_TRUST_DOCKER_LOOPBACK",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(api_server, "_API_KEY", "")


def _multitenant(monkeypatch: pytest.MonkeyPatch, key: str | None = "k3y") -> None:
    monkeypatch.setenv("VIBE_MULTITENANT", "1")
    monkeypatch.setenv("VIBE_TRADING_TENANT_SAFE", "1")
    if key:
        monkeypatch.setenv("API_AUTH_KEY", key)
        monkeypatch.setattr(api_server, "_API_KEY", key)


# ── loopback trust ───────────────────────────────────────────────────────────


def test_multitenant_loopback_without_key_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _multitenant(monkeypatch)
    for path in ("/runs", "/sessions", "/swarm/runs"):
        assert _local().get(path).status_code == 401, path


def test_multitenant_loopback_with_bearer_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    _multitenant(monkeypatch)
    resp = _local().get("/runs", headers={"Authorization": "Bearer k3y"})
    assert resp.status_code == 200


def test_multitenant_without_configured_key_refuses_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    _multitenant(monkeypatch, key=None)
    resp = _local().get("/runs")
    assert resp.status_code == 403
    assert "API_AUTH_KEY" in resp.json()["detail"]


def test_multitenant_loopback_cannot_open_the_event_stream_without_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _multitenant(monkeypatch)
    resp = _local().get("/sessions/abcdef012345/events")
    assert resp.status_code == 401


def test_multitenant_settings_read_requires_key_even_on_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _multitenant(monkeypatch)
    assert _local().get("/settings/llm").status_code == 401


def test_health_stays_open_for_the_launcher_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    _multitenant(monkeypatch)
    assert _local().get("/health").status_code == 200


def test_single_user_loopback_trust_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_AUTH_KEY", "secret")
    monkeypatch.setattr(api_server, "_API_KEY", "secret")
    assert _local().get("/runs").status_code == 200
    assert _remote().get("/runs").status_code == 401


# ── control-plane entry points closed for hosted tenants ─────────────────────


@pytest.mark.parametrize(
    "path,body",
    [
        ("/swarm/runs", {"preset_name": "investment_committee", "user_vars": {}}),
        ("/swarm/runs/abcdef012345/retry", {}),
        ("/mandate/commit", {}),
        ("/live/halt", {}),
        ("/live/resume", {}),
        ("/live/authorize", {}),
        ("/live/runner/start", {}),
        ("/live/runner/stop", {}),
    ],
)
def test_tenant_profile_closes_swarm_and_money_endpoints(
    monkeypatch: pytest.MonkeyPatch, path: str, body: dict
) -> None:
    _multitenant(monkeypatch)
    resp = _remote().post(path, json=body, headers={"Authorization": "Bearer k3y"})
    assert resp.status_code == 403, (path, resp.status_code, resp.text[:200])
    assert "hosted tenants" in resp.json()["detail"]


def test_tenant_safe_alone_also_closes_them(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIBE_TRADING_TENANT_SAFE", "1")
    resp = _local().post("/mandate/commit", json={})
    assert resp.status_code == 403


def test_swarm_start_still_open_for_single_user(monkeypatch: pytest.MonkeyPatch) -> None:
    resp = _local().post("/swarm/runs", json={})
    # Reaches the handler (validation error on the empty body), not the gate.
    assert resp.status_code != 403


# ── default deadline ─────────────────────────────────────────────────────────


class _RecordingService:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def send_message(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"message_id": "m", "attempt_id": "a"}


def _send(monkeypatch: pytest.MonkeyPatch, body: dict, headers: dict | None = None) -> _RecordingService:
    svc = _RecordingService()
    monkeypatch.setattr(api_server, "_get_session_service", lambda: svc)
    resp = _remote().post("/sessions/abcdef012345/messages", json=body, headers=headers or {})
    assert resp.status_code == 200, resp.text
    return svc


def test_tenant_attempt_without_deadline_gets_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    _multitenant(monkeypatch)
    svc = _send(monkeypatch, {"content": "hi"}, {"Authorization": "Bearer k3y"})
    assert svc.calls[0]["deadline_s"] == 900.0


def test_explicit_deadline_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    _multitenant(monkeypatch)
    svc = _send(monkeypatch, {"content": "hi", "deadline_s": 120}, {"Authorization": "Bearer k3y"})
    assert svc.calls[0]["deadline_s"] == 120


def test_default_deadline_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    _multitenant(monkeypatch)
    monkeypatch.setenv("VIBE_DEFAULT_DEADLINE_S", "300")
    svc = _send(monkeypatch, {"content": "hi"}, {"Authorization": "Bearer k3y"})
    assert svc.calls[0]["deadline_s"] == 300.0


def test_single_user_attempt_keeps_no_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_AUTH_KEY", "secret")
    monkeypatch.setattr(api_server, "_API_KEY", "secret")
    svc = _send(monkeypatch, {"content": "hi"}, {"Authorization": "Bearer secret"})
    assert svc.calls[0]["deadline_s"] is None


# ── /proc hardening ──────────────────────────────────────────────────────────


def test_hardening_marks_process_non_dumpable_on_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    import ctypes

    calls: list[tuple] = []

    class _Libc:
        def prctl(self, *args: Any) -> int:
            calls.append(args)
            return 0

    monkeypatch.setenv("VIBE_MULTITENANT", "1")
    monkeypatch.setattr(api_server._sys, "platform", "linux")
    monkeypatch.setattr(ctypes, "CDLL", lambda *a, **k: _Libc())
    api_server._harden_tenant_process()
    assert calls == [(4, 0, 0, 0, 0)]


def test_hardening_is_a_noop_outside_multitenant(monkeypatch: pytest.MonkeyPatch) -> None:
    import ctypes

    monkeypatch.setattr(api_server._sys, "platform", "linux")
    monkeypatch.setattr(
        ctypes, "CDLL", lambda *a, **k: (_ for _ in ()).throw(AssertionError("called"))
    )
    api_server._harden_tenant_process()
