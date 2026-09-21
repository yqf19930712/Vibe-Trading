"""Tenant-safety profile flag shared by the tool registry and config loading.

``VIBE_TRADING_TENANT_SAFE=1`` marks a single-user engine instance running
behind the shared multi-tenant gateway (injected by the vibe-router). It has
no importers from the tools package on purpose, so ``src.config`` can read
it without triggering tool discovery.
"""

from __future__ import annotations

import os

_TRUE_VALUES = frozenset({"1", "true", "yes"})


def tenant_safe_enabled() -> bool:
    """Whether the multi-tenant safety profile is active for this process."""
    return os.getenv("VIBE_TRADING_TENANT_SAFE", "").strip().lower() in _TRUE_VALUES
