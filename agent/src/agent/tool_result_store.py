"""Off-disk storage + explicit truncation preview for oversized tool results (V2).

Before this module every tool result was silently clipped with
``result[:10_000]`` on its way into the trajectory. The model was never told,
so a half-cut JSON document read as a complete one — it would parse the prefix,
conclude the missing rows simply did not exist, and cite that as data. The
``run_swarm`` return (a full multi-agent report bundle) was the worst case: it
routinely exceeded the limit, so the cut landed mid-JSON.

The fix follows book §2.7.4 tier 1 — write the full result to disk, hand the
model a bounded preview that says so, and tell it exactly how to read the rest.

Three tool-aware refinements (2026-09-04, review round 3 P0 — the generic
envelope was quietly disabling the two tools the model needs most):

* ``load_skill`` gets its own, much larger budget (:data:`SKILL_RESULT_LIMIT`)
  and is trimmed **by Markdown ``##`` section**, never mid-sentence; the reply
  lists the omitted section headings and the exact line to resume from.
* ``get_market_data`` keeps every symbol's ``summary`` plus the first/last
  :data:`MARKET_DATA_EDGE_ROWS` rows instead of a blind character cut, and its
  on-disk copy is written one bar per line so ``read_file`` paging by line
  is paging by bar.
* Any other single-line JSON result is pretty-printed before it hits disk, so
  ``read_file(offset, limit)`` can actually page through it.

Byte stability (book §2.3.4): file names are a deterministic function of
(iteration, tool, call id), so a replay or retry reuses the same path and the
preview text — which embeds that path — stays byte-identical. No timestamps,
no random ids.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from src.agent.context_policy import TRUNCATED_TAG

logger = logging.getLogger(__name__)

# Moved from ``loop.py`` so the offload path and the limit cannot drift apart.
TOOL_RESULT_LIMIT = 10_000
# The tail is usually where a result keeps its conclusion/totals, so the
# preview shows head + tail rather than a bare prefix.
PREVIEW_TAIL = 1_000
TOOL_RESULTS_DIRNAME = "tool-results"

# ``load_skill`` is exempt from the generic limit: a skill IS the API contract
# the model is about to code against, and 27 of the 79 bundled skills exceed
# 10k (tushare ≈ 100k). Above this budget the skill is trimmed by section.
SKILL_RESULT_LIMIT = 60_000
SKILL_TOOL_NAME = "load_skill"
# Room reserved inside SKILL_RESULT_LIMIT for the envelope + omitted-section
# list so the trimmed reply itself never breaches the budget.
_SKILL_FOOTER_RESERVE = 1_500

# ``get_market_data`` structured truncation: rows kept at each end per symbol.
MARKET_DATA_TOOL_NAME = "get_market_data"
MARKET_DATA_EDGE_ROWS = 20

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

_READ_HINT = (
    'read_file(path="{path}", offset=<start line>, limit=<line count>) pages through '
    "the file; in bash, `grep -n <pattern> {path}` finds the line to start from."
)


def _safe(part: str) -> str:
    """Return a filesystem-safe fragment for a path component."""
    cleaned = _UNSAFE_NAME_RE.sub("_", part or "unknown")
    return cleaned[:60] or "unknown"


def _is_json_like(result: str) -> bool:
    return result.lstrip()[:1] in ("{", "[")


def result_path(base_dir: Path, iteration: int, tool_name: str, call_id: str, result: str) -> Path:
    """Return the deterministic on-disk path for one oversized result.

    Args:
        base_dir: Run directory (main loop) or artifact directory (worker).
        iteration: Loop iteration the call belongs to.
        tool_name: Tool name.
        call_id: Provider tool-call id.
        result: The raw result (only its first bytes are inspected, to pick
            the extension; a ``load_skill`` result is Markdown).

    Returns:
        Path under ``<base_dir>/tool-results/``.
    """
    if tool_name == SKILL_TOOL_NAME:
        ext = "md"
    else:
        ext = "json" if _is_json_like(result) else "txt"
    name = f"{max(0, int(iteration)):03d}-{_safe(tool_name)}-{_safe(call_id)[:8]}.{ext}"
    return Path(base_dir) / TOOL_RESULTS_DIRNAME / name


def _reflow_single_line_json(result: str) -> str:
    """Pretty-print a single-line JSON document so line paging works.

    A tool that ``json.dumps`` without ``indent`` produces one physical line;
    ``read_file(offset, limit)`` pages by line, so the model could never
    reach the part the preview omitted. Anything that is not single-line
    JSON (or does not parse) is returned unchanged.
    """
    stripped = result.strip()
    if not _is_json_like(stripped) or "\n" in stripped:
        return result
    try:
        parsed = json.loads(stripped)
    except (TypeError, ValueError):
        return result
    try:
        return json.dumps(parsed, ensure_ascii=False, indent=1)
    except (TypeError, ValueError):
        return result


def disk_text(tool_name: str, result: str) -> str:
    """Return the text actually written to disk for ``result``.

    ``get_market_data`` payloads are written one bar per line (see
    :func:`_market_data_disk_text`); other single-line JSON is pretty-printed;
    everything else (Markdown skills, multi-line JSON, plain text) is stored
    verbatim so line numbers quoted in the preview stay exact.
    """
    if tool_name == MARKET_DATA_TOOL_NAME:
        payload = _parse_market_data(result)
        if payload is not None:
            return _market_data_disk_text(payload)
    return _reflow_single_line_json(result)


def offload(
    base_dir: Path,
    iteration: int,
    tool_name: str,
    call_id: str,
    result: str,
    *,
    text: str | None = None,
) -> Path:
    """Write the full result to disk and return its path.

    Args:
        base_dir: Run directory (main loop) or artifact directory (worker).
        iteration: Loop iteration the call belongs to.
        tool_name: Tool name.
        call_id: Provider tool-call id.
        result: Full raw result text (decides the file name/extension).
        text: What to write, when it differs from ``result`` (a line-paged
            reflow). Defaults to :func:`disk_text` of ``result``.

    Returns:
        Path of the written file.

    Raises:
        OSError: When the tenant volume is full or read-only. Callers degrade
            to a disk-free preview rather than failing the run.
    """
    path = result_path(base_dir, iteration, tool_name, call_id, result)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(disk_text(tool_name, result) if text is None else text, encoding="utf-8")
    return path


# ── Generic envelope ────────────────────────────────────────────────────────


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
        f"{_READ_HINT.format(path=path)}]"
    )


# ── load_skill: section-aware trimming ──────────────────────────────────────


def _split_markdown_sections(text: str) -> list[tuple[str, str, int]]:
    """Split Markdown into ``(heading, text, start_line)`` chunks at ``## ``.

    The preamble before the first level-2 heading is the first chunk with an
    empty heading. Headings inside fenced code blocks are not split points.
    ``start_line`` is 1-based, matching ``read_file(offset=...)``.
    """
    sections: list[tuple[str, str, int]] = []
    heading = ""
    buf: list[str] = []
    start = 1
    in_fence = False
    for lineno, line in enumerate(text.splitlines(keepends=True), start=1):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        if not in_fence and line.startswith("## "):
            if buf or sections or heading:
                sections.append((heading, "".join(buf), start))
            heading = line.rstrip("\n")
            buf = [line]
            start = lineno
            continue
        buf.append(line)
    sections.append((heading, "".join(buf), start))
    return sections


def _skill_name(result: str) -> str | None:
    first = result.split("\n", 1)[0]
    if first.startswith("# skill: "):
        return first[len("# skill: "):].strip() or None
    return None


def build_skill_preview(result: str, path: Path | None) -> str:
    """Trim an oversized ``load_skill`` result by ``##`` section.

    Keeps a contiguous prefix of whole sections that fits the skill budget,
    then lists every omitted section heading and the exact line to resume
    from, so the model knows what it has not read — a skill cut mid-API
    table is worse than no skill at all.
    """
    budget = SKILL_RESULT_LIMIT - _SKILL_FOOTER_RESERVE
    sections = _split_markdown_sections(result)
    kept: list[str] = []
    omitted: list[tuple[str, int]] = []
    used = 0
    for index, (heading, text, start_line) in enumerate(sections):
        if not omitted and used + len(text) <= budget:
            kept.append(text)
            used += len(text)
            continue
        if index == 0:
            # A preamble alone bigger than the budget: hard-cut it, but say so.
            kept.append(text[:budget])
            used = budget
            omitted.append(("(rest of the preamble)", 1))
            continue
        omitted.append((heading or "(untitled section)", start_line))

    shown = "".join(kept).rstrip("\n")
    resume_line = omitted[0][1] if omitted else None
    headings = ", ".join(f'"{h}" (line {ln})' for h, ln in omitted)
    envelope = (
        f'{TRUNCATED_TAG} tool="{SKILL_TOOL_NAME}" '
        f'total_chars="{len(result)}" shown="{len(shown)}" unit="sections">\n'
        f"{shown}\n"
        "</tool-result-truncated>\n"
        f"[Omitted {len(omitted)} of {len(sections)} sections: {headings}]"
    )
    if path is not None:
        return (
            f"{envelope}\n"
            f"[FULL SKILL ON DISK: {path}]\n"
            f"[The omitted sections start at line {resume_line}. "
            f"{_READ_HINT.format(path=path)}]"
        )
    name = _skill_name(result)
    if name:
        return (
            f"{envelope}\n"
            "[Full skill could NOT be written to disk (storage unavailable). The bundled "
            f'copy is readable with read_file(path="{name}/SKILL.md", offset=<start line>, '
            "limit=<line count>) — its line numbers are offset by the frontmatter block.]"
        )
    return envelope + _NO_DISK_NOTE


# ── get_market_data: structured truncation ──────────────────────────────────


def _parse_market_data(result: str) -> dict[str, Any] | None:
    """Parse a ``get_market_data`` result; None unless it is a symbol→table dict."""
    if not _is_json_like(result):
        return None
    try:
        payload = json.loads(result)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    from src.market_data import table_rows

    for key, value in payload.items():
        if isinstance(key, str) and not key.startswith("_") and table_rows(value) is not None:
            return payload
    return None


def _market_data_disk_text(payload: dict[str, Any]) -> str:
    """Serialize a market-data payload with one bar per physical line.

    Valid JSON whose ``rows`` arrays are laid out one element per line, so
    ``read_file(offset=N, limit=M)`` returns bars N..N+M and ``grep -n
    2025-03-14`` lands on the bar. Every other value stays compact.
    """
    from src.market_data import dumps_compact, table_rows

    lines = ["{"]
    items = list(payload.items())
    for index, (key, value) in enumerate(items):
        trailing = "," if index < len(items) - 1 else ""
        table = table_rows(value)
        if table is None:
            lines.append(f"{json.dumps(key, ensure_ascii=False)}: {dumps_compact(value)}{trailing}")
            continue
        _columns, rows = table
        lines.append(f"{json.dumps(key, ensure_ascii=False)}: {{")
        for field, field_value in value.items():
            if field == "rows":
                continue
            lines.append(f' {json.dumps(field, ensure_ascii=False)}: {dumps_compact(field_value)},')
        lines.append(' "rows": [')
        for row_index, row in enumerate(rows):
            comma = "," if row_index < len(rows) - 1 else ""
            lines.append(f"  {dumps_compact(row)}{comma}")
        lines.append(" ]")
        lines.append(f"}}{trailing}")
    lines.append("}")
    return "\n".join(lines) + "\n"


def _trim_market_data(payload: dict[str, Any], edge: int) -> tuple[dict[str, Any], int]:
    """Return a copy keeping ``summary`` + first/last ``edge`` rows per symbol."""
    from src.market_data import table_rows

    trimmed: dict[str, Any] = {}
    dropped_total = 0
    for key, value in payload.items():
        table = table_rows(value)
        if table is None or len(table[1]) <= 2 * edge:
            trimmed[key] = value
            continue
        _columns, rows = table
        dropped = len(rows) - 2 * edge
        dropped_total += dropped
        copy = {k: v for k, v in value.items() if k != "rows"}
        copy["rows_omitted"] = dropped
        copy["rows_shown"] = f"first {edge} + last {edge}"
        copy["rows"] = rows[:edge] + rows[-edge:]
        trimmed[key] = copy
    return trimmed, dropped_total


def build_market_data_preview(
    payload: dict[str, Any], total_chars: int, path: Path | None
) -> str | None:
    """Structured preview for an oversized ``get_market_data`` result.

    Every symbol keeps its ``summary`` block and the first/last
    :data:`MARKET_DATA_EDGE_ROWS` bars; if that still exceeds the limit (many
    symbols) the edge shrinks, and past that the caller falls back to the
    generic character envelope (returns None).
    """
    from src.market_data import dumps_compact

    for edge in (MARKET_DATA_EDGE_ROWS, 5):
        trimmed, dropped = _trim_market_data(payload, edge)
        body = dumps_compact(trimmed)
        if len(body) <= TOOL_RESULT_LIMIT:
            break
    else:
        return None
    envelope = (
        f'{TRUNCATED_TAG} tool="{MARKET_DATA_TOOL_NAME}" '
        f'total_chars="{total_chars}" shown="{len(body)}" unit="rows">\n'
        f"{body}\n"
        "</tool-result-truncated>\n"
        f"[STRUCTURED PREVIEW: valid JSON; every symbol keeps its summary (start/end, "
        f"first/last close, high/low, change_pct) plus the first {edge} and last {edge} "
        f'bars; "rows_omitted" counts the middle bars dropped ({dropped} in total). '
        "Do NOT conclude the omitted bars are missing from the source.]"
    )
    if path is None:
        return (
            f"{envelope}\n"
            "[Full result could NOT be written to disk (storage unavailable). Re-run with "
            "a narrower date range, a coarser interval, or fewer symbols for the middle bars.]"
        )
    return (
        f"{envelope}\n"
        f"[FULL RESULT ON DISK: {path} — one bar per line under each symbol's \"rows\". "
        f"{_READ_HINT.format(path=path)}]"
    )


# ── Dispatcher ──────────────────────────────────────────────────────────────


def _try_offload(
    base_dir: Path | None, iteration: int, tool_name: str, call_id: str, result: str
) -> Path | None:
    if base_dir is None:
        return None
    try:
        return offload(Path(base_dir), iteration, tool_name, call_id, result)
    except OSError as exc:  # noqa: BLE001 - storage failure must not kill the run
        logger.warning("tool-result offload failed for %s: %s", tool_name, exc)
        return None


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

    Tool-aware: ``load_skill`` uses :data:`SKILL_RESULT_LIMIT` and section
    trimming; ``get_market_data`` uses the structured row preview; every
    other tool gets the generic head+tail envelope at
    :data:`TOOL_RESULT_LIMIT`. The dispatch lives here (not in ``loop.py``)
    so the main loop and the swarm worker cannot drift.

    Args:
        result: Full raw result text.
        base_dir: Directory to offload into, or None to skip the disk write.
        iteration: Loop iteration the call belongs to.
        tool_name: Tool name.
        call_id: Provider tool-call id.

    Returns:
        Tuple of (payload, offload_failed).
    """
    if tool_name == SKILL_TOOL_NAME:
        if len(result) <= SKILL_RESULT_LIMIT:
            return result, False
        path = _try_offload(base_dir, iteration, tool_name, call_id, result)
        return build_skill_preview(result, path), path is None

    if len(result) <= TOOL_RESULT_LIMIT:
        return result, False

    path = _try_offload(base_dir, iteration, tool_name, call_id, result)
    if tool_name == MARKET_DATA_TOOL_NAME:
        payload = _parse_market_data(result)
        if payload is not None:
            preview = build_market_data_preview(payload, len(result), path)
            if preview is not None:
                return preview, path is None
    return build_preview(result, path, tool_name), path is None
