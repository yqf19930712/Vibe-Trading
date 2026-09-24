"""Tenant-safety profile flags shared by the tool registry, config loading and the API.

``VIBE_TRADING_TENANT_SAFE=1`` marks a single-user engine instance running
behind the shared multi-tenant gateway (injected by the vibe-router). It has
no importers from the tools package on purpose, so ``src.config`` can read
it without triggering tool discovery.

``VIBE_MULTITENANT=1`` marks the hosted deployment itself (one engine per
tenant MicroVM, driven only by the router through cube-proxy). In that
profile nothing inside the guest is a trusted local operator: the model's
own shell subprocesses share the engine's loopback, so the API stops
treating loopback peers as authenticated.
"""

from __future__ import annotations

import os

_TRUE_VALUES = frozenset({"1", "true", "yes"})


def _flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in _TRUE_VALUES


def tenant_safe_enabled() -> bool:
    """Whether the multi-tenant safety profile is active for this process."""
    return _flag("VIBE_TRADING_TENANT_SAFE")


def multitenant_enabled() -> bool:
    """Whether this engine runs as one tenant of the hosted multi-tenant stack."""
    return _flag("VIBE_MULTITENANT")


def tenant_profile_active() -> bool:
    """Either tenant flag: the engine serves a hosted tenant, not a local user."""
    return tenant_safe_enabled() or multitenant_enabled()
