"""Minimal environment for every subprocess the engine spawns for the model.

Two policies live here: ``_subprocess_env()`` for ``bash`` / ``background_run``
and MCP stdio servers, and ``backtest_subprocess_env()`` for the backtest
Runner (the shell allowlist plus the data-source tokens and network plumbing
the loaders read in that child).

Why this exists: the engine process env in the multi-tenant deployment
carries the SHARED builtin LLM credentials (``OPENAI_API_KEY`` /
``ANTHROPIC_*``), the data-source tokens (``TUSHARE_TOKEN``, ``JINA_API_KEY``,
``IFIND_MCP_TOKEN`` …) and the engine's own Bearer key (``API_AUTH_KEY``) —
if ``bash`` / ``background_run`` inherited it, a single ``env`` command run by
the model would dump every tenant-shared secret into the tool result, the
trace and the LLM context.

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

# Backtest subprocess (``src/core/runner.py``): the model's ``signal_engine.py``
# is imported in that process, so it gets the same allowlist as ``bash`` plus
# only what the data loaders in ``backtest/loaders`` actually read there —
# the three data-source tokens they authenticate with, the loader tuning
# knobs, and network plumbing (proxy / CA bundle) so OKX / yfinance / ccxt can
# reach their endpoints. LLM credentials, the engine's ``API_AUTH_KEY``,
# ``JINA_API_KEY`` and ``ROUTER_*`` never cross into it.
BACKTEST_DATA_TOKENS: tuple[str, ...] = ("TUSHARE_TOKEN", "TICKFLOW_API_KEY", "IFIND_MCP_TOKEN")
BACKTEST_ALLOWED_EXACT: frozenset[str] = frozenset(
    {
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "all_proxy",
        "SSL_CERT_FILE",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        # Windows: CPython needs SYSTEMROOT to start, data libraries cache
        # under the profile directories.
        "SYSTEMROOT",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "TEMP",
        "TMP",
    }
)
# Loader tuning knobs (base URLs, timeouts, throttles); a credential-looking
# name under these prefixes is still dropped unless listed in
# ``BACKTEST_DATA_TOKENS``.
BACKTEST_ALLOWED_PREFIXES: tuple[str, ...] = (
    "TUSHARE_",
    "TICKFLOW_",
    "IFIND_",
    "CCXT_",
    "OKX_",
    "FUTU_",
    "RSSHUB_",
)

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


def backtest_subprocess_env(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """Build the env dict for the backtest runner subprocess.

    The shell allowlist (:func:`_subprocess_env`) plus the data-source tokens
    and network/loader plumbing listed in ``BACKTEST_*`` above. Every other
    credential (LLM keys, ``API_AUTH_KEY``, ``JINA_API_KEY``, ``ROUTER_*``)
    stays out: the model-written strategy code runs in that process.

    Args:
        source: Environment to filter (defaults to ``os.environ``).

    Returns:
        A new dict for ``subprocess.run(env=...)``.
    """
    env = os.environ if source is None else source
    out = _subprocess_env(env)
    for name, value in env.items():
        if name in BACKTEST_DATA_TOKENS:
            out[name] = value
        elif name in BACKTEST_ALLOWED_EXACT:
            out[name] = value
        elif name.startswith(BACKTEST_ALLOWED_PREFIXES) and not is_secret_env_name(name):
            out[name] = value
    return out
