"""Web reader tool: fetch a URL as Markdown text via the Jina Reader API."""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import socket
import threading
from urllib.parse import urlsplit

import requests

from src.agent.progress import emit_progress
from src.agent.tools import BaseTool
from src.security.scanner import with_security_warnings, wrap_external_content

logger = logging.getLogger(__name__)

_JINA_PREFIX = "https://r.jina.ai/"
# (connect, read)：连接 5s 快败、读 30s——r.jina.ai 不可达时 30s 的连接死等会在
# 单次调用里烧掉 90s。
_TIMEOUT = (5, 30)


def _proxies() -> dict[str, str] | None:
    """沙箱内 r.jina.ai 须经白名单出境隧道（同 web_search/yfinance）。"""
    proxy = os.getenv("VIBE_TRADING_EGRESS_PROXY", "").strip()
    if not proxy:
        return None
    return {"http": proxy, "https": proxy}
_MAX_LENGTH = 8000
_CACHED_MARKER = "Warning: This is a cached snapshot"


_NUMERIC_HOST_RE = re.compile(r"^(0x[0-9a-f]*|[0-9]+)(\.(0x[0-9a-f]*|[0-9]+)){0,3}$")
# Resolving the host is a best-effort extra check (the fetch itself runs on
# r.jina.ai's side), so it gets a short, bounded wait.
_RESOLVE_TIMEOUT_S = 2.0


def _legacy_ipv4(host: str) -> ipaddress.IPv4Address | None:
    """Decode the inet_aton spellings browsers and curl still accept.

    ``2852039166``, ``0xa9fea9fe``, ``0251.0376.0251.0376`` and ``169.254.43518``
    all mean 169.254.169.254; ``ipaddress`` rejects them, so they used to pass
    as ordinary host names.
    """
    if not _NUMERIC_HOST_RE.match(host):
        return None
    values: list[int] = []
    for part in host.split("."):
        try:
            if part.startswith("0x"):
                values.append(int(part[2:] or "0", 16))
            elif len(part) > 1 and part.startswith("0"):
                values.append(int(part, 8))
            else:
                values.append(int(part, 10))
        except ValueError:
            return None
    head, last = values[:-1], values[-1]
    if any(v > 255 for v in head) or last >= 1 << (8 * (4 - len(head))):
        return None
    number = 0
    for v in head:
        number = (number << 8) | v
    number = (number << (8 * (4 - len(head)))) | last
    return ipaddress.IPv4Address(number)


def _is_public(ip: ipaddress._BaseAddress) -> bool:
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or not ip.is_global
    )


def _resolves_to_non_public(host: str) -> bool:
    """Whether ``host`` resolves (here, within a short wait) to a non-public address.

    Only consulted for hosted tenants; an unresolvable host or a slow
    resolver is not a rejection — the remote reader resolves on its side.
    """
    from src.config.tenant import tenant_profile_active

    if not tenant_profile_active():
        return False
    found: list[str] = []

    def _lookup() -> None:
        try:
            found.extend(info[4][0] for info in socket.getaddrinfo(host, None))
        except (OSError, UnicodeError):
            pass

    worker = threading.Thread(target=_lookup, daemon=True)
    worker.start()
    worker.join(_RESOLVE_TIMEOUT_S)
    for address in list(found):
        try:
            if not _is_public(ipaddress.ip_address(address.split("%", 1)[0])):
                return True
        except ValueError:
            continue
    return False


def _url_allowed(url: str) -> tuple[bool, str]:
    """Return whether a URL is safe to forward to the remote reader service."""
    try:
        parsed = urlsplit(url.strip())
    except ValueError:
        return False, "target URL is not allowed"

    if parsed.scheme.lower() not in {"http", "https"}:
        return False, "target URL is not allowed"
    if not parsed.hostname:
        return False, "target URL is not allowed"
    if parsed.username or parsed.password:
        return False, "target URL is not allowed"

    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        return False, "target URL is not allowed"

    ip_host = host.split("%", 1)[0]
    try:
        ip: ipaddress._BaseAddress | None = ipaddress.ip_address(ip_host)
    except ValueError:
        ip = _legacy_ipv4(ip_host)

    if ip is None:
        if _resolves_to_non_public(host):
            return False, "target URL is not allowed"
        return True, ""
    if not _is_public(ip):
        return False, "target URL is not allowed"
    return True, ""


def read_url(url: str, no_cache: bool = False) -> str:
    """Fetch web page content via the Jina Reader API.

    The full URL (including query string) is sent to the third-party Jina
    Reader service (r.jina.ai); never pass credentials/tokens or private
    addresses. Results may be a cached snapshot.

    Args:
        url: Target URL.
        no_cache: When true, ask the reader for a fresh (uncached) fetch.

    Returns:
        JSON result with title, content, url; ``cached: true`` is added
        when the reader served a stale snapshot.
    """
    target_url = url.strip()
    allowed, error = _url_allowed(target_url)
    if not allowed:
        return json.dumps({"status": "error", "error": error}, ensure_ascii=False)

    try:
        headers = {"Accept": "text/markdown"}
        # Jina 对部分数据中心 ASN（含 server B 出口 AS20473）禁止匿名请求
        # （401 AuthenticationRequiredError）；配置免费 API key 即可解除。
        api_key = os.getenv("JINA_API_KEY", "").strip()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        if no_cache:
            headers["x-no-cache"] = "true"
        emit_progress(
            "fetching",
            message=f"GET {target_url[:60]}{'…' if len(target_url) > 60 else ''}",
        )
        resp = requests.get(
            f"{_JINA_PREFIX}{target_url}",
            headers=headers,
            timeout=_TIMEOUT,
            proxies=_proxies(),
        )
        emit_progress("parsing", message="extracting markdown")
        if resp.status_code != 200:
            logger.warning("read_url upstream HTTP %s: %s", resp.status_code, resp.text[:500])
            return json.dumps({
                "status": "error",
                "error": f"remote reader returned HTTP {resp.status_code}: {resp.text[:500]}",
            }, ensure_ascii=False)

        text = resp.text
        title = ""
        for line in text.split("\n"):
            if line.startswith("Title:"):
                title = line[6:].strip()
                break

        if len(text) > _MAX_LENGTH:
            text = text[:_MAX_LENGTH] + f"\n\n... (truncated, total {len(resp.text)} chars)"

        result = {
            "status": "ok",
            "title": title,
            "url": target_url,
            "content": text,
            "length": len(resp.text),
        }
        if _CACHED_MARKER in resp.text:
            result["cached"] = True
        result = with_security_warnings(result, fields=("content",))
        # Declare the page body as untrusted DATA, mirroring what the
        # recalled-memories block does for stored content. Otherwise the page
        # text sits bare in the trajectory while the scanner's verdict lives
        # in a JSON field the model reads last, if at all.
        result["content"] = wrap_external_content(
            result["content"],
            source=target_url,
            kind="web_page",
            findings=result.get("security_warnings"),
        )
        return json.dumps(result, ensure_ascii=False)

    except requests.Timeout:
        return json.dumps({"status": "error", "error": f"Request timed out ({_TIMEOUT}s)"}, ensure_ascii=False)
    except Exception as exc:
        logger.warning("read_url request failed: %s", exc)
        return json.dumps(
            {"status": "error", "error": f"remote reader request failed: {exc}"},
            ensure_ascii=False,
        )


class WebReaderTool(BaseTool):
    """Web reader tool."""

    name = "read_url"
    description = "Fetch web page content: provide a URL and receive the page as Markdown text. Useful for reading docs, articles, API references, etc."
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL of the web page to read"},
            "no_cache": {"type": "boolean", "description": "Request a fresh (uncached) fetch", "default": False},
        },
        "required": ["url"],
    }
    repeatable = True

    def execute(self, **kwargs) -> str:
        """Fetch web page."""
        return read_url(kwargs["url"], no_cache=bool(kwargs.get("no_cache", False)))
