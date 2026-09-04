"""Minimal environment for shell subprocesses spawned by the agent's tools.

Why this exists (review 2026-09-04, P0): ``bash`` / ``background_run`` used
to inherit the whole engine process env. In the multi-tenant deployment that
env carries the SHARED builtin LLM credentials (``OPENAI_API_KEY`` /
``ANTHROPIC_*``), the data-source tokens (``TUSHARE_TOKEN``, ``JINA_API_KEY``,
``IFIND_MCP_TOKEN`` …) and the engine's own Bearer key (``API_AUTH_KEY``) —
so a single ``env`` command run by the model dumped every tenant-shared
secret into the tool result, the trace and the LLM context.

The engine's own LLM calls are unaffected: those are in-process httpx calls
that read ``os.environ`` directly, not subprocesses.

Policy (deny wins over allow):

* allowed: a fixed set of plumbing names (``PATH``, ``HOME``, locale, ``TZ``,
  ``TMPDIR``, python venv vars) plus every ``VIBE_*`` tenant flag;
* denied regardless: any name matching ``*_KEY`` / ``*_TOKEN`` / ``*_SECRET``
  / ``*_PASSWORD`` (also as an interior segment, e.g. ``*_KEY_B64``) and
  anything under the ``OPENAI_`` / ``ANTHROPIC_`` / ``LANGCHAIN_`` prefixes.
"""

from __future__ import annotations

import os
from typing import Mapping

ALLOWED_EXACT: frozenset[str] = frozenset(
    {
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TERM",
        "TMPDIR",
        "USER",
        "SHELL",
        "PYTHONPATH",
        "PYTHONIOENCODING",
        "VIRTUAL_ENV",
        "TZ",
    }
)
ALLOWED_PREFIXES: tuple[str, ...] = ("VIBE_",)

DENIED_PREFIXES: tuple[str, ...] = ("OPENAI_", "ANTHROPIC_", "LANGCHAIN_")
# Matched as ``<seg>`` at the end of the name or followed by ``_`` (so
# ``VIBE_EGRESS_SSH_KEY_B64`` is caught, ``VIBE_TRADING_KEYWORDS`` is not).
_DENIED_SEGMENTS: tuple[str, ...] = ("_KEY", "_TOKEN", "_SECRET", "_PASSWORD", "_PASSWD")


def has_secret_segment(name: str) -> bool:
    """Return whether an env var NAME carries a credential-looking segment.

    Shared by the subprocess allowlist (drop the var) and the value-based
    redaction in :mod:`src.tools.redaction` (scrub the value from tool
    output). Deliberately NOT prefix-based: ``LANGCHAIN_MODEL_NAME`` or
    ``OPENAI_BASE_URL`` hold no secret and their values must stay readable
    in tool output — the prefixes are only a subprocess-side denial (see
    :func:`is_secret_env_name`). Regex-free on purpose (zero ReDoS surface,
    like the rest of the redaction helpers).

    Args:
        name: Environment variable name.

    Returns:
        ``True`` for ``*_KEY`` / ``*_TOKEN`` / ``*_SECRET`` / ``*_PASSWORD``
        style names (suffix or interior segment such as ``*_KEY_B64``).
    """
    upper = name.upper()
    for seg in _DENIED_SEGMENTS:
        idx = upper.find(seg)
        while idx != -1:
            end = idx + len(seg)
            if end == len(upper) or upper[end] == "_":
                return True
            idx = upper.find(seg, end)
    return False


def is_secret_env_name(name: str) -> bool:
    """Subprocess-side denial: secret segment OR a whole credential family.

    The ``OPENAI_`` / ``ANTHROPIC_`` / ``LANGCHAIN_`` prefixes are dropped
    wholesale from the child env (base URLs and model names are useless to a
    shell command and only leak topology), on top of the segment rule.
    """
    return name.upper().startswith(DENIED_PREFIXES) or has_secret_segment(name)


def _subprocess_env(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """Build the env dict handed to ``subprocess.run`` by the shell tools.

    Args:
        source: Environment to filter (defaults to ``os.environ``).

    Returns:
        A new dict containing only allowlisted, non-secret variables.
    """
    env = os.environ if source is None else source
    out: dict[str, str] = {}
    for name, value in env.items():
        if is_secret_env_name(name):
            continue
        if name in ALLOWED_EXACT or name.startswith(ALLOWED_PREFIXES):
            out[name] = value
    return out
