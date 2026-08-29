"""Web search tool: free multi-engine search via ddgs (no API key).

`ddgs` (the successor to `duckduckgo_search`) is a metasearch aggregator: it can
query DuckDuckGo, Google, Bing, Brave, Mojeek, Yahoo and more behind one API,
none of which need an API key.  DuckDuckGo alone rate-limits aggressively from
cloud / shared IPs (issue #231: ``web_search`` showed ❌ while the run still
succeeded via ``read_url``), so we pass an explicit ordered backend list and let
ddgs fall through a throttled engine to the next one, with a short retry/backoff
on top for transient failures.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from src.agent.tools import BaseTool
from src.security.scanner import with_security_warnings

logger = logging.getLogger(__name__)

# Free, no-key engines aggregated by ddgs, tried in order. A single engine
# returning nothing or being rate-limited no longer fails the whole search.
# Override (or pin to one engine) via VIBE_TRADING_SEARCH_BACKENDS.
# ddgs 9.x dropped the google/bing backends (requesting them logs a warning
# and shrinks the pool), and datacenter egress IPs get refused by individual
# engines unpredictably — "auto" lets ddgs rotate everything it has,
# including the wikipedia/grokipedia fallbacks.
_DEFAULT_BACKENDS = "auto"
_MAX_ATTEMPTS = 3
_BACKOFF_BASE_SECONDS = 0.8


def _make_client(ddgs_cls, proxy: str | None):
    """Build the DDGS client, passing the egress proxy when configured.

    In restricted-egress deployments (multi-tenant sandboxes behind the
    firewall) VIBE_TRADING_EGRESS_PROXY points at an in-guest encrypted
    tunnel to a domain-whitelisted proxy; without it, foreign engines are
    simply unreachable. Handles the proxy kwarg rename across ddgs versions.
    """
    if proxy:
        try:
            return ddgs_cls(proxy=proxy)
        except TypeError:
            try:
                return ddgs_cls(proxies=proxy)
            except TypeError:
                logger.warning("ddgs client accepts no proxy kwarg; searching direct")
    return ddgs_cls()


class WebSearchTool(BaseTool):
    """Search the web via ddgs across several free engines and return top results."""

    name = "web_search"

    @classmethod
    def check_available(cls) -> bool:
        """Available only if ddgs or duckduckgo_search is installed."""
        try:
            try:
                import ddgs  # noqa: F401
            except ImportError:
                import duckduckgo_search  # noqa: F401
            return True
        except ImportError:
            return False
    # Deliberately no engine roster here: ddgs 9.x removed the google/bing
    # backends and the pool now rotates under "auto". A hard-coded list in the
    # description is how the model ends up naming engines deleted two releases
    # ago — and it has no way to pick one anyway (there is no engine parameter).
    description = (
        "Search the web across the free, no-key engines ddgs can reach — they are "
        "rotated automatically, so a rate-limited engine falls through to the next. "
        "Returns top results with title, URL, and snippet. Use this to find "
        "information, news, or URLs before reading them with read_url."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query",
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum number of results to return (default 5, max 10)",
                "default": 5,
            },
        },
        "required": ["query"],
    }
    repeatable = True

    def execute(self, **kwargs: Any) -> str:
        """Run a web search across free engines with retry and backend fallback.

        Args:
            **kwargs: Must include query; optionally max_results.

        Returns:
            JSON envelope with status, query, the backend list used, and results
            (or an actionable error message on persistent failure).
        """
        query = kwargs["query"]
        max_results = min(int(kwargs.get("max_results", 5)), 10)
        backends = os.getenv("VIBE_TRADING_SEARCH_BACKENDS", _DEFAULT_BACKENDS).strip() or "auto"

        try:
            from ddgs import DDGS

            supports_backend = True
        except ImportError:
            try:
                from duckduckgo_search import DDGS  # legacy package, no engine selection
            except ImportError:
                return json.dumps(
                    {
                        "status": "error",
                        "error": "Web search package not installed. Run: pip install ddgs",
                    },
                    ensure_ascii=False,
                )
            supports_backend = False

        egress_proxy = os.getenv("VIBE_TRADING_EGRESS_PROXY", "").strip() or None
        last_error: Exception | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                with _make_client(DDGS, egress_proxy) as client:
                    if supports_backend:
                        raw = list(client.text(query, max_results=max_results, backend=backends))
                    else:
                        raw = list(client.text(query, max_results=max_results))
            except TypeError:
                # Older ddgs/duckduckgo_search without the backend kwarg.
                supports_backend = False
                continue
            except Exception as exc:  # noqa: BLE001 — surface a clean error to the agent
                last_error = exc
                # "No results found" is a definitive empty answer, not a transient
                # failure — retrying or switching engines won't change it.
                if "no results" in str(exc).lower():
                    return json.dumps(
                        {
                            "status": "ok",
                            "query": query,
                            "backends": backends if supports_backend else "duckduckgo",
                            "results": [],
                            "note": "No results found for this query across the search engines.",
                        },
                        ensure_ascii=False,
                    )
                logger.warning("web_search attempt %d/%d failed: %s", attempt, _MAX_ATTEMPTS, exc)
                if attempt < _MAX_ATTEMPTS:
                    time.sleep(_BACKOFF_BASE_SECONDS * attempt)
                continue

            results = [
                {
                    "title": r.get("title", ""),
                    "url": r.get("href", ""),
                    "snippet": r.get("body", ""),
                }
                for r in raw
            ]
            payload = {
                "status": "ok",
                "query": query,
                "backends": backends if supports_backend else "duckduckgo",
                "results": results,
            }
            payload = with_security_warnings(
                payload,
                fields=("results.*.title", "results.*.snippet"),
            )
            return json.dumps(payload, ensure_ascii=False)

        return json.dumps(
            {
                "status": "error",
                "error": (
                    # Only actions the MODEL can take belong here. The old text
                    # told it to set VIBE_TRADING_SEARCH_BACKENDS (an operator
                    # env var it cannot touch) and named google/bing, which
                    # ddgs 9.x no longer has.
                    f"Web search failed after {_MAX_ATTEMPTS} attempts "
                    f"(backends: {backends if supports_backend else 'duckduckgo'}): {last_error}. "
                    "Free search engines rate-limit aggressively from cloud/shared IPs — "
                    "retry shortly, or read a known URL directly with read_url."
                ),
            },
            ensure_ascii=False,
        )
