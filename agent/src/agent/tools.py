"""BaseTool + ToolRegistry: tool infrastructure."""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)
from typing import Any, Dict, List, Optional


class BaseTool(ABC):
    """Tool base class.

    Attributes:
        name: Unique tool identifier.
        description: Tool description shown to the LLM.
        parameters: Parameter definition in JSON Schema format.
        repeatable: Whether the tool may be called more than once.
        timeout_seconds: Optional per-call upper bound this tool declares for
            itself (see below).
    """

    name: str = ""
    description: str = ""
    parameters: Dict[str, Any] = {}
    repeatable: bool = False
    is_readonly: bool = True
    # The tool's own upper bound on a single call, in seconds.
    # None = use the loop's global VIBE_TRADING_TOOL_TIMEOUT_SECONDS.
    #
    # This is NOT an exemption from the F2 write-tool watchdog: the loop still
    # clamps whatever is declared here by the attempt's remaining budget
    # (``cap_timeout``), so a hung tool can never outlive the caller's
    # deadline. The declaration only RAISES the base of the 1x-warn / 2x-abandon
    # window — ``_tool_timeout`` takes ``max(global, declared)``, so a tool can
    # never quietly shorten its own window either.
    #
    # Declare it only when the tool's NORMAL runtime legitimately exceeds the
    # tenant-wide limit (run_swarm: a multi-layer DAG of LLM workers that
    # routinely runs for tens of minutes). A tool that declares a long timeout
    # MUST also enforce a budget of its own and return partial results when it
    # expires — otherwise the declaration just moves the unbounded wait from the
    # loop into the tool.
    #
    # May be overridden as a property when the value depends on runtime config
    # (see SwarmTool / AlphaBenchTool / MCPRemoteTool).
    timeout_seconds: Optional[float] = None

    @classmethod
    def check_available(cls) -> bool:
        """Check if this tool's dependencies are met.

        Override in subclasses to check for API keys, packages, etc.
        Tools that return False are excluded from the registry.
        """
        return True

    @abstractmethod
    def execute(self, **kwargs: Any) -> str:
        """Execute the tool and return a JSON string."""

    def to_openai_schema(self) -> Dict[str, Any]:
        """Convert to OpenAI function calling format."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters or {"type": "object", "properties": {}, "required": []},
            },
        }


def _coerce_params(schema: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
    """Best-effort coercion of LLM-emitted args toward the declared schema.

    OpenAI-compatible channels intermittently stringify argument values —
    attempt 052d98f52286 sent ``max_rows: "0"`` and even a JSON-encoded
    string for an array param (``codes: "[\\"CBRS.US\\"]"``), which blew up
    deep inside the tool (``'<' not supported between str and int``) and
    burned four identical retries. Coercion is lossless-only: values that
    don't parse cleanly pass through unchanged so the tool's own validation
    still owns the final word.
    """
    props = (schema or {}).get("properties")
    if not isinstance(props, dict) or not isinstance(params, dict):
        return params
    out: Dict[str, Any] = {}
    for key, value in params.items():
        spec = props.get(key)
        declared = spec.get("type") if isinstance(spec, dict) else None
        out[key] = _coerce_value(declared, value)
    return out


def _coerce_value(declared: Any, value: Any) -> Any:
    if not isinstance(value, str) or not isinstance(declared, str):
        return value
    text = value.strip()
    try:
        if declared == "integer":
            return int(text, 10)
        if declared == "number":
            return float(text)
        if declared == "boolean":
            if text.lower() in ("true", "1"):
                return True
            if text.lower() in ("false", "0"):
                return False
        elif declared in ("array", "object"):
            parsed = json.loads(text)
            if (declared == "array" and isinstance(parsed, list)) or (
                declared == "object" and isinstance(parsed, dict)
            ):
                return parsed
    except (ValueError, TypeError):
        pass
    return value


class ToolRegistry:
    """Tool registry."""

    def __init__(self) -> None:
        self._tools: Dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        """Register a tool."""
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[BaseTool]:
        """Retrieve a tool by name."""
        return self._tools.get(name)

    def get_definitions(self) -> List[Dict[str, Any]]:
        """Return all tools in OpenAI function calling format."""
        return [t.to_openai_schema() for t in self._tools.values()]

    def execute(self, name: str, params: Dict[str, Any]) -> str:
        """Execute a tool and guarantee a valid JSON return value."""
        tool = self._tools.get(name)
        if not tool:
            # List what IS available: a bare "not found" leaves the model to
            # guess again from the same wrong memory (the same reason
            # _resolve_preset spells out the preset roster on a bad name).
            available = ", ".join(sorted(self._tools))
            return json.dumps(
                {
                    "status": "error",
                    "error": f"Tool '{name}' not found. Available tools: {available}",
                    "available_tools": sorted(self._tools),
                },
                ensure_ascii=False,
            )
        try:
            return tool.execute(**_coerce_params(tool.parameters, params))
        except Exception as exc:
            logger.exception("Tool %s failed", name)
            # F6: the raw exception text can leak internal filesystem topology
            # (home dir, venv paths) to the model. Individual tools already
            # redact their own error paths (e.g. read_file); this is the
            # registry-level backstop for every tool that doesn't.
            # Lazy import: src.tools.redaction lives in the package whose
            # __init__ imports this module — a top-level import would cycle.
            from src.tools.redaction import redact_internal_paths

            return json.dumps({
                "status": "error", "tool": name,
                "error": redact_internal_paths(str(exc)),
            }, ensure_ascii=False)

    @property
    def tool_names(self) -> List[str]:
        return list(self._tools.keys())

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools
