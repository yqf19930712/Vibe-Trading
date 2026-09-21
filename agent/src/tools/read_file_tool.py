"""Read file tool: read file contents from the workspace."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.agent.tools import BaseTool
from src.security.scanner import wrap_external_content
from src.tools.path_utils import safe_path as _safe_path
from src.tools.path_utils import safe_run_dir as _safe_run_dir
from src.tools.redaction import redact_internal_paths

# Lines returned when the caller gives neither ``offset`` nor ``limit``. The
# page itself is bounded downstream by the shared 10k-character trajectory
# envelope (``agent.tool_result_store``); a default page keeps most reads
# under it instead of handing the model a preview it has to re-request.
_DEFAULT_LIMIT = 200

# An offloaded ``read_url`` / ``web_search`` / ``read_document`` / MCP result
# carries its ``<external-content>`` declaration only at the start and end of
# the file; a page from its middle would come back bare. Pages of such a file
# are re-wrapped so the data-not-instructions declaration follows the text.
_EXTERNAL_MARK = "<external-content "


class ReadFileTool(BaseTool):
    """Read file contents with optional line limit."""

    name = "read_file"
    description = (
        "Read a file from the workspace. Returns up to 200 lines by default; "
        "use offset+limit to page through large files (a page over 10k "
        "characters is shown as a head+tail preview, so page in smaller slices)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path relative to run_dir or skills/"},
            "limit": {
                "type": "integer",
                "description": "Max number of lines to return (default: 200)",
            },
            "offset": {
                "type": "integer",
                "description": "1-based line number to start reading from (default: 1). Combine with limit to page through large files.",
            },
        },
        "required": ["path"],
    }
    repeatable = True

    def execute(self, **kwargs: Any) -> str:
        """Read a file.

        Args:
            **kwargs: Must include path. Optional limit and run_dir.

        Returns:
            JSON string containing content or an error.
        """
        file_path = kwargs["path"]
        limit = kwargs.get("limit")
        offset = kwargs.get("offset")
        run_dir = kwargs.get("run_dir")

        allowed_roots = []
        if run_dir:
            try:
                allowed_roots.append(_safe_run_dir(str(run_dir)))
            except ValueError as exc:
                return json.dumps(
                    {
                        "status": "error",
                        "error": str(exc),
                    },
                    ensure_ascii=False,
                )
        # Read-only access to skills/
        skills_dir = Path(__file__).resolve().parents[1] / "skills"
        if skills_dir.exists():
            allowed_roots.append(skills_dir.resolve())

        # Strip redundant "skills/" prefix that LLMs sometimes add
        paths_to_try = [file_path]
        if file_path.startswith("skills/"):
            paths_to_try.append(file_path[len("skills/") :])

        resolved = None
        for root in allowed_roots:
            for p in paths_to_try:
                try:
                    candidate = _safe_path(p, root)
                    if candidate.exists():
                        resolved = candidate
                        break
                except ValueError:
                    continue
            if resolved:
                break

        if resolved is None:
            return json.dumps(
                {
                    "status": "error",
                    "error": f"File not found or path escapes workspace: {file_path}",
                },
                ensure_ascii=False,
            )

        try:
            text = resolved.read_text(encoding="utf-8")
            # F4: offset (1-based line start) + limit paging with an explicit
            # continuation hint, so large files are readable in slices instead
            # of always losing everything past the truncation point.
            full_text = text
            start = 0
            try:
                if offset is not None and int(offset) > 1:
                    start = int(offset) - 1
            except (TypeError, ValueError):
                start = 0
            try:
                page = int(limit) if limit is not None else _DEFAULT_LIMIT
            except (TypeError, ValueError):
                page = _DEFAULT_LIMIT
            if page <= 0:
                page = _DEFAULT_LIMIT
            lines = text.splitlines(keepends=True)
            total_lines = len(lines)
            end = start + page
            text = "".join(lines[start:end])
            remaining = total_lines - min(end, total_lines)
            if remaining > 0:
                text += (
                    f"\n... ({remaining} more lines; continue with "
                    f"offset={min(end, total_lines) + 1})"
                )
            if _EXTERNAL_MARK in full_text:
                text = wrap_external_content(
                    text, source=str(resolved), kind="offloaded_external"
                )
            return json.dumps(
                {
                    "status": "ok",
                    "path": str(resolved),
                    "content": text,
                },
                ensure_ascii=False,
            )
        except Exception as exc:
            return json.dumps(
                {
                    "status": "error",
                    "error": redact_internal_paths(str(exc)),
                },
                ensure_ascii=False,
            )
