"""Tests for the registry-level error-path redaction backstop (batch F, F6)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.agent.tools import BaseTool, ToolRegistry


class _ExplodingTool(BaseTool):
    name = "exploding_tool"
    description = "Always raises with an internal path in the message."
    parameters = {"type": "object", "properties": {}, "required": []}

    def execute(self, **kwargs: Any) -> str:
        raise RuntimeError(f"config missing at {Path.home()}/secret/place.json")


def test_registry_fallback_redacts_internal_paths() -> None:
    registry = ToolRegistry()
    registry.register(_ExplodingTool())

    result = json.loads(registry.execute("exploding_tool", {}))

    assert result["status"] == "error"
    assert str(Path.home()) not in result["error"]
    assert "<redacted>" in result["error"]
    # The relative tail survives for diagnosability.
    assert "secret/place.json" in result["error"]
