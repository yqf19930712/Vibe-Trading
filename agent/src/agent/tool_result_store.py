"""Off-disk storage + explicit truncation preview for oversized tool results (V2).

Before this module every tool result was silently clipped with
``result[:10_000]`` on its way into the trajectory. The model was never told,
so a half-cut JSON document read as a complete one — it would parse the prefix,
conclude the missing rows simply did not exist, and cite that as data. The
``run_swarm`` return (a full multi-agent report bundle) was the worst case: it
routinely exceeded the limit, so the cut landed mid-JSON.

The fix follows book §2.7.4 tier 1 — write the full result to disk, hand the
model a bounded preview that says so, and tell it exactly how to read the rest.

Byte stability (book §2.3.4): file names are a deterministic function of
(iteration, tool, call id), so a replay or retry reuses the same path and the
preview text — which embeds that path — stays byte-identical. No timestamps,
no random ids.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from src.agent.context_policy import TRUNCATED_TAG

logger = logging.getLogger(__name__)

# Moved from ``loop.py`` so the offload path and the limit cannot drift apart.
TOOL_RESULT_LIMIT = 10_000
# The tail is usually where a result keeps its conclusion/totals, so the
# preview shows head + tail rather than a bare prefix.
PREVIEW_TAIL = 1_000
TOOL_RESULTS_DIRNAME = "tool-results"

# Keep name fragments filesystem-safe. Tool names are internal identifiers,
# but a remote MCP tool name is not under our control. Dots are excluded along
# with separators so no fragment can ever be ``..`` — the extension is appended
# by us, not taken from the input.
_UNSAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_-]")

_NO_DISK_NOTE = (
    "\n[Full result could NOT be written to disk (storage unavailable). "
    "The text above is a PREVIEW with the middle omitted — do not parse it as "
    "a whole, and do not conclude the omitted data is missing from the source. "
    "Re-run the tool with narrower arguments to get a smaller result.]"
)


def _safe(part: str) -> str:
    """Return a filesystem-safe fragment for a path component."""
    cleaned = _UNSAFE_NAME_RE.sub("_", part or "unknown")
    return cleaned[:60] or "unknown"


def result_path(base_dir: Path, iteration: int, tool_name: str, call_id: str, result: str) -> Path:
    """Return the deterministic on-disk path for one oversized result.

    Args:
        base_dir: Run directory (main loop) or artifact directory (worker).
        iteration: Loop iteration the call belongs to.
        tool_name: Tool name.
        call_id: Provider tool-call id.
        result: The raw result (only its first bytes are inspected, to pick
            the extension).

    Returns:
        Path under ``<base_dir>/tool-results/``.
    """
    ext = "json" if result.lstrip()[:1] in ("{", "[") else "txt"
    name = f"{max(0, int(iteration)):03d}-{_safe(tool_name)}-{_safe(call_id)[:8]}.{ext}"
    return Path(base_dir) / TOOL_RESULTS_DIRNAME / name


def offload(base_dir: Path, iteration: int, tool_name: str, call_id: str, result: str) -> Path:
    """Write the full result to disk and return its path.

    Args:
        base_dir: Run directory (main loop) or artifact directory (worker).
        iteration: Loop iteration the call belongs to.
        tool_name: Tool name.
        call_id: Provider tool-call id.
        result: Full raw result text.

    Returns:
        Path of the written file.

    Raises:
        OSError: When the tenant volume is full or read-only. Callers degrade
            to a disk-free preview rather than failing the run.
    """
    path = result_path(base_dir, iteration, tool_name, call_id, result)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(result, encoding="utf-8")
    return path


def build_preview(result: str, path: Path | None, tool_name: str) -> str:
    """Build the explicit, byte-stable truncation envelope.

    Args:
        result: Full raw result text (longer than :data:`TOOL_RESULT_LIMIT`).
        path: Where the full copy lives, or None when the write failed.
        tool_name: Tool name, echoed into the envelope attributes.

    Returns:
        Preview text to place in the trajectory in place of ``result``.
    """
    head = result[: TOOL_RESULT_LIMIT - PREVIEW_TAIL]
    tail = result[-PREVIEW_TAIL:]
    omitted = len(result) - TOOL_RESULT_LIMIT
    envelope = (
        f'{TRUNCATED_TAG} tool="{_safe(tool_name)}" '
        f'total_chars="{len(result)}" shown="{TOOL_RESULT_LIMIT}">\n'
        f"{head}\n"
        f"\n...[{omitted} chars omitted from the middle]...\n\n"
        f"{tail}\n"
        "</tool-result-truncated>"
    )
    if path is None:
        return envelope + _NO_DISK_NOTE
    return (
        f"{envelope}\n"
        f"[FULL RESULT ON DISK: {path}]\n"
        "[This is a PREVIEW, not the complete result. It may be syntactically "
        "incomplete (e.g. unbalanced JSON) — do NOT parse it as a whole and do "
        "NOT conclude data is missing from the source. To read the rest: "
        f'read_file(path="{path}", offset=<line>, limit=<n>) or grep_file.]'
    )


def prepare_for_context(
    result: str,
    *,
    base_dir: Path | None,
    iteration: int,
    tool_name: str,
    call_id: str,
) -> tuple[str, bool]:
    """Return the trajectory payload for a tool result, offloading if oversized.

    Never raises: a storage failure degrades to a marked, disk-free preview.
    The raw ``result`` is untouched — the error classifier, the grounding
    verifier and the trace all keep consuming the full text.

    Args:
        result: Full raw result text.
        base_dir: Directory to offload into, or None to skip the disk write.
        iteration: Loop iteration the call belongs to.
        tool_name: Tool name.
        call_id: Provider tool-call id.

    Returns:
        Tuple of (payload, offload_failed).
    """
    if len(result) <= TOOL_RESULT_LIMIT:
        return result, False
    if base_dir is None:
        return build_preview(result, None, tool_name), True
    try:
        path = offload(Path(base_dir), iteration, tool_name, call_id, result)
    except OSError as exc:  # noqa: BLE001 - storage failure must not kill the run
        logger.warning("tool-result offload failed for %s: %s", tool_name, exc)
        return build_preview(result, None, tool_name), True
    return build_preview(result, path, tool_name), False
