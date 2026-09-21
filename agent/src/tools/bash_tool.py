"""Bash tool: execute shell commands under run_dir."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.agent.progress import emit_progress
from src.agent.tools import BaseTool
from src.tools.redaction import redact_secret_values
from src.tools.subprocess_env import _subprocess_env

# The trajectory copy of a bash result is bounded by the one truncation
# layer every tool shares (``agent.tool_result_store``: 10k head+tail preview,
# full streams offloaded to ``<run_dir>/tool-results/`` as plain text for
# ``read_file`` paging), so this tool returns its streams whole. The hard cap
# below is a resource guard on the process itself: each stream is read
# incrementally and the process group is killed the moment one of them
# exceeds the cap, so a runaway producer can neither park an unbounded string
# in engine memory nor keep the sandbox busy until the timeout. The kept
# prefix is marked explicitly when it fires.
_OUTPUT_HARD_CAP = 1_000_000
_READ_CHUNK = 65536
# After the process (group) is gone, how long to wait for stragglers that
# escaped the group and still hold a pipe before returning what was read.
_DRAIN_GRACE_S = 2.0
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


@dataclass(frozen=True)
class CappedRun:
    """Outcome of :func:`run_capped`.

    Attributes:
        returncode: Process exit status (negative signal number when killed).
        stdout: Decoded stdout, at most ``_OUTPUT_HARD_CAP`` bytes' worth.
        stderr: Decoded stderr, same bound.
        capped: Names of the streams that exceeded the cap (the process was
            killed on the first one).
    """

    returncode: int
    stdout: str
    stderr: str
    capped: tuple[str, ...] = ()


def _kill_tree(proc: subprocess.Popen[bytes]) -> None:
    """SIGKILL the process group started for ``proc`` (falls back to the leader)."""
    try:
        if hasattr(os, "killpg"):
            os.killpg(proc.pid, signal.SIGKILL)
        else:  # pragma: no cover - non-POSIX
            proc.kill()
    except OSError:
        try:
            proc.kill()
        except OSError:
            pass


def cap_marker(stream: str) -> str:
    """Marker appended to a stream whose producer was killed at the cap."""
    return (
        f"\n\n...[{stream}: the process produced more than {_OUTPUT_HARD_CAP} chars "
        f"and was killed; only the first {_OUTPUT_HARD_CAP} chars were kept. "
        "Rerun with a filter (head/tail/grep) or redirect to a file under run_dir]...\n"
    )


def run_capped(
    command: str,
    *,
    cwd: str | Path | None,
    env: dict[str, str],
    timeout_s: float,
) -> CappedRun:
    """Run ``command`` in a shell, streaming its output under the hard cap.

    Both pipes are drained on reader threads; the first stream to exceed
    ``_OUTPUT_HARD_CAP`` bytes kills the whole process group and the read
    stops there. The process group is also killed on ``timeout_s`` — this
    covers grandchildren of the shell, which ``subprocess.run`` leaves alive
    (and blocks on, since they keep the pipes open).

    Raises:
        subprocess.TimeoutExpired: The process, or a child still holding a
            pipe, outlived ``timeout_s``.
    """
    proc = subprocess.Popen(  # noqa: S602 - shell is the tool's contract
        command,
        shell=True,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=hasattr(os, "killpg"),
    )
    chunks: dict[str, list[bytes]] = {"stdout": [], "stderr": []}
    capped: list[str] = []

    def _drain(name: str, pipe: Any) -> None:
        total = 0
        try:
            while True:
                chunk = pipe.read1(_READ_CHUNK)
                if not chunk:
                    return
                if total < _OUTPUT_HARD_CAP:
                    chunks[name].append(chunk[: _OUTPUT_HARD_CAP - total])
                total += len(chunk)
                if total > _OUTPUT_HARD_CAP:
                    capped.append(name)
                    _kill_tree(proc)
                    return
        finally:
            # Closing our end makes any writer that outlived the kill fail
            # with EPIPE instead of blocking forever.
            pipe.close()

    readers = [
        threading.Thread(target=_drain, args=(name, pipe), daemon=True, name=f"bash-{name}")
        for name, pipe in (("stdout", proc.stdout), ("stderr", proc.stderr))
    ]
    for t in readers:
        t.start()
    deadline = time.monotonic() + timeout_s
    try:
        proc.wait(timeout=timeout_s)
        for t in readers:
            t.join(max(0.0, deadline - time.monotonic()))
        if any(t.is_alive() for t in readers):
            raise subprocess.TimeoutExpired(command, timeout_s)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        proc.wait()
        for t in readers:
            t.join(_DRAIN_GRACE_S)
        raise subprocess.TimeoutExpired(command, timeout_s) from None

    def _text(name: str) -> str:
        return b"".join(chunks[name]).decode("utf-8", errors="replace")

    return CappedRun(proc.returncode, _text("stdout"), _text("stderr"), tuple(capped))


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
            # Allowlisted env only: the engine process env carries
            # tenant-shared LLM/data-source credentials.
            result = run_capped(command, cwd=cwd, env=_subprocess_env(), timeout_s=timeout_s)
            # Value-based scrub here so neither the trajectory copy nor the
            # offloaded full copy (tool_result_store) carries a secret.
            stdout = redact_secret_values(result.stdout)
            stderr = redact_secret_values(result.stderr)
            if "stdout" in result.capped:
                stdout += cap_marker("stdout")
            if "stderr" in result.capped:
                stderr += cap_marker("stderr")
            payload: dict[str, Any] = {
                "status": "ok" if result.returncode == 0 else "error",
                "exit_code": result.returncode,
                "stdout": stdout,
                "stderr": stderr,
            }
            if result.capped:
                payload["output_capped"] = list(result.capped)
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
