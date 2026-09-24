"""Edit file tool: find-and-replace in workspace files."""

from __future__ import annotations

import json
from typing import Any

from src.agent.tools import BaseTool
from src.tools.path_utils import safe_path as _safe_path
from src.tools.path_utils import safe_run_dir as _safe_run_dir
from src.tools.redaction import redact_internal_paths


class EditFileTool(BaseTool):
    """Find and replace the first occurrence of a string in a workspace file."""

    name = "edit_file"
    description = (
        "Find and replace the first occurrence of old_text with new_text in a "
        "file under run_dir (path is relative to run_dir). old_text must match "
        "exactly, whitespace included, and must not be empty (to add text, "
        "write_file with mode='append' or edit around an existing line); if it "
        "occurs more than once only the FIRST occurrence changes, so include "
        "enough surrounding lines to make it unique. Returns {status:'ok', path, "
        "occurrences, remaining} — remaining > 0 means other copies were left "
        "untouched; status:'error' when the file is missing, old_text is empty "
        "or not found, or the path resolves outside run_dir."
    )
    is_readonly = False
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path relative to run_dir"},
            "old_text": {"type": "string", "description": "Text to find"},
            "new_text": {"type": "string", "description": "Text to replace with"},
        },
        "required": ["path", "old_text", "new_text"],
    }
    repeatable = True

    def execute(self, **kwargs: Any) -> str:
        """Perform find-and-replace.

        Args:
            **kwargs: Must include path, old_text, new_text. Optional run_dir.

        Returns:
            JSON string with the operation result or an error.
        """
        file_path = kwargs["path"]
        old_text = kwargs["old_text"]
        new_text = kwargs["new_text"]
        run_dir = kwargs.get("run_dir")

        if not run_dir:
            return json.dumps(
                {
                    "status": "error",
                    "error": "run_dir is required for edit_file",
                },
                ensure_ascii=False,
            )
        if not isinstance(old_text, str) or old_text == "":
            # "" is "found" at offset 0 of every file: the edit silently
            # prepended new_text to the file and reported success.
            return json.dumps(
                {
                    "status": "error",
                    "error": (
                        "old_text must not be empty. To add content use "
                        "write_file(mode='append'), or replace an existing "
                        "line together with the new text."
                    ),
                },
                ensure_ascii=False,
            )

        try:
            run_root = _safe_run_dir(str(run_dir))
            resolved = _safe_path(file_path, run_root)
        except ValueError as exc:
            return json.dumps(
                {
                    "status": "error",
                    "error": str(exc),
                },
                ensure_ascii=False,
            )

        if not resolved.exists():
            return json.dumps(
                {
                    "status": "error",
                    "error": f"File not found: {file_path}",
                },
                ensure_ascii=False,
            )

        try:
            content = resolved.read_text(encoding="utf-8")
            if old_text not in content:
                return json.dumps(
                    {
                        "status": "error",
                        "error": f"old_text not found in {file_path}",
                    },
                    ensure_ascii=False,
                )
            occurrences = content.count(old_text)
            new_content = content.replace(old_text, new_text, 1)
            resolved.write_text(new_content, encoding="utf-8")
            remaining = occurrences - 1
            message = "Edit applied successfully"
            if remaining:
                message = (
                    f"Replaced the first of {occurrences} occurrences; {remaining} "
                    "other(s) left unchanged. Call again with more surrounding "
                    "context for each one you also meant to change."
                )
            return json.dumps(
                {
                    "status": "ok",
                    "path": str(resolved),
                    "occurrences": occurrences,
                    "replaced": 1,
                    "remaining": remaining,
                    "message": message,
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
