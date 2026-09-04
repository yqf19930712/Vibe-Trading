"""A2 (P0, review 2026-09-04): shell subprocesses get a minimal env and
credential VALUES are scrubbed from tool output.

Before: ``bash`` / ``background_run`` inherited the whole engine env, so a
single ``env`` command handed the model every tenant-shared secret
(``OPENAI_API_KEY``, ``TUSHARE_TOKEN``, ``API_AUTH_KEY`` …).
"""

from __future__ import annotations

import json
import time

import pytest

from src.tools import redaction
from src.tools.background_tools import BackgroundManager
from src.tools.bash_tool import BashTool
from src.tools.subprocess_env import _subprocess_env, has_secret_segment, is_secret_env_name

SECRET = "sk-test-secret-value-1234567890"


@pytest.fixture(autouse=True)
def _fresh_secret_cache():
    redaction.refresh_secret_values()
    yield
    redaction.refresh_secret_values()


class TestAllowlist:
    def test_plumbing_and_vibe_flags_pass_secrets_do_not(self):
        src = {
            "PATH": "/usr/bin",
            "HOME": "/home/vibe",
            "TZ": "Asia/Shanghai",
            "VIBE_MULTITENANT": "1",
            "VIBE_TRADING_KEYWORDS": "x",
            "OPENAI_API_KEY": SECRET,
            "OPENAI_BASE_URL": "https://api.example",
            "ANTHROPIC_AUTH_TOKEN": "t" * 20,
            "LANGCHAIN_MODEL_NAME": "gpt",
            "TUSHARE_TOKEN": "u" * 20,
            "API_AUTH_KEY": "k" * 20,
            "VIBE_EGRESS_SSH_KEY_B64": "b" * 20,
            "JINA_API_KEY": "j" * 20,
            "RANDOM_OTHER": "no",
        }
        out = _subprocess_env(src)
        assert out == {
            "PATH": "/usr/bin",
            "HOME": "/home/vibe",
            "TZ": "Asia/Shanghai",
            "VIBE_MULTITENANT": "1",
            "VIBE_TRADING_KEYWORDS": "x",
        }

    def test_secret_segment_rules(self):
        assert has_secret_segment("OPENAI_API_KEY")
        assert has_secret_segment("VIBE_EGRESS_SSH_KEY_B64")
        assert has_secret_segment("DB_PASSWORD")
        assert not has_secret_segment("VIBE_TRADING_KEYWORDS")
        assert not has_secret_segment("LANGCHAIN_MODEL_NAME")
        # Prefix families are a subprocess-side denial only.
        assert is_secret_env_name("OPENAI_BASE_URL")
        assert not has_secret_segment("OPENAI_BASE_URL")


class TestValueRedaction:
    def test_known_secret_value_is_replaced_by_key_name(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", SECRET)
        redaction.refresh_secret_values()
        out = redaction.redact_secret_values(f"Authorization: Bearer {SECRET} ok")
        assert SECRET not in out
        assert "[redacted:OPENAI_API_KEY]" in out
        assert out.endswith(" ok")

    def test_short_values_are_not_scrubbed(self, monkeypatch):
        monkeypatch.setenv("SHORT_TOKEN", "abc123")
        redaction.refresh_secret_values()
        assert redaction.redact_secret_values("abc123 appears") == "abc123 appears"

    def test_non_secret_env_values_are_left_alone(self, monkeypatch):
        monkeypatch.setenv("LANGCHAIN_MODEL_NAME", "claude-opus-5-20260901")
        redaction.refresh_secret_values()
        assert redaction.redact_secret_values("model claude-opus-5-20260901") == (
            "model claude-opus-5-20260901"
        )

    def test_longest_secret_wins_when_one_contains_another(self, monkeypatch):
        monkeypatch.setenv("A_TOKEN", "prefix-secret-1234")
        monkeypatch.setenv("B_TOKEN", "prefix-secret-1234-longer-suffix")
        redaction.refresh_secret_values()
        out = redaction.redact_secret_values("x prefix-secret-1234-longer-suffix y")
        assert out == "x [redacted:B_TOKEN] y"

    def test_none_and_non_str(self):
        assert redaction.redact_secret_values(None) == ""
        assert redaction.redact_secret_values(42) == "42"


class TestBashToolDoesNotLeak:
    def test_env_dump_has_path_but_no_secret(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", SECRET)
        monkeypatch.setenv("TUSHARE_TOKEN", "tushare-token-value-0000")
        redaction.refresh_secret_values()

        result = json.loads(BashTool().execute(command="env", run_dir=str(tmp_path)))

        assert result["status"] == "ok"
        assert "PATH=" in result["stdout"]
        assert SECRET not in result["stdout"]
        assert "tushare-token-value-0000" not in result["stdout"]
        assert "OPENAI_API_KEY=" not in result["stdout"]

    def test_echoed_literal_secret_is_scrubbed_from_output(self, tmp_path, monkeypatch):
        """The value can still enter via the command text itself (the model
        read it from a file) — the value-based pass catches that."""
        monkeypatch.setenv("OPENAI_API_KEY", SECRET)
        redaction.refresh_secret_values()

        result = json.loads(
            BashTool().execute(command=f"echo {SECRET}; echo {SECRET} 1>&2", run_dir=str(tmp_path))
        )

        assert SECRET not in result["stdout"]
        assert SECRET not in result["stderr"]
        assert "[redacted:OPENAI_API_KEY]" in result["stdout"]
        assert "[redacted:OPENAI_API_KEY]" in result["stderr"]

    def test_description_promises_no_credentials(self):
        assert "no API keys" in BashTool().description


class TestBackgroundRunDoesNotLeak:
    def test_background_env_dump_is_filtered_and_scrubbed(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", SECRET)
        redaction.refresh_secret_values()
        mgr = BackgroundManager()

        task_id = json.loads(mgr.run(f"env; echo {SECRET}"))["task_id"]
        deadline = time.monotonic() + 20
        while mgr.tasks[task_id]["status"] == "running" and time.monotonic() < deadline:
            time.sleep(0.05)

        assert mgr.tasks[task_id]["status"] == "completed"
        out = mgr.tasks[task_id]["result"]
        assert "PATH=" in out
        assert SECRET not in out
        assert "[redacted:OPENAI_API_KEY]" in out
