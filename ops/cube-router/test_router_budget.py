"""Pure-logic tests for cube-router's budget derivation and disk accounting.

Run: VIBE_ROUTER_SECRET=x VIBE_ROUTER_TOKEN=y VIBE_CUBE_TEMPLATE_ID=tpl-test \
     python -m pytest test_router_budget.py

Why these two areas specifically:
  · **Budget** — the 2026-08-24 incident and the F2/run_swarm P0 both came from
    the two-hour swarm budget being written out in several places that then
    drifted. `BUDGET_BY_INTENT` is now the single source; these tests pin both
    the derivation AND the `timeoutS`-wins precedence that makes a laicai-only
    rollback safe.
  · **Disk accounting** — `/healthz` and `/tenants/usage` are the read-only half
    of the retention plan. They must never throw on a missing/racing tenant dir,
    or a full disk takes the health endpoint down with it.
"""
import os

os.environ.setdefault("VIBE_ROUTER_SECRET", "test-secret")
os.environ.setdefault("VIBE_ROUTER_TOKEN", "test-token")
os.environ.setdefault("VIBE_CUBE_TEMPLATE_ID", "tpl-test")

import router  # noqa: E402


# ── budget derivation ────────────────────────────────────────────────────────


def test_default_intent_gets_standard_budget():
    assert router.budget_for(None, None) == router.DEFAULT_ASK_TIMEOUT_S
    assert router.budget_for("standard", None) == router.DEFAULT_ASK_TIMEOUT_S


def test_deep_team_gets_the_swarm_budget():
    assert router.budget_for("deep_team", None) == router.SWARM_ASK_TIMEOUT_S
    assert router.SWARM_ASK_TIMEOUT_S > router.DEFAULT_ASK_TIMEOUT_S


def test_explicit_timeout_wins_over_intent():
    """The rollback guarantee: laicai keeps sending timeoutS during the staged
    rollout, so reverting laicai alone restores the old budget behaviour."""
    assert router.budget_for("deep_team", 60) == 60
    assert router.budget_for("standard", 7200) == 7200
    assert router.budget_for(None, 900) == 900


def test_unknown_intent_falls_back_to_standard_not_crash():
    assert router.budget_for("committee", None) == router.DEFAULT_ASK_TIMEOUT_S


def test_engine_swarm_env_is_derived_not_copied():
    """SWARM_TIMEOUT handed to the tenant engine must equal the router's own
    deep_team budget — the whole point of collapsing the four copies."""
    env, _key = router.engine_env(None, None)
    assert env["SWARM_TIMEOUT"] == str(router.BUDGET_BY_INTENT["deep_team"])


# ── disk accounting ──────────────────────────────────────────────────────────


def test_dir_bytes_sums_files(tmp_path):
    (tmp_path / "a.txt").write_bytes(b"x" * 100)
    sub = tmp_path / "sessions" / "s1"
    sub.mkdir(parents=True)
    (sub / "messages.jsonl").write_bytes(b"y" * 50)
    assert router._dir_bytes(tmp_path) == 150


def test_dir_bytes_returns_zero_for_missing_dir(tmp_path):
    """A tenant dir can vanish under us (/forget races a healthz poll)."""
    assert router._dir_bytes(tmp_path / "gone") == 0


def test_tenant_usage_is_cached_and_sorted(tmp_path, monkeypatch):
    monkeypatch.setattr(router, "DATA_ROOT", tmp_path)
    router._du_cache.clear()
    big, small = tmp_path / "big-tenant", tmp_path / "small-tenant"
    big.mkdir()
    small.mkdir()
    (big / "f").write_bytes(b"x" * 500)
    (small / "f").write_bytes(b"x" * 10)

    rows = router._tenant_usage_sync()
    assert [r["disk_bytes"] for r in rows] == [500, 10], "sorted desc by size"

    # Second call inside the TTL must reuse the cache, not re-walk.
    (big / "f2").write_bytes(b"x" * 9999)
    assert router._tenant_usage_sync()[0]["disk_bytes"] == 500


def test_over_watermark_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(router, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(router, "TENANT_QUOTA_BYTES", 1000)
    monkeypatch.setattr(router, "TENANT_WATERMARK", 0.8)
    router._du_cache.clear()
    d = tmp_path / "tk"
    d.mkdir()
    (d / "f").write_bytes(b"x" * 900)
    row = router._tenant_usage_sync()[0]
    assert row["over_watermark"] is True
    assert row["pct"] == 90.0


def test_stale_cache_entries_are_dropped(tmp_path, monkeypatch):
    monkeypatch.setattr(router, "DATA_ROOT", tmp_path)
    router._du_cache.clear()
    d = tmp_path / "tk"
    d.mkdir()
    (d / "f").write_bytes(b"x")
    router._tenant_usage_sync()
    assert "tk" in router._du_cache
    # Tenant forgotten → its cache row must not linger forever.
    (d / "f").unlink()
    d.rmdir()
    router._tenant_usage_sync()
    assert "tk" not in router._du_cache
