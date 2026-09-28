"""Write file tool: create or overwrite files in the workspace."""

from __future__ import annotations

import json
from typing import Any

from src.agent.tools import BaseTool
from src.tools.path_utils import safe_path as _safe_path
from src.tools.path_utils import safe_run_dir as _safe_run_dir
from src.tools.redaction import redact_internal_paths


class WriteFileTool(BaseTool):
    """Create or overwrite a workspace file, creating parent directories as needed."""

    name = "write_file"
    description = (
        "Write content to a file under run_dir (path is relative to run_dir), "
        "creating parent directories. mode='overwrite' (default) replaces an "
        "existing file whole — use edit_file for a targeted change; "
        "mode='append' adds content to the end (creating the file if needed). "
        "For a long report or script, write it in parts — first part with the "
        "default mode, the rest with mode='append' — so no single call has to "
        "fit the whole text into one reply. Use it for strategy code "
        "(code/signal_engine.py, config.json), notes and reports. Returns "
        "{status:'ok', path, bytes_written, mode}; status:'error' with the reason "
        "when the path resolves outside run_dir or the write fails."
    )
    is_readonly = False
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path relative to run_dir"},
            "content": {"type": "string", "description": "Content to write"},
            "mode": {
                "type": "string",
                "enum": ["overwrite", "append"],
                "description": "overwrite (default) or append to the end of the file",
            },
        },
        "required": ["path", "content"],
    }
    repeatable = True

    def execute(self, **kwargs: Any) -> str:
        """Write content to a file.

        Args:
            **kwargs: Must include path and content. Optional run_dir.

        Returns:
            JSON string with bytes_written or an error.
        """
        file_path = kwargs["path"]
        content = kwargs["content"]
        run_dir = kwargs.get("run_dir")
        mode = str(kwargs.get("mode") or "overwrite").strip().lower()
        if mode not in ("overwrite", "append"):
            return json.dumps(
                {"status": "error", "error": "mode must be 'overwrite' or 'append'"},
                ensure_ascii=False,
            )

        if not run_dir:
            return json.dumps(
                {
                    "status": "error",
                    "error": "run_dir is required for write_file",
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

        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            if mode == "append":
                with resolved.open("a", encoding="utf-8") as fh:
                    fh.write(content)
            else:
                resolved.write_text(content, encoding="utf-8")
            return json.dumps(
                {
                    "status": "ok",
                    "path": str(resolved),
                    "bytes_written": len(content.encode("utf-8")),
                    "mode": mode,
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
