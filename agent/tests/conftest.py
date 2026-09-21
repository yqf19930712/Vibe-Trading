"""Shared fixtures and sys.path setup for all tests."""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure agent/ is on sys.path so imports like `backtest.*` and `src.*` work.
AGENT_DIR = Path(__file__).resolve().parent.parent
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_background_manager():
    """Reset the process-wide BackgroundManager around every test.

    ``background_run`` tasks and their notifications live on one singleton
    for the life of the process; a task launched by one test would otherwise
    be drained into an unrelated test's agent loop as ``<background-results>``.
    """
    from src.tools.background_tools import get_background_manager

    get_background_manager().reset()
    yield
    get_background_manager().reset()
