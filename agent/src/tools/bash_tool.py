"""Bash tool: execute shell commands under run_dir."""

from __future__ import annotations

import json
import os
import re
import subprocess
from typing import Any

from src.agent.progress import emit_progress
from src.agent.tools import BaseTool
from src.tools.redaction import redact_secret_values
from src.tools.subprocess_env import _subprocess_env

# The trajectory copy of a bash result is bounded by the one truncation
# layer every tool shares (``agent.tool_result_store``: 10k head+tail preview,
# full streams offloaded to ``<run_dir>/tool-results/`` as plain text for
# ``read_file`` paging), so this tool returns its streams whole. The hard cap
# below is a resource guard only — it keeps a runaway process from parking an
# unbounded string in memory — and is marked explicitly when it fires.
_OUTPUT_HARD_CAP = 1_000_000
_HARD_CAP_HEAD = 800_000
_HARD_CAP_TAIL = 200_000
# Configurable (a bare hard-coded value has no relationship to the tenant's
# own budget), and clamped per call by the attempt's remaining budget (``_effective_timeout``) so bash always returns
# its own actionable "use background_run" error BEFORE the loop's write-tool
# watchdog abandons the call with a generic one.
_DEFAULT_TIMEOUT = float(os.getenv("VIBE_BASH_TIMEOUT_S", "120"))
# Kept back so the JSON error can still be built and returned after the cut-off.
_TIMEOUT_RESERVE_S = 15.0
_TIMEOUT_FLOOR_S = 10.0


def _effective_timeout() -> float:
    """Per-call bash timeout, clamped by the attempt's remaining budget.

    Returns:
        Timeout in seconds to hand ``subprocess.run``.
    """
    from src.core.budget import cap_timeout

    return cap_timeout(
        _DEFAULT_TIMEOUT, reserve_s=_TIMEOUT_RESERVE_S, floor_s=_TIMEOUT_FLOOR_S
    )


# F4: dangerous-pattern AUDIT blacklist. Matching commands are NOT blocked —
# the sandbox is the enforcement layer — but each match is recorded in the
# result payload (which lands in the trace via the tool_result entry) and
# emitted as a progress event, so the observability panel can review what the
# model tried to run. Patterns are deliberately narrow: they target the classic
# foot-guns, not every superficially similar command.
_DANGEROUS_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # rm -rf (or -fr) aimed at /, /*, ~ or $HOME — filesystem-wide deletion.
    ("rm_rf_root", re.compile(r"\brm\s+(-\w+\s+)*-\w*[rf]\w*\s+(/|/\*|~|\$HOME)(\s|$|;)")),
    # Redirecting output to an absolute path outside the run_dir sandbox
    # (/dev/null and /tmp are tolerated as benign).
    ("abs_path_redirect", re.compile(r"(?<![0-9<>])>{1,2}\s*/(?!dev/null|tmp/)")),
    # Piping a remote download straight into a shell interpreter.
    ("curl_pipe_sh", re.compile(r"\b(curl|wget)\b[^|;&]*\|\s*(sudo\s+)?(ba|z|da)?sh\b")),
    # Privilege escalation attempts inside the sandbox.
    ("sudo", re.compile(r"\bsudo\b")),
    # Raw disk writes.
    ("dd_to_device", re.compile(r"\bdd\b[^;|&]*\bof=/dev/")),
    # Recursive permission blow-open on absolute paths.
    ("chmod_777_abs", re.compile(r"\bchmod\s+(-\w+\s+)*777\s+/")),
)


def _audit_command(command: str) -> list[str]:
    """Return the ids of dangerous patterns matched by ``command`` (F4)."""
    return [name for name, pattern in _DANGEROUS_PATTERNS if pattern.search(command)]


