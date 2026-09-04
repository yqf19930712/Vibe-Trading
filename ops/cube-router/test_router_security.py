"""Pure-logic tests for cube-router's review-3 remediation (2026-09-04).

Run: VIBE_ROUTER_SECRET=x VIBE_ROUTER_TOKEN=y VIBE_CUBE_TEMPLATE_ID=tpl-test \
     python -m pytest test_router_security.py

Covered (no CubeAPI, no sandbox — endpoints are awaited directly):
  · A1  tenant symlink escape: /memory list / delete, /obs/*, _dir_bytes
  · A4  a failed engine attempt is an error frame, never an answer frame
  · A6  engine 422 (input length cap) becomes a readable 400 detail
  · A7  /forget answers ok=false when the sandbox or the dir is not gone
  · A10 POST /sessions/delete offline mode + watermark/disk fields
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

os.environ.setdefault("VIBE_ROUTER_SECRET", "test-secret")
os.environ.setdefault("VIBE_ROUTER_TOKEN", "test-token")
os.environ.setdefault("VIBE_CUBE_TEMPLATE_ID", "tpl-test")

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

import router  # noqa: E402

AUTH = f"Bearer {os.environ['VIBE_ROUTER_TOKEN']}"
UID = "user-1"


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def tenant(tmp_path, monkeypatch) -> Path:
    """DATA_ROOT under tmp; returns the tenant dir for UID."""
    monkeypatch.setattr(router, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(router, "STATE_FILE", tmp_path / "state.json")
    router._du_cache.clear()
    d = tmp_path / router.tenant_key(UID)
    d.mkdir()
    return d


# ── A1: symlink escape ───────────────────────────────────────────────────────


class TestSafeTenantPath:
    def test_plain_child_is_accepted(self, tmp_path):
        f = tmp_path / "a" / "b.txt"
        f.parent.mkdir()
        f.write_text("x")
        assert router._safe_tenant_path(tmp_path, f) == f

    def test_symlink_entry_is_rejected(self, tmp_path):
        outside = tmp_path.parent / "outside.txt"
        outside.write_text("secret")
        link = tmp_path / "link.txt"
        link.symlink_to(outside)
        assert router._safe_tenant_path(tmp_path, link) is None

    def test_symlinked_parent_dir_is_rejected(self, tmp_path):
        outside_dir = tmp_path.parent / "outside-dir"
        outside_dir.mkdir(exist_ok=True)
        (outside_dir / "trace.jsonl").write_text("{}")
        (tmp_path / "sessions").mkdir()
        (tmp_path / "sessions" / "s1").symlink_to(outside_dir)
        assert router._safe_tenant_path(tmp_path, tmp_path / "sessions" / "s1" / "trace.jsonl") is None

    def test_nonexistent_inside_path_is_fine(self, tmp_path):
        p = tmp_path / "not-yet"
        assert router._safe_tenant_path(tmp_path, p) == p


class TestMemoryEndpointsRefuseSymlinks:
    def test_list_skips_symlinked_entry(self, tenant, tmp_path):
        mem = tenant / "memory"
        mem.mkdir()
        (mem / "user_real.md").write_text("---\nname: real\n---\nbody", encoding="utf-8")
        host_file = tmp_path.parent / "host-secret.md"
        host_file.write_text("ROOT ONLY", encoding="utf-8")
        (mem / "user_evil.md").symlink_to(host_file)

        rows = _run(router.memory_list(UID, authorization=AUTH))["files"]

        names = {r["name"] for r in rows}
        assert "user_real.md" in names
        assert "user_evil.md" not in names
        assert not any("ROOT ONLY" in json.dumps(r) for r in rows)

    def test_list_returns_empty_when_memory_dir_is_a_symlink(self, tenant, tmp_path):
        host_dir = tmp_path.parent / "host-memory"
        host_dir.mkdir(exist_ok=True)
        (host_dir / "user_x.md").write_text("---\nname: x\n---\nleak", encoding="utf-8")
        (tenant / "memory").symlink_to(host_dir)
        assert _run(router.memory_list(UID, authorization=AUTH)) == {"files": []}

    def test_delete_refuses_symlinked_entry_and_index(self, tenant, tmp_path):
        mem = tenant / "memory"
        mem.mkdir()
        host_file = tmp_path.parent / "host-target.md"
        host_file.write_text("keep me", encoding="utf-8")
        (mem / "user_evil.md").symlink_to(host_file)
        # Index symlinked to a host file: the rewrite must NOT go through it.
        host_idx = tmp_path.parent / "authorized_keys"
        host_idx.write_text("- [x](user_evil.md) — line\n", encoding="utf-8")
        (mem / "MEMORY.md").symlink_to(host_idx)

        with pytest.raises(HTTPException) as ei:
            _run(router.memory_delete({"uid": UID, "name": "user_evil.md"}, authorization=AUTH))
        assert ei.value.status_code == 404
        assert host_file.read_text(encoding="utf-8") == "keep me"
        assert host_idx.read_text(encoding="utf-8") == "- [x](user_evil.md) — line\n"

    def test_delete_of_real_entry_rewrites_index_but_not_a_symlinked_one(self, tenant, tmp_path):
        mem = tenant / "memory"
        mem.mkdir()
        (mem / "user_gone.md").write_text("---\nname: gone\n---\nbody", encoding="utf-8")
        host_idx = tmp_path.parent / "host-index.md"
        host_idx.write_text("- [gone](user_gone.md) — d\n", encoding="utf-8")
        (mem / "MEMORY.md").symlink_to(host_idx)

        out = _run(router.memory_delete({"uid": UID, "name": "user_gone.md"}, authorization=AUTH))

        assert out["deleted"] is True
        assert not (mem / "user_gone.md").exists()
        # The symlinked index was left alone (root must not write through it).
        assert host_idx.read_text(encoding="utf-8") == "- [gone](user_gone.md) — d\n"


class TestObsEndpointsRefuseSymlinks:
    def test_engine_log_symlink_is_empty(self, tenant, tmp_path):
        host_log = tmp_path.parent / "host.jsonl"
        host_log.write_text(json.dumps({"ts": 1, "msg": "HOST"}) + "\n", encoding="utf-8")
        (tenant / "logs").mkdir()
        (tenant / "logs" / "engine.jsonl").symlink_to(host_log)
        out = _run(router.obs_engine_log(UID, authorization=AUTH))
        assert out == {"lines": [], "truncated": False}

    def test_trace_under_symlinked_session_dir_is_empty(self, tenant, tmp_path):
        host_dir = tmp_path.parent / "host-sess"
        host_dir.mkdir(exist_ok=True)
        (host_dir / "trace.jsonl").write_text(json.dumps({"type": "x"}) + "\n", encoding="utf-8")
        (tenant / "sessions").mkdir()
        (tenant / "sessions" / "sess1234").symlink_to(host_dir)
        out = _run(router.obs_trace(UID, "sess1234", authorization=AUTH))
        assert out == {"entries": [], "truncated": False}
        prompt = _run(router.obs_prompt(UID, "sess1234", authorization=AUTH))
        assert prompt == {"starts": []}

    def test_swarm_events_symlink_is_empty(self, tenant, tmp_path):
        host_ev = tmp_path.parent / "host-events.jsonl"
        host_ev.write_text(json.dumps({"type": "e"}) + "\n", encoding="utf-8")
        run_dir = tenant / ".swarm" / "runs" / "run12345"
        run_dir.mkdir(parents=True)
        (run_dir / "events.jsonl").symlink_to(host_ev)
        out = _run(router.obs_swarm_events(UID, "run12345", authorization=AUTH))
        assert out == {"entries": [], "truncated": False}


class TestDirBytesDoesNotFollowLinks:
    def test_symlinked_dir_and_file_are_not_counted(self, tmp_path):
        tenant = tmp_path / "tk"
        tenant.mkdir()
        (tenant / "own.bin").write_bytes(b"x" * 100)
        host_dir = tmp_path / "host"
        host_dir.mkdir()
        (host_dir / "huge.bin").write_bytes(b"y" * 10_000)
        (tenant / "escape").symlink_to(host_dir)
        (tenant / "escape.bin").symlink_to(host_dir / "huge.bin")
        assert router._dir_bytes(tenant) == 100

    def test_symlinked_tenant_root_is_zero(self, tmp_path):
        host_dir = tmp_path / "host"
        host_dir.mkdir()
        (host_dir / "f").write_bytes(b"z" * 50)
        (tmp_path / "tk").symlink_to(host_dir)
        assert router._dir_bytes(tmp_path / "tk") == 0


# ── A4: failed attempt → error frame ─────────────────────────────────────────


class TestFailedAttemptClassification:
    def test_ok_false_metadata_is_failed(self):
        msg = {
            "role": "assistant", "content": "Execution failed: boom",
            "linked_attempt_id": "a1", "metadata": {"ok": False, "error": "boom"},
        }
        assert router._classify_answer_message(msg, "a1") == ("failed", "boom")

    def test_legacy_status_failed_is_honoured(self):
        msg = {
            "role": "assistant", "content": "Execution failed: x",
            "linked_attempt_id": "a1", "metadata": {"status": "failed"},
        }
        kind, text = router._classify_answer_message(msg, "a1")
        assert kind == "failed"
        assert text == "Execution failed: x"

    def test_completed_message_is_an_answer(self):
        msg = {
            "role": "assistant", "content": "the answer",
            "linked_attempt_id": "a1", "metadata": {"ok": True, "status": "completed"},
        }
        assert router._classify_answer_message(msg, "a1") == ("answer", "the answer")

    def test_other_attempt_and_non_assistant_are_skipped(self):
        assert router._classify_answer_message(
            {"role": "user", "content": "q"}, "a1")[0] == "skip"
        assert router._classify_answer_message(
            {"role": "assistant", "content": "old", "linked_attempt_id": "a0"}, "a1")[0] == "skip"

    def test_classify_status_maps_engine_failed(self):
        assert router._classify_status(502, router._EngineFailed("x")) == "engine_failed"
        assert router._classify_status(502, HTTPException(502, "u")) == "upstream_failed"
        assert router._classify_status(504) == "timeout"

    def test_wait_answer_raises_engine_failed(self, monkeypatch):
        monkeypatch.setattr(router, "POLL_INTERVAL_S", 0.0)

        class _Resp:
            status_code = 200

            def json(self):
                return [
                    {"role": "user", "content": "q"},
                    {"role": "assistant", "content": "Execution failed: boom",
                     "linked_attempt_id": "a1", "metadata": {"ok": False, "error": "boom"}},
                ]

        async def _fake_vibe(inst, method, path, **kw):
            return _Resp()

        monkeypatch.setattr(router, "_vibe", _fake_vibe)
        inst = router.Instance("tk", "sbx", None, "key")
        with pytest.raises(router._EngineFailed) as ei:
            _run(router._wait_answer(inst, "sid", "a1", timeout_s=5))
        assert ei.value.engine_error == "boom"
        assert ei.value.status_code == 502

    def test_ask_stream_emits_error_frame_not_answer(self, monkeypatch):
        """End-to-end over the generator with every upstream call stubbed."""
        inst = router.Instance("tk", "sbx", None, "key")
        recorded: list[dict] = []

        async def _get_or_create(tk, model, llm, meta=None):
            return inst

        async def _ensure_session(inst_, sid):
            return "sid-1"

        async def _post_turn(inst_, sid, query, **kw):
            return "att-1"

        async def _pump_events(inst_, sid, q):
            await asyncio.sleep(3600)

        async def _wait_answer(inst_, sid, attempt_id, timeout_s):
            raise router._EngineFailed("Execution failed: provider 502")

        async def _cancel_attempt_bg(inst_, sid, tk):
            return None

        monkeypatch.setattr(router, "get_or_create", _get_or_create)
        monkeypatch.setattr(router, "_ensure_session", _ensure_session)
        monkeypatch.setattr(router, "_post_turn", _post_turn)
        monkeypatch.setattr(router, "_pump_events", _pump_events)
        monkeypatch.setattr(router, "_wait_answer", _wait_answer)
        monkeypatch.setattr(router, "_cancel_attempt_bg", _cancel_attempt_bg)
        monkeypatch.setattr(router, "_record_ask", recorded.append)

        async def _collect():
            body = router.AskBody(uid=UID, query="q")
            return [json.loads(line) async for line in router._ask_stream(body, 30)]

        frames = _run(_collect())

        kinds = [f["t"] for f in frames]
        assert "answer" not in kinds
        err = [f for f in frames if f["t"] == "error"][0]
        assert err["status"] == 502
        assert "provider 502" in err["detail"]
        assert err["stats"]["router"]["outcome"] == "engine_failed"
        assert recorded and recorded[0]["outcome"] == "engine_failed"
        assert recorded[0]["engine_cancelled"] is True


# ── A6: engine 422 → readable 400 ────────────────────────────────────────────


class TestEngine422Detail:
    def test_length_cap_gets_the_chinese_hint(self):
        class _R:
            text = json.dumps({"detail": [{"type": "string_too_long", "loc": ["body", "content"]}]})

        d = router._engine_422_detail(_R())
        assert "问题过长" in d
        assert str(router.ENGINE_QUERY_MAX_CHARS) in d

    def test_other_422_is_passed_through_truncated(self):
        class _R:
            text = "x" * 500

        d = router._engine_422_detail(_R())
        assert d.startswith("引擎拒绝了请求参数")
        assert len(d) < 300

    def test_post_turn_maps_422_to_400(self, monkeypatch):
        class _Resp:
            status_code = 422
            text = '{"detail":[{"type":"string_too_long","loc":["body","content"]}]}'

        async def _fake_vibe(inst, method, path, **kw):
            return _Resp()

        monkeypatch.setattr(router, "_vibe", _fake_vibe)
        inst = router.Instance("tk", "sbx", None, "key")
        with pytest.raises(HTTPException) as ei:
            _run(router._post_turn(inst, "sid", "q"))
        assert ei.value.status_code == 400
        assert "问题过长" in ei.value.detail


# ── A7: /forget reports failure ──────────────────────────────────────────────


class TestForgetReportsFailure:
    def test_sandbox_delete_raising_yields_ok_false(self, tenant, monkeypatch):
        tk = router.tenant_key(UID)
        router.state[tk] = {"sandbox_id": "sbx-123456789"}

        async def _boom(sandbox_id):
            raise RuntimeError("cube api down")

        monkeypatch.setattr(router, "sbx_delete", _boom)
        resp = _run(router.forget({"uid": UID}, authorization=AUTH))

        assert resp.status_code == 500
        body = json.loads(resp.body)
        assert body["ok"] is False
        assert "sandbox" in body["error"]
        # Mapping kept so the nightly retry can find the sandbox again.
        assert tk in router.state
        router.state.pop(tk, None)

    def test_sandbox_delete_returning_false_yields_ok_false(self, tenant, monkeypatch):
        tk = router.tenant_key(UID)
        router.state[tk] = {"sandbox_id": "sbx-123456789"}

        async def _nope(sandbox_id):
            return False

        monkeypatch.setattr(router, "sbx_delete", _nope)
        resp = _run(router.forget({"uid": UID}, authorization=AUTH))
        assert resp.status_code == 500
        assert json.loads(resp.body)["ok"] is False
        router.state.pop(tk, None)

    def test_rmtree_failure_yields_ok_false(self, tenant, monkeypatch):
        (tenant / "memory").mkdir()
        (tenant / "memory" / "x.md").write_text("x")
        router.state.pop(router.tenant_key(UID), None)

        def _boom(path, onerror=None, **kw):
            onerror(None, str(path / "memory"), (OSError, OSError("EBUSY"), None))

        monkeypatch.setattr(router.shutil, "rmtree", _boom)
        resp = _run(router.forget({"uid": UID}, authorization=AUTH))
        assert resp.status_code == 500
        body = json.loads(resp.body)
        assert body["ok"] is False
        assert "incomplete" in body["error"]

    def test_clean_forget_is_ok_and_idempotent(self, tenant):
        (tenant / "sessions").mkdir()
        router.state.pop(router.tenant_key(UID), None)
        assert _run(router.forget({"uid": UID}, authorization=AUTH)) == {"ok": True}
        assert not tenant.exists()
        # Second call: nothing to do, still ok.
        assert _run(router.forget({"uid": UID}, authorization=AUTH)) == {"ok": True}

    def test_symlinked_tenant_dir_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.setattr(router, "DATA_ROOT", tmp_path)
        host_dir = tmp_path.parent / "host-tenant"
        host_dir.mkdir(exist_ok=True)
        (host_dir / "keep").write_text("k")
        (tmp_path / router.tenant_key(UID)).symlink_to(host_dir)
        assert router._rmtree_tenant_dir(tmp_path / router.tenant_key(UID)) is not None
        assert (host_dir / "keep").exists()


# ── A10: per-session delete + watermark fields ───────────────────────────────


class TestSessionsDelete:
    def test_offline_mode_removes_the_host_dir(self, tenant):
        sess = tenant / "sessions" / "sess0001"
        sess.mkdir(parents=True)
        (sess / "messages.jsonl").write_text("{}\n")
        (sess / "trace.jsonl").write_text("{}\n")
        (sess / "transcript_1.jsonl").write_text("{}\n")
        (sess / "handoff.json").write_text("{}")
        router.pool.pop(router.tenant_key(UID), None)

        out = _run(router.sessions_delete(
            router.SessionDeleteBody(uid=UID, session_id="sess0001"), authorization=AUTH))

        assert out == {"ok": True, "mode": "offline", "deleted": True}
        assert not sess.exists()
        assert (tenant / "sessions").is_dir()

    def test_missing_session_is_ok_but_not_deleted(self, tenant):
        router.pool.pop(router.tenant_key(UID), None)
        out = _run(router.sessions_delete(
            router.SessionDeleteBody(uid=UID, session_id="nothere1"), authorization=AUTH))
        assert out == {"ok": True, "mode": "offline", "deleted": False}

    def test_symlinked_session_dir_is_refused(self, tenant, tmp_path):
        host_dir = tmp_path.parent / "host-sess-del"
        host_dir.mkdir(exist_ok=True)
        (host_dir / "keep").write_text("k")
        (tenant / "sessions").mkdir()
        (tenant / "sessions" / "evil0001").symlink_to(host_dir)
        router.pool.pop(router.tenant_key(UID), None)

        resp = _run(router.sessions_delete(
            router.SessionDeleteBody(uid=UID, session_id="evil0001"), authorization=AUTH))

        assert resp.status_code == 500
        assert json.loads(resp.body)["ok"] is False
        assert (host_dir / "keep").exists()

    def test_invalid_session_id_is_400(self, tenant):
        with pytest.raises(HTTPException) as ei:
            _run(router.sessions_delete(
                router.SessionDeleteBody(uid=UID, session_id="../x"), authorization=AUTH))
        assert ei.value.status_code == 400

    def test_engine_mode_when_sandbox_is_running(self, tenant, monkeypatch):
        tk = router.tenant_key(UID)
        inst = router.Instance(tk, "sbx", None, "key")
        router.pool[tk] = inst
        calls: list[tuple[str, str]] = []

        class _Resp:
            status_code = 200
            text = ""

        async def _fake_vibe(inst_, method, path, **kw):
            calls.append((method, path))
            return _Resp()

        monkeypatch.setattr(router, "_vibe", _fake_vibe)
        try:
            out = _run(router.sessions_delete(
                router.SessionDeleteBody(uid=UID, session_id="sess0002"), authorization=AUTH))
        finally:
            router.pool.pop(tk, None)

        assert calls == [("DELETE", "/sessions/sess0002")]
        assert out == {"ok": True, "mode": "engine", "deleted": True}

    def test_engine_unreachable_falls_back_to_offline(self, tenant, monkeypatch):
        tk = router.tenant_key(UID)
        router.pool[tk] = router.Instance(tk, "sbx", None, "key")
        sess = tenant / "sessions" / "sess0003"
        sess.mkdir(parents=True)

        async def _dead(inst_, method, path, **kw):
            raise ConnectionError("proxy lost it")

        monkeypatch.setattr(router, "_vibe", _dead)
        try:
            out = _run(router.sessions_delete(
                router.SessionDeleteBody(uid=UID, session_id="sess0003"), authorization=AUTH))
        finally:
            router.pool.pop(tk, None)

        assert out == {"ok": True, "mode": "offline", "deleted": True}
        assert not sess.exists()

    def test_requires_bearer(self, tenant):
        with pytest.raises(HTTPException) as ei:
            _run(router.sessions_delete(
                router.SessionDeleteBody(uid=UID, session_id="sess0001"), authorization=None))
        assert ei.value.status_code == 401


class TestWatermarkIsConsumable:
    def test_usage_lists_offending_tk8s_and_disk_pct(self, tmp_path, monkeypatch):
        monkeypatch.setattr(router, "DATA_ROOT", tmp_path)
        monkeypatch.setattr(router, "TENANT_QUOTA_BYTES", 1000)
        monkeypatch.setattr(router, "TENANT_WATERMARK", 0.8)
        router._du_cache.clear()
        for name, size in (("aaaaaaaa-big", 900), ("bbbbbbbb-small", 10), ("cccccccc-big", 950)):
            d = tmp_path / name
            d.mkdir()
            (d / "f").write_bytes(b"x" * size)

        out = _run(router.tenants_usage(authorization=AUTH))

        assert out["over_watermark"] == ["cccccccc", "aaaaaaaa"]  # largest first
        assert isinstance(out["disk_used_pct"], float)
        assert 0.0 <= out["disk_used_pct"] <= 100.0

    def test_healthz_disk_block(self, tmp_path, monkeypatch):
        monkeypatch.setattr(router, "DATA_ROOT", tmp_path)
        monkeypatch.setattr(router, "TENANT_QUOTA_BYTES", 1000)
        router._du_cache.clear()
        d = tmp_path / "dddddddd-big"
        d.mkdir()
        (d / "f").write_bytes(b"x" * 999)

        out = _run(router.healthz(authorization=AUTH))

        assert out["disk"]["over_watermark"] == ["dddddddd"]
        assert out["disk"]["disk_used_pct"] is not None

    def test_disk_used_pct_survives_missing_path(self, tmp_path):
        assert router._disk_used_pct(tmp_path / "missing") is not None
