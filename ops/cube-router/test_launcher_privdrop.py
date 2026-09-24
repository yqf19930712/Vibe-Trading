"""The launcher runs the engine unprivileged (ops/cube-engine/launcher.py).

Run: python -m pytest test_launcher_privdrop.py

CubeSandbox starts the template's CMD as root, so in production the launcher
is root. It must spawn the engine as the image user, hand the tenant data dir
to that user first, keep the egress key in a root-only directory, and refuse
to boot rather than fall back to running the engine as root. A non-root
launcher (docker run honouring USER) keeps its old behaviour.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

_LAUNCHER = Path(__file__).resolve().parent.parent / "cube-engine" / "launcher.py"


@pytest.fixture()
def launcher(monkeypatch):
    spec = importlib.util.spec_from_file_location("vibe_launcher_privdrop", _LAUNCHER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    spawned: list[dict] = []

    class FakeProc:
        def poll(self):
            return None

    def fake_popen(argv, **kw):
        spawned.append(kw)
        return FakeProc()

    monkeypatch.setattr(mod.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(mod, "_engine_healthy", lambda: True)
    monkeypatch.setattr(mod, "_configure_tunnel", lambda env: None)
    monkeypatch.setattr(mod, "_wait_tunnel_ready", lambda s: True)
    return mod, spawned


def _as_root(monkeypatch, mod, home, uid=1000, gid=1000):
    monkeypatch.setattr(mod.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        mod.pwd, "getpwnam",
        lambda name: SimpleNamespace(pw_uid=uid, pw_gid=gid, pw_dir=str(home)),
    )


def test_non_root_launcher_spawns_engine_as_itself(launcher, monkeypatch):
    mod, spawned = launcher
    monkeypatch.setattr(mod.os, "geteuid", lambda: 1000)
    ok, _ = mod._boot_engine({})
    assert ok
    assert "user" not in spawned[0] and "group" not in spawned[0]


def test_root_launcher_drops_engine_to_image_user(launcher, monkeypatch, tmp_path):
    mod, spawned = launcher
    _as_root(monkeypatch, mod, tmp_path)
    handed: list[tuple] = []
    monkeypatch.setattr(mod, "_hand_over_data_dir", lambda d, u, g: handed.append((d, u, g)))
    monkeypatch.setenv("VIBE_DATA_DIR", str(tmp_path / ".vibe-trading"))
    ok, _ = mod._boot_engine({"LANGCHAIN_MODEL_NAME": "m"})
    assert ok
    kw = spawned[0]
    assert (kw["user"], kw["group"], kw["extra_groups"]) == (1000, 1000, [])
    assert "preexec_fn" not in kw  # multi-threaded server: no preexec_fn
    assert kw["env"]["HOME"] == str(tmp_path)
    assert kw["env"]["USER"] == "vibe"
    assert kw["env"]["LANGCHAIN_MODEL_NAME"] == "m"
    assert handed == [(str(tmp_path / ".vibe-trading"), 1000, 1000)]


def test_root_launcher_refuses_when_engine_user_missing(launcher, monkeypatch):
    mod, spawned = launcher
    monkeypatch.setattr(mod.os, "geteuid", lambda: 0)

    def missing(name):
        raise KeyError(name)

    monkeypatch.setattr(mod.pwd, "getpwnam", missing)
    ok, detail = mod._boot_engine({})
    assert not ok
    assert "refusing" in detail
    assert spawned == []


def test_hand_over_data_dir_does_not_follow_links(launcher, monkeypatch, tmp_path):
    mod, _ = launcher
    data = tmp_path / "data"
    (data / "sessions" / "s1").mkdir(parents=True)
    (data / "sessions" / "s1" / "messages.jsonl").write_text("{}")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("x")
    (data / "link").symlink_to(outside)
    chowned: list[str] = []
    monkeypatch.setattr(mod.os, "lchown", lambda p, u, g: chowned.append(os.path.relpath(p, tmp_path)))
    # A uid nothing in tmp_path belongs to, so every entry needs handing over.
    mod._hand_over_data_dir(str(data), 4242, 4242)
    assert "data" in chowned
    assert os.path.join("data", "sessions", "s1", "messages.jsonl") in chowned
    assert os.path.join("data", "link") in chowned  # the link itself (lchown)
    assert not any(p.startswith("outside") for p in chowned)


def test_hand_over_skips_entries_already_owned(launcher, monkeypatch, tmp_path):
    mod, _ = launcher
    (tmp_path / "f").write_text("x")
    chowned: list[str] = []
    monkeypatch.setattr(mod.os, "lchown", lambda p, u, g: chowned.append(p))
    st = os.lstat(tmp_path)
    mod._hand_over_data_dir(str(tmp_path), st.st_uid, st.st_gid)
    assert chowned == []


def test_root_launcher_keeps_egress_key_out_of_engine_home(launcher, monkeypatch):
    mod, _ = launcher
    monkeypatch.setattr(mod.os, "geteuid", lambda: 0)
    assert mod._key_dir() == mod.ROOT_KEY_DIR == "/run/vibe-launcher"
    monkeypatch.setattr(mod.os, "geteuid", lambda: 1000)
    assert mod._key_dir() == os.path.expanduser("~/.ssh")