def _cap_output(text: str, stream: str) -> str:
    """Apply the resource hard cap to one stream (see ``_OUTPUT_HARD_CAP``).

    Args:
        text: Raw stream output.
        stream: Stream label ("stdout"/"stderr") named in the marker.

    Returns:
        ``text`` unchanged when within the cap, otherwise head + explicit
        marker + tail.
    """
    if len(text) <= _OUTPUT_HARD_CAP:
        return text
    dropped = len(text) - _HARD_CAP_HEAD - _HARD_CAP_TAIL
    return (
        text[:_HARD_CAP_HEAD]
        + f"\n\n...[{stream}: {dropped} chars dropped from the middle — the process "
        f"produced more than {_OUTPUT_HARD_CAP} chars; rerun with a filter "
        "(head/tail/grep) or redirect to a file under run_dir]...\n\n"
        + text[-_HARD_CAP_TAIL:]
    )


class BashTool(BaseTool):
    """Execute shell commands in the working directory."""

    name = "bash"
    description = (
        "Execute a shell command in the working directory and wait for it. Use for "
        "installing packages, running scripts, or inspecting files. The command "
        "runs inside the tenant's isolated sandbox with a minimal environment: "
        "no API keys or data-source credentials are exported to it, and sites "
        "outside mainland China are not directly reachable from it, so use the "
        "dedicated web_search/read_url/get_market_data tools for anything that "
        "needs authenticated data access or a foreign endpoint. "
        f"The command is killed after ~{_DEFAULT_TIMEOUT:.0f}s (less when the turn's "
        "remaining budget is shorter), so this tool is for work that finishes in "
        "well under that. For anything longer — model training, bulk data "
        "processing, large installs — use background_run instead: it returns a "
        "task_id immediately and you poll it with check_background."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command to execute"},
        },
        "required": ["command"],
    }
    repeatable = True
    is_readonly = False

    def execute(self, **kwargs: Any) -> str:
        """Execute a shell command.

        Args:
            **kwargs: Must include command. Optional run_dir used as cwd.

        Returns:
            JSON string with stdout, stderr, and exit_code.
        """
        command = kwargs["command"]
        cwd = kwargs.get("run_dir")

        # F4: audit-only dangerous-pattern scan (never blocks — see constant).
        audit_findings = _audit_command(str(command))
        if audit_findings:
            emit_progress(
                stage="security_audit",
                message=f"bash command matched dangerous patterns: {', '.join(audit_findings)}",
            )

        timeout_s = _effective_timeout()
        try:
            result = subprocess.run(
                command,
                shell=True,
                cwd=cwd,
                # Allowlisted env only: the engine process
                # env carries tenant-shared LLM/data-source credentials.
                env=_subprocess_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout_s,
                encoding="utf-8",
                errors="replace",
            )
            # Value-based scrub here so neither the trajectory copy nor the
            # offloaded full copy (tool_result_store) carries a secret.
            stdout = redact_secret_values(result.stdout)
            stderr = redact_secret_values(result.stderr)
            payload: dict[str, Any] = {
                "status": "ok" if result.returncode == 0 else "error",
                "exit_code": result.returncode,
                "stdout": _cap_output(stdout, "stdout"),
                "stderr": _cap_output(stderr, "stderr"),
            }
            if audit_findings:
                payload["security_audit"] = audit_findings
            return json.dumps(payload, ensure_ascii=False)
        except subprocess.TimeoutExpired:
            payload = {
                "status": "error",
                "error_code": "bash_timeout",
                "timeout_seconds": timeout_s,
                # The old message said only that it timed out, leaving the model
                # to re-run the same doomed command. Name the escape hatch.
                "error": (
                    f"Command timed out after {timeout_s:.0f}s and was killed. "
                    "bash waits synchronously and is only for short commands — "
                    "re-running it will time out again. For long-running work "
                    "use background_run(command=...), which returns a task_id "
                    "immediately, then poll check_background(task_id=...). "
                    "Otherwise narrow the command (smaller date range, fewer "
                    "symbols, one file at a time)."
                ),
            }
            if audit_findings:
                payload["security_audit"] = audit_findings
            return json.dumps(payload, ensure_ascii=False)
        except Exception as exc:
            payload = {
                "status": "error",
                "error": str(exc),
            }
            if audit_findings:
                payload["security_audit"] = audit_findings
            return json.dumps(payload, ensure_ascii=False)
