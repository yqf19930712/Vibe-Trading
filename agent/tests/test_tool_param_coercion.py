"""Tests for schema-driven tool-arg coercion (attempt 052d98f52286).

OpenAI-compatible channels intermittently stringify tool-call argument
values; the registry must coerce them toward the declared JSON-schema types
before the tool sees them, and coercion must be lossless-only.
"""

from __future__ import annotations

import json

from src.agent.tools import BaseTool, ToolRegistry, _coerce_params, _coerce_value


SCHEMA = {
    "type": "object",
    "properties": {
        "codes": {"type": "array", "items": {"type": "string"}},
        "max_rows": {"type": "integer"},
        "ratio": {"type": "number"},
        "flag": {"type": "boolean"},
        "note": {"type": "string"},
    },
    "required": ["codes"],
}


class TestCoerceValue:
    def test_integer_string(self):
        assert _coerce_value("integer", "0") == 0
        assert _coerce_value("integer", " 60 ") == 60

    def test_number_and_boolean(self):
        assert _coerce_value("number", "1.5") == 1.5
        assert _coerce_value("boolean", "true") is True
        assert _coerce_value("boolean", "False") is False

    def test_array_json_string(self):
        # attempt 052d98f52286: codes arrived as '["CBRS.US"]'
        assert _coerce_value("array", '["CBRS.US"]') == ["CBRS.US"]

    def test_lossless_only(self):
        assert _coerce_value("integer", "abc") == "abc"
        assert _coerce_value("array", "not json") == "not json"
        assert _coerce_value("array", '"just a string"') == '"just a string"'
        assert _coerce_value("integer", 5) == 5
        assert _coerce_value(None, "5") == "5"

    def test_string_type_untouched(self):
        assert _coerce_value("string", "0") == "0"


class TestCoerceParams:
    def test_mixed(self):
        out = _coerce_params(
            SCHEMA,
            {"codes": '["CBRS.US"]', "max_rows": "0", "note": "7", "extra": "1"},
        )
        assert out["codes"] == ["CBRS.US"]
        assert out["max_rows"] == 0
        assert out["note"] == "7"  # declared string stays string
        assert out["extra"] == "1"  # undeclared param passes through


class _EchoTool(BaseTool):
    name = "echo_types"
    description = "test"
    parameters = SCHEMA

    def execute(self, **kwargs):
        return json.dumps({k: type(v).__name__ for k, v in kwargs.items()})


class TestRegistryExecute:
    def test_execute_applies_coercion(self):
        reg = ToolRegistry()
        reg.register(_EchoTool())
        out = json.loads(
            reg.execute("echo_types", {"codes": '["A.US"]', "max_rows": "10"})
        )
        assert out == {"codes": "list", "max_rows": "int"}


class TestFetchMarketDataBelt:
    def test_stringified_args_no_typeerror(self):
        # Direct-call path (bypasses registry coercion) must also survive.
        from src.market_data import fetch_market_data

        out = fetch_market_data(
            codes='["NOSUCH.US"]',
            start_date="2026-08-20",
            end_date="2026-08-21",
            max_rows="0",
            loader_resolver=lambda s: (_ for _ in ()).throw(RuntimeError("no loader")),
        )
        # No TypeError; the unknown symbol lands in gaps instead.
        assert "NOSUCH.US" in out.get("_unresolved", [])
