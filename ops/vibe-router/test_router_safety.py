"""Pure-logic safety tests for vibe-router (no live instances needed).

Covers the security-critical path derivation (M5/m3): tenant_key must be
hex-only and tenant_home must never escape the users base, even for adversarial
uids. Run: VIBE_ROUTER_SECRET=x VIBE_ROUTER_TOKEN=y python -m pytest test_router_safety.py
(or execute directly).
"""
import os
import re

os.environ.setdefault("VIBE_ROUTER_SECRET", "test-secret")
os.environ.setdefault("VIBE_ROUTER_TOKEN", "test-token")
os.environ.setdefault("VIBE_USERS_BASE", "/tmp/vibe-users-test")

import router  # noqa: E402


def test_tenant_key_is_hex_and_stable():
    tk1 = router.tenant_key("user-123")
    tk2 = router.tenant_key("user-123")
    assert tk1 == tk2, "tenant_key must be stable for the same uid"
    assert re.fullmatch(r"[0-9a-f]{64}", tk1), "tenant_key must be 64 hex chars"
    assert router.tenant_key("other") != tk1, "different uids → different keys"


def test_tenant_home_under_base():
    tk = router.tenant_key("alice")
    home = router.tenant_home(tk)
    assert str(home).startswith(str(router.USERS_BASE) + "/")
    assert home.parent == router.USERS_BASE


def test_tenant_home_rejects_traversal_and_raw_uid():
    # Adversarial uids must never reach the path: they go through HMAC first, so
    # tenant_home only ever sees hex. Directly feeding a non-hex key must raise.
    for bad in ["../../etc/passwd", "..", "a/b", "", "g" * 64, "ABC", "0" * 63]:
        try:
            router.tenant_home(bad)
        except AssertionError:
            continue
        raise AssertionError(f"tenant_home accepted unsafe key: {bad!r}")
    # And the HMAC of an adversarial uid is still hex-only → safe.
    tk = router.tenant_key("../../../etc/passwd")
    assert re.fullmatch(r"[0-9a-f]{64}", tk)
    assert router.tenant_home(tk).parent == router.USERS_BASE


if __name__ == "__main__":
    test_tenant_key_is_hex_and_stable()
    test_tenant_home_under_base()
    test_tenant_home_rejects_traversal_and_raw_uid()
    print("PASS all router safety tests")
