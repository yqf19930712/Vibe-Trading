"""Single source of truth for per-tenant mutable-state roots.

``VIBE_DATA_DIR`` (set by the multi-tenant vibe-router per instance) relocates
all run/session/upload artifacts under a per-user HOME so tenants are isolated.
Unset (single-user / upstream) it falls back to the install ``agent/`` dir,
leaving behavior unchanged. Every run/session/upload path resolver should derive
from here so a new write site can't silently escape the tenant root again.
See PRODUCT_DESIGN.md §2.3.
"""
from __future__ import annotations

import os
from pathlib import Path


def _install_agent_dir() -> Path:
    # this file: agent/src/core/paths.py → parents[2] == the agent/ dir
    return Path(__file__).resolve().parents[2]


def data_root() -> Path:
    env = os.getenv("VIBE_DATA_DIR")
    return Path(env).expanduser() if env else _install_agent_dir()


def runs_root() -> Path:
    return data_root() / "runs"
