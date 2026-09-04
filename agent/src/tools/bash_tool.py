"""Bash tool: execute shell commands under run_dir."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from src.agent.progress import emit_progress
from src.agent.tools import BaseTool
from src.tools.redaction import redact_secret_values
from src.tools.subprocess_env import _subprocess_env

_OUTPUT_LIMIT = 50_000
# F4: head+tail truncation instead of a silent hard cut — long outputs keep
# their beginning (setup, first errors) AND their end (final result, traceback
# tail), and the full text is persisted to run_dir for read_file paging.
_TRUNC_HEAD = 40_000
_TRUNC_TAIL = 8_000
# V1: was a bare hard-coded 120 with no way to tune it and no relationship to
# the tenant's own budget. Now configurable, and clamped per call by the
# attempt's remaining budget (``_effective_timeout``) so bash always returns
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


def _truncate_output(text: str, stream: str, run_dir: str | None) -> str:
    """Head+tail truncate ``text``, persisting the full output when possible.

    Args:
        text: Raw stream output.
        stream: Stream label ("stdout"/"stderr") used in the dump filename.
        run_dir: Run directory to persist the full output into (may be None).

    Returns:
        The original text when within the limit, otherwise head + marker +
        tail. The marker names the on-disk dump (readable via ``read_file``)
        when persisting succeeded.
    """
    if len(text) <= _OUTPUT_LIMIT:
        return text

    dump_hint = ""
    if run_dir:
        try:
            dump_name = f"bash_output_{stream}_{int(time.time() * 1000)}.log"
            dump_path = Path(run_dir) / dump_name
            dump_path.write_text(text, encoding="utf-8")
            dump_hint = f" Full output saved to '{dump_name}' — use read_file (offset/limit) to inspect it."
        except OSError:
            dump_hint = ""

    trimmed = len(text) - _TRUNC_HEAD - _TRUNC_TAIL
    return (
        text[:_TRUNC_HEAD]
        + f"\n\n...[output truncated: {trimmed} chars omitted.{dump_hint}]...\n\n"
        + text[-_TRUNC_TAIL:]
    )


class BashTool(BaseTool):
    """Execute shell commands in the working directory."""

    name = "bash"
    description = (
        "Execute a shell command in the working directory and wait for it. Use for "
        "installing packages, running scripts, or inspecting files. The command "
        "runs inside the tenant's isolated sandbox with a minimal environment: "
        "no API keys or data-source credentials are exported to it, so use the "
        "dedicated web_search/read_url/get_market_data tools for anything that "
        "needs authenticated data access. "
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
                # Allowlisted env only (P0 2026-09-04): the engine process
                # env carries tenant-shared LLM/data-source credentials.
                env=_subprocess_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout_s,
                encoding="utf-8",
                errors="replace",
            )
            # Value-based scrub BEFORE truncation/dumping so neither the
            # trajectory copy nor the on-disk full dump carries a secret.
            stdout = redact_secret_values(result.stdout)
            stderr = redact_secret_values(result.stderr)
            payload: dict[str, Any] = {
                "status": "ok" if result.returncode == 0 else "error",
                "exit_code": result.returncode,
                "stdout": _truncate_output(stdout, "stdout", cwd),
                "stderr": _truncate_output(stderr, "stderr", cwd),
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
