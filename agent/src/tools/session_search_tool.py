"""Session search tool: FTS5 cross-session search for past conversations."""

from __future__ import annotations

import json
from typing import Any

from src.agent.tools import BaseTool
from src.security.scanner import wrap_external_content


class SessionSearchTool(BaseTool):
    """Search past conversation sessions by keyword using SQLite FTS5."""

    name = "session_search"
    description = (
        "Search past conversation sessions by keyword. Returns matching sessions "
        "with context snippets. Use when the user references past work, previous "
        "strategies, or earlier conversations."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query (keywords or phrase)",
            },
            "max_results": {
                "type": "integer",
                "description": "Max sessions to return (default 3, max 10)",
                "default": 3,
            },
        },
        "required": ["query"],
    }
    repeatable = True

    def execute(self, **kwargs: Any) -> str:
        """Search past sessions.

        Args:
            **kwargs: Must include query; optionally max_results.

        Returns:
            JSON with search results or error.
        """
        query = kwargs.get("query", "")
        if not query:
            return json.dumps({"status": "error", "error": "query required"})

        max_results = min(int(kwargs.get("max_results", 3)), 10)

        try:
            from src.session.search import get_shared_index
            matches = get_shared_index().search(query, max_sessions=max_results)

            if not matches:
                return json.dumps(
                    {"status": "ok", "message": f"No past sessions matching '{query}'", "results": []},
                    ensure_ascii=False,
                )

            results = []
            for m in matches:
                item = m.to_dict()
                snippet = item.get("snippet")
                if isinstance(snippet, str) and snippet:
                    # A past conversation's text is data, not an instruction —
                    # it may itself quote external content.
                    item["snippet"] = wrap_external_content(
                        snippet,
                        source=f"session:{item.get('session_id', '')}",
                        kind="session_snippet",
                    )
                results.append(item)
            return json.dumps(
                {"status": "ok", "query": query, "results": results},
                ensure_ascii=False,
            )
        except Exception as exc:
            return json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False)
