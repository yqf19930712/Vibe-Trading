"""PersistentMemory: file-based cross-session memory, zero external dependencies.

Storage layout:
    ~/.vibe-trading/memory/
    +-- MEMORY.md          # Index (< 200 lines)
    +-- user_prefs.md      # Individual memory entries with YAML frontmatter
    +-- project_btc.md
    +-- ...
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from src.agent.frontmatter import parse_frontmatter as _parse_frontmatter
from typing import List, Optional

logger = logging.getLogger(__name__)

class MemoryWriteError(RuntimeError):
    """Raised when a memory entry cannot be persisted (full / read-only disk).

    Every tenant volume has a hard size cap, and the failure mode past it used
    to be an unhandled ``OSError`` from ``Path.write_text`` that propagated all
    the way up and failed the attempt. Callers catch this and return a
    structured tool error: losing one memory write must not lose the answer.
    """


MEMORY_BASE = Path.home() / ".vibe-trading" / "memory"
MAX_INDEX_LINES = 200
# V2 (P2-11): once the index gets this long the engine runs one consolidation
# pass on its own at run end, instead of waiting for the model to notice the
# F7① "index is full" warning and call consolidate_memory itself. Past
# MAX_INDEX_LINES new entries stop appearing in the session-start snapshot
# altogether, so the tidy-up has to happen BEFORE the cap, not at it.
AUTO_CONSOLIDATE_INDEX_LINES = 180
MAX_ENTRY_CHARS = 8000
MAX_RESULTS = 5
METADATA_WEIGHT = 2.0
MEMORY_TYPES = ("user", "feedback", "project", "reference")

# Script ranges tokenized and slugged at char level (no word-boundary
# whitespace). Arabic/Hebrew narrowed to letter blocks to exclude bidi
# controls and combining marks from on-disk slugs.
_NON_LATIN_SCRIPT_RANGES = (
    "一-鿿"   # CJK Unified Ideographs   (U+4E00-U+9FFF)
    "㐀-䶿"   # CJK Extension A          (U+3400-U+4DBF)
    "฀-๿"   # Thai                     (U+0E00-U+0E7F)
    "ؠ-ي"   # Arabic letters           (U+0620-U+064A)
    "א-ת"   # Hebrew letters           (U+05D0-U+05EA)
    "Ѐ-ӿ"   # Cyrillic                 (U+0400-U+04FF)
)

_TOKEN_RE = re.compile(rf"[a-zA-Z0-9]{{3,}}|[{_NON_LATIN_SCRIPT_RANGES}]")
_SLUG_DISALLOWED_RE = re.compile(rf"[^a-z0-9_\-{_NON_LATIN_SCRIPT_RANGES}]")


@dataclass(frozen=True)
class MemoryEntry:
    """A single memory entry on disk.

    Attributes:
        path: File path.
        title: Memory title.
        description: One-line description (used for retrieval scoring).
        memory_type: Category (user/feedback/project/reference).
        body: Body text content.
        modified_at: File modification timestamp.
        created: ISO timestamp from frontmatter (empty for legacy entries).
        source: Optional provenance note from frontmatter (empty for legacy
            entries) — what conversation/tool/task produced this memory.
    """

    path: Path
    title: str
    description: str
    memory_type: str
    body: str
    modified_at: float
    created: str = ""
    source: str = ""


def _tokenize(text: str) -> set[str]:
    """Split text into searchable tokens.

    ASCII words >= 3 chars + individual characters from non-Latin scripts
    listed in ``_NON_LATIN_SCRIPT_RANGES`` (CJK, Thai, Arabic, Hebrew,
    Cyrillic), plus adjacent-pair 2-grams of those characters (F7④: single
    CJK chars are far too promiscuous — "分" matches half the corpus — so
    scoring weights 2-grams full and lone chars low). Underscores are
    treated as word boundaries so snake_case titles (e.g. ``mcp_wiring_test``)
    match natural-language queries (``"mcp wiring"``) as well as verbatim
    lookups.

    Args:
        text: Input text.

    Returns:
        Set of tokens (1-char non-Latin tokens, non-Latin 2-grams, ASCII words).
    """
    lowered = text.lower()
    tokens = set(_TOKEN_RE.findall(lowered))
    # Non-Latin 2-grams: pairs of ADJACENT script chars in the original text
    # (runs of consecutive script chars), so "比特币" yields 比特/特币 but a
    # boundary like "价格,走势" does not bridge the comma.
    for run in _NON_LATIN_RUN_RE.findall(lowered):
        tokens.update(run[i : i + 2] for i in range(len(run) - 1))
    return tokens


#: Weight applied to lone non-Latin (e.g. single CJK) character tokens when
#: scoring — they carry little signal on their own (F7④).
SINGLE_CJK_WEIGHT = 0.3
#: Recency bonus: score is multiplied by ``1 + RECENCY_WEIGHT * freshness``
#: where freshness decays linearly from 1 (just modified) to 0 over
#: ``RECENCY_HORIZON_DAYS`` (F7④).
RECENCY_WEIGHT = 0.1
RECENCY_HORIZON_DAYS = 30.0

_NON_LATIN_RUN_RE = re.compile(rf"[{_NON_LATIN_SCRIPT_RANGES}]{{2,}}")
_NON_LATIN_CHAR_RE = re.compile(rf"^[{_NON_LATIN_SCRIPT_RANGES}]$")


def _token_weight(token: str) -> float:
    """Return the scoring weight of one token (lone non-Latin chars count low)."""
    if _NON_LATIN_CHAR_RE.match(token):
        return SINGLE_CJK_WEIGHT
    return 1.0


# Strip C0 (U+0000-U+001F except \t \n) and C1 (U+0080-U+009F) bytes from
# user-supplied body content. These never carry useful payload from agent
# writes but can be replayed back through `memory show` to inject ANSI
# escape sequences into the user's terminal (see issue #108).
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")

# Truncation marker appended when content exceeds MAX_ENTRY_CHARS. Read
# semantics are unchanged (clipped at MAX_ENTRY_CHARS), but the marker
# makes the silent clip surfaceable to anyone inspecting the file directly
# (see issue #109).
_TRUNCATION_MARKER = "\n\n[truncated at {limit} chars]\n"


def _sanitize_body(content: str) -> str:
    """Strip C0/C1 control bytes from `content` while keeping ``\n`` and ``\t``."""
    return _CONTROL_CHAR_RE.sub("", content)


def _truncate_body(content: str, limit: int = None) -> str:
    """Clip `content` to `limit` chars total, leaving room for the marker.

    The marker is reserved inside the limit (not appended on top) so the on-
    disk body length stays <= MAX_ENTRY_CHARS and the marker survives the
    matching read-side clip in `_scan_entries`. Callers that inspect
    `entry.body` see the marker; the original tail content past the head
    window is dropped.
    """
    if limit is None:
        limit = MAX_ENTRY_CHARS
    if len(content) <= limit:
        return content
    marker = _TRUNCATION_MARKER.format(limit=limit)
    head_len = max(0, limit - len(marker))
    return content[:head_len] + marker


def _coerce_str(value: object, default: str = "") -> str:
    """Coerce frontmatter values to a display string.

    ``parse_frontmatter`` returns lists for ``[a, b]`` syntax and bools for
    ``true``/``false``. ``MemoryEntry`` annotates these fields as ``str`` so
    callers (CLI rendering, recall scoring) can rely on string operations.
    """
    if value is None:
        return default
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return str(value)


class PersistentMemory:
    """File-based persistent memory that survives across sessions.

    Design:
        - Frozen snapshot injected into system prompt at session start (preserves prompt cache).
        - Disk writes via add()/remove() update files immediately but do NOT change the snapshot.
        - Next session picks up the updated state.

    Attributes:
        snapshot: Frozen memory index text for system prompt injection.
    """

    def __init__(self, memory_dir: Optional[Path] = None) -> None:
        """Initialize PersistentMemory.

        Args:
            memory_dir: Override memory directory (default: ~/.vibe-trading/memory/).
        """
        self._dir = memory_dir or MEMORY_BASE
        self._dir.mkdir(parents=True, exist_ok=True)
        self._index_path = self._dir / "MEMORY.md"
        self._snapshot: str = ""
        # Whether the most recent add() landed inside the line-capped index
        # (F7①). True until an add is actually dropped by the cap.
        self.last_add_indexed: bool = True
        self._load_snapshot()

    def _load_snapshot(self) -> None:
        """Load index as frozen snapshot. Called once at init."""
        if self._index_path.exists():
            try:
                text = self._index_path.read_text(encoding="utf-8")
                lines = text.split("\n")[:MAX_INDEX_LINES]
                self._snapshot = "\n".join(lines)
            except OSError:
                self._snapshot = ""

    @property
    def snapshot(self) -> str:
        """Frozen memory index for system prompt injection."""
        return self._snapshot

    def _scan_entries(self) -> List[MemoryEntry]:
        """Scan all .md files (except MEMORY.md) and parse frontmatter.

        Returns:
            List of parsed memory entries.
        """
        entries: List[MemoryEntry] = []
        for path in sorted(self._dir.glob("*.md")):
            if path.name == "MEMORY.md":
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            meta, body = _parse_frontmatter(text)
            entries.append(MemoryEntry(
                path=path,
                title=_coerce_str(meta.get("name"), default=path.stem),
                description=_coerce_str(meta.get("description")),
                memory_type=_coerce_str(meta.get("type"), default="project"),
                body=body[:MAX_ENTRY_CHARS],
                modified_at=path.stat().st_mtime,
                # F7③: optional fields — legacy entries simply have "".
                created=_coerce_str(meta.get("created")),
                source=_coerce_str(meta.get("source")),
            ))
        return entries

    def list_entries(self) -> List[MemoryEntry]:
        """Return all persisted memory entries, filename-sorted."""
        return self._scan_entries()

    def find(self, name: str) -> Optional[MemoryEntry]:
        """Resolve a memory by exact title, then by on-disk filename stem.

        Stem fallback accepts both the full ``{type}_{slug}`` form and the
        bare ``slug`` suffix so users can paste either form from the index.
        """
        needle = name.strip()
        if not needle:
            return None
        entries = self._scan_entries()
        for entry in entries:
            if entry.title == needle:
                return entry
        for entry in entries:
            stem = entry.path.stem
            if stem == needle or stem.endswith(f"_{needle}"):
                return entry
        return None

    def remove_entry(self, entry: MemoryEntry) -> bool:
        """Delete a resolved entry without re-scanning to find it again."""
        try:
            entry.path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Failed to remove memory entry %s: %s", entry.path, exc)
            return False
        self._rebuild_index()
        return True

    def find_relevant(self, query: str, max_results: int = MAX_RESULTS) -> List[MemoryEntry]:
        """Keyword search across all memory entries.

        Scoring (F7④): weighted token overlap — metadata hits × 2.0 + body
        hits × 1.0, where non-Latin 2-grams and ASCII words weigh 1.0 and lone
        non-Latin chars weigh ``SINGLE_CJK_WEIGHT`` (they match half the corpus
        on their own). The result is then multiplied by a small recency bonus
        ``1 + RECENCY_WEIGHT × freshness`` (mtime-based, linear decay over
        ``RECENCY_HORIZON_DAYS``) so newer memories win ties.

        Args:
            query: Search query.
            max_results: Maximum entries to return.

        Returns:
            Top-scoring memory entries.
        """
        import time as _time

        query_tokens = _tokenize(query)
        if not query_tokens:
            return []

        now = _time.time()
        scored: list[tuple[float, MemoryEntry]] = []
        for entry in self._scan_entries():
            meta_tokens = _tokenize(f"{entry.title} {entry.description}")
            body_tokens = _tokenize(entry.body)
            meta_hits = sum(_token_weight(t) for t in query_tokens & meta_tokens)
            body_hits = sum(_token_weight(t) for t in query_tokens & body_tokens)
            score = meta_hits * METADATA_WEIGHT + body_hits
            if score <= 0:
                continue
            age_days = max(0.0, (now - entry.modified_at) / 86400.0)
            freshness = max(0.0, 1.0 - age_days / RECENCY_HORIZON_DAYS)
            score *= 1.0 + RECENCY_WEIGHT * freshness
            scored.append((score, entry))

        scored.sort(key=lambda x: (-x[0], -x[1].modified_at))
        return [entry for _, entry in scored[:max_results]]

    def add(self, name: str, content: str, memory_type: str = "project",
            description: str = "", source: str = "") -> Path:
        """Save a new memory entry and update the index.

        Args:
            name: Memory name (used as filename slug). Empty or whitespace-
                only names are rejected.
            content: Memory body text. C0/C1 control bytes (other than
                ``\n`` and ``\t``) are stripped; the body is truncated to
                ``MAX_ENTRY_CHARS`` with a visible marker.
            memory_type: One of user/feedback/project/reference.
            description: One-line description for retrieval scoring.
            source: Optional provenance note (F7③) — which conversation /
                tool / task produced this memory. Stored in frontmatter;
                readers treat a missing field as "".

        Returns:
            Path to the created memory file. After the call,
            :attr:`last_add_indexed` reports whether the entry made it into
            the (line-capped) index.

        Raises:
            ValueError: If `name` is empty or whitespace-only.
        """
        # Reject empty / whitespace-only names so they cannot all collapse
        # to the same `{type}_.md` filename and silently overwrite each
        # other (issue #110).
        stripped_name = name.strip()
        if not stripped_name:
            raise ValueError("memory name must not be empty or whitespace-only")

        # Preserve non-Latin script characters in the slug — collapsing
        # them all to ``_`` caused two same-length non-Latin names to share a
        # filename and silently overwrite each other (PR #95 + #104).
        slug = _SLUG_DISALLOWED_RE.sub("_", stripped_name.lower())[:60]

        # If the slug normalized to all underscores (emoji-only, punctuation-
        # only, etc.) the on-disk filename would still collide between any
        # two such names. Append a short deterministic hash so distinct
        # inputs produce distinct files (issue #110).
        if slug.strip("_") == "":
            digest = hashlib.sha256(stripped_name.encode("utf-8")).hexdigest()[:6]
            slug = f"{slug}_{digest}" if slug else digest

        filename = f"{memory_type}_{slug}.md"
        path = self._dir / filename

        safe_name = stripped_name.replace("\n", " ").replace("\r", " ")
        safe_desc = (description or stripped_name).replace("\n", " ").replace("\r", " ")
        safe_source = (source or "").replace("\n", " ").replace("\r", " ").strip()

        # Strip control bytes (#108) before truncation (#109) so the marker
        # is computed against the user-visible content length.
        clean_content = _truncate_body(_sanitize_body(content))

        # V2 (P2-7): same-name-same-type used to overwrite silently, so one
        # bad update destroyed the previous body for good — the Mem0 write-time
        # UPDATE failure mode. The old body is now folded into the tail of the
        # new file under the same merge marker ``consolidate()`` uses, which
        # costs nothing and keeps the history visible.
        previous_body = self._read_body(path)
        if previous_body:
            clean_content = _truncate_body(
                clean_content
                + f"\n\n---\n[superseded body, kept from the previous version of "
                f"'{safe_name}']\n{previous_body}"
            )

        # F7③: created timestamp always; source only when supplied. Readers
        # (_scan_entries) treat both as optional so legacy entries are fine.
        created_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
        source_line = f"source: {safe_source}\n" if safe_source else ""
        frontmatter = (
            f"---\nname: {safe_name}\n"
            f"description: {safe_desc}\n"
            f"type: {memory_type}\n"
            f"created: {created_iso}\n"
            f"{source_line}---\n\n"
            f"{clean_content}"
        )
        # V2 (memory P1-3): a full tenant volume raised OSError straight out of
        # here and killed the whole attempt. Writing memory is a nice-to-have;
        # the caller turns this into a structured tool error instead.
        try:
            path.write_text(frontmatter, encoding="utf-8")
        except OSError as exc:
            logger.warning("memory write failed for %s: %s", path.name, exc)
            raise MemoryWriteError(
                f"memory store unavailable: {exc}"
            ) from exc
        try:
            self.last_add_indexed = self._update_index(
                stripped_name, filename, description or stripped_name
            )
        except OSError as exc:  # noqa: BLE001 - entry exists; index is derived
            logger.warning("memory index update failed for %s: %s", path.name, exc)
            self.last_add_indexed = False
        return path

    def _read_body(self, path: Path) -> str:
        """Return the body (frontmatter stripped) of an existing entry file.

        Args:
            path: Entry file path.

        Returns:
            The body text, or ``""`` when the file is absent or unreadable.
        """
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return ""
        _, body = _parse_frontmatter(text)
        return (body or "").strip()

    def remove(self, name: str) -> bool:
        """Remove a memory entry by name.

        Args:
            name: Memory name to remove.

        Returns:
            True if found and removed.
        """
        for entry in self._scan_entries():
            if entry.title == name:
                entry.path.unlink(missing_ok=True)
                self._rebuild_index()
                return True
        return False

    def _update_index(self, title: str, filename: str, description: str) -> bool:
        """Append or update an entry in MEMORY.md.

        Returns:
            ``True`` when the entry's line landed inside the kept
            ``MAX_INDEX_LINES`` window, ``False`` when the cap truncated it
            away (F7① — the caller should warn: the entry file exists but it
            will not appear in the session-start snapshot).
        """
        new_line = f"- [{title}]({filename}) — {description}"

        included = True
        if self._index_path.exists():
            lines = self._index_path.read_text(encoding="utf-8").split("\n")
            updated = False
            for i, line in enumerate(lines):
                if f"[{title}]" in line:
                    lines[i] = new_line
                    updated = True
                    included = i < MAX_INDEX_LINES
                    break
            if not updated:
                lines.append(new_line)
                included = len(lines) <= MAX_INDEX_LINES
            text = "\n".join(lines[:MAX_INDEX_LINES])
        else:
            text = new_line

        self._index_path.write_text(text, encoding="utf-8")
        return included

    @property
    def index_full(self) -> bool:
        """Whether the index has reached its line cap (new adds get dropped)."""
        return self.index_line_count() >= MAX_INDEX_LINES

    def index_line_count(self) -> int:
        """Return the number of lines currently in the index.

        Returns:
            Line count, or 0 when the index does not exist or cannot be read.
        """
        if not self._index_path.exists():
            return 0
        try:
            return len(self._index_path.read_text(encoding="utf-8").split("\n"))
        except OSError:
            return 0

    def maybe_auto_consolidate(self) -> dict | None:
        """Run one consolidation pass when the index is close to its cap (V2).

        Called at run end, not per-write: consolidation rewrites entry files,
        and doing that mid-run would churn the session-start snapshot the
        system prompt froze. Failures are swallowed — tidying is best effort.

        Returns:
            The ``consolidate()`` stats dict when a pass ran, else None.
        """
        if self.index_line_count() < AUTO_CONSOLIDATE_INDEX_LINES:
            return None
        try:
            return self.consolidate()
        except OSError as exc:  # noqa: BLE001 - tidying must never fail a run
            logger.warning("auto consolidation failed: %s", exc)
            return None

    def consolidate(self) -> dict:
        """Deduplicate entries sharing a title and rebuild the index (F7⑤).

        Same-title entries can accumulate under different ``memory_type``
        prefixes (``project_x.md`` + ``user_x.md``) because the filename
        embeds the type. For every duplicated title the newest file (mtime)
        is kept, older bodies are appended into it under a merge marker
        (subject to the entry size cap), and the older files are deleted.

        Returns:
            Stats dict: ``duplicates_merged`` (files removed), ``entries``
            (count after), ``index_lines``, ``index_full``.
        """
        entries = self._scan_entries()
        by_title: dict[str, list[MemoryEntry]] = {}
        for entry in entries:
            by_title.setdefault(entry.title, []).append(entry)

        removed = 0
        for title, group in by_title.items():
            if len(group) <= 1:
                continue
            group.sort(key=lambda e: -e.modified_at)
            keeper, older = group[0], group[1:]
            merged_body = keeper.body
            for dup in older:
                note = (
                    f"\n\n---\n[merged from duplicate '{dup.memory_type}' entry "
                    f"{dup.path.name} during consolidation]\n{dup.body}"
                )
                merged_body = _truncate_body(merged_body + note)
            try:
                text = keeper.path.read_text(encoding="utf-8")
                header_end = text.find("\n---\n", 4)
                if header_end != -1 and text.startswith("---"):
                    header = text[: header_end + len("\n---\n")]
                    keeper.path.write_text(header + "\n" + merged_body, encoding="utf-8")
                else:
                    # No frontmatter to preserve — write the merged body as-is.
                    keeper.path.write_text(merged_body, encoding="utf-8")
            except OSError as exc:
                # Merge failed → keep the duplicates (deleting them now would
                # lose their bodies).
                logger.warning("Consolidation merge failed for %s: %s", title, exc)
                continue
            for dup in older:
                try:
                    dup.path.unlink(missing_ok=True)
                    removed += 1
                except OSError as exc:
                    logger.warning("Failed to remove duplicate %s: %s", dup.path, exc)

        self._rebuild_index()
        remaining = self._scan_entries()
        try:
            index_lines = len(
                self._index_path.read_text(encoding="utf-8").split("\n")
            ) if self._index_path.exists() else 0
        except OSError:
            index_lines = 0
        return {
            "duplicates_merged": removed,
            "entries": len(remaining),
            "index_lines": index_lines,
            "index_full": index_lines >= MAX_INDEX_LINES,
        }

    def _rebuild_index(self) -> None:
        """Rebuild MEMORY.md from all existing entry files."""
        entries = self._scan_entries()
        lines = [f"- [{e.title}]({e.path.name}) — {e.description}" for e in entries]
        self._index_path.write_text("\n".join(lines[:MAX_INDEX_LINES]), encoding="utf-8")
