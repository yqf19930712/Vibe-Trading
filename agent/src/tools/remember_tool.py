"""Remember tool: LLM-initiated persistent memory operations (save / recall / forget)."""

from __future__ import annotations

import json
from typing import Any

from src.agent.progress import emit_progress
from src.agent.tools import BaseTool
from src.memory.persistent import MAX_INDEX_LINES, PersistentMemory


class RememberTool(BaseTool):
    """Save, recall, or forget cross-session memories.

    Memories persist to ~/.vibe-trading/memory/ and survive across sessions.
    """

    name = "remember"
    description = (
        "Persistent cross-session memory. "
        "save: store user preferences, strategy insights, or project context. "
        "recall: search past memories by keyword. "
        "forget: remove a memory by title. "
        "DO save: durable user preferences (risk tolerance, favored assets), "
        "hard-won strategy/parameter insights, and project facts needed next "
        "session. Do NOT save: transient market prices, whole reports, or "
        "anything already in the run's artifacts. Saving with an existing "
        "title of the SAME memory_type overwrites that entry (same title with "
        "a different type creates a parallel entry — run consolidate_memory "
        "to merge those). The index tops out at "
        f"{MAX_INDEX_LINES} lines; past that, new entries are stored but "
        "no longer appear in the session-start snapshot — consolidate or "
        "forget stale entries when warned."
    )
    is_readonly = False
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["save", "recall", "forget"],
                "description": "save | recall | forget",
            },
            "title": {
                "type": "string",
                "description": "Memory title (for save/forget)",
            },
            "content": {
                "type": "string",
                "description": "Memory content (for save)",
            },
            "memory_type": {
                "type": "string",
                "enum": ["user", "feedback", "project", "reference"],
                "description": "Memory category (default: project)",
            },
            "query": {
                "type": "string",
                "description": "Search query (for recall)",
            },
            "source": {
                "type": "string",
                "description": (
                    "Optional provenance note (for save): what conversation, "
                    "tool result, or task this memory came from."
                ),
            },
        },
        "required": ["action"],
    }
    repeatable = True

    def __init__(self, memory: PersistentMemory | None = None) -> None:
        """Initialize RememberTool.

        Args:
            memory: PersistentMemory instance (auto-created if omitted).
        """
        self._memory = memory or PersistentMemory()

    def execute(self, **kwargs: Any) -> str:
        """Execute a memory action.

        Args:
            **kwargs: Must include action; other params depend on action.

        Returns:
            JSON result string.
        """
        action = kwargs.get("action", "save")

        if action == "save":
            return self._save(kwargs)
        if action == "recall":
            return self._recall(kwargs)
        if action == "forget":
            return self._forget(kwargs)
        return json.dumps({"status": "error", "error": f"Unknown action: {action}"})

    def _save(self, kwargs: dict) -> str:
        title = kwargs.get("title", "")
        content = kwargs.get("content", "")
        if not title or not content:
            return json.dumps({"status": "error", "error": "title and content required"})
        memory_type = kwargs.get("memory_type", "project")
        source = kwargs.get("source", "") or ""
        path = self._memory.add(
            title, content, memory_type, description=title, source=source
        )
        payload: dict[str, Any] = {
            "status": "ok",
            "message": f"Saved: {title}",
            "path": str(path),
        }
        # F7①: index-cap warning — the entry file exists, but it will not be
        # in the session-start snapshot. Surface it to the model AND emit an
        # observability event.
        if not getattr(self._memory, "last_add_indexed", True):
            warning = (
                f"Memory index is full ({MAX_INDEX_LINES} lines): this entry was "
                "saved but will NOT appear in the always-on session snapshot. "
                "Run consolidate_memory to merge duplicates, or forget stale "
                "entries to make room."
            )
            payload["warning"] = warning
            emit_progress(stage="memory_index_full", message=warning)
        return json.dumps(payload, ensure_ascii=False)

    def _recall(self, kwargs: dict) -> str:
        query = kwargs.get("query", "")
        if not query:
            return json.dumps({"status": "error", "error": "query required"})
        entries = self._memory.find_relevant(query)
        results = [
            {"title": e.title, "type": e.memory_type, "content": e.body[:2000]}
            for e in entries
        ]
        return json.dumps({"status": "ok", "count": len(results), "memories": results}, ensure_ascii=False)

    def _forget(self, kwargs: dict) -> str:
        title = kwargs.get("title", "")
        if not title:
            return json.dumps({"status": "error", "error": "title required"})
        removed = self._memory.remove(title)
        msg = f"Removed: {title}" if removed else f"Not found: {title}"
        return json.dumps({"status": "ok" if removed else "not_found", "message": msg})


class ConsolidateMemoryTool(BaseTool):
    """Merge duplicate persistent-memory entries and rebuild the index."""

    name = "consolidate_memory"
    description = (
        "Tidy the persistent cross-session memory store: merge duplicate "
        "entries that share a title (keeping the newest, folding older bodies "
        "in under a merge marker) and rebuild the index. Use it when a "
        "remember save warns that the memory index is full, or when recall "
        "returns near-identical duplicate entries. Returns merge/count stats."
    )
    is_readonly = False
    repeatable = True
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, memory: PersistentMemory | None = None) -> None:
        """Initialize ConsolidateMemoryTool.

        Args:
            memory: PersistentMemory instance (auto-created if omitted).
        """
        self._memory = memory or PersistentMemory()

    def execute(self, **kwargs: Any) -> str:
        """Run consolidation and report stats.

        Returns:
            JSON result string with duplicates_merged / entries /
            index_lines / index_full.
        """
        del kwargs
        stats = self._memory.consolidate()
        return json.dumps({"status": "ok", **stats}, ensure_ascii=False)
