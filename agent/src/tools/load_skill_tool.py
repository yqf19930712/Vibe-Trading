"""Load skill tool: load full skill documentation by name."""

from __future__ import annotations

import json
import time
from typing import Any

from src.agent.skills import SkillsLoader
from src.agent.tools import BaseTool
from src.core.fetch_stats import record_skill


class LoadSkillTool(BaseTool):
    """Load the full documentation for a named skill."""

    name = "load_skill"
    description = "Load full documentation for a named skill. Use this to learn about unfamiliar strategy patterns or workflows before starting."
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Skill name (e.g. 'strategy-generate', 'momentum')"},
        },
        "required": ["name"],
    }
    repeatable = True

    def __init__(self, skills_loader: SkillsLoader | None = None) -> None:
        """Initialize LoadSkillTool.

        Args:
            skills_loader: SkillsLoader instance; creates one automatically if omitted.
        """
        self._loader = skills_loader or SkillsLoader()

    def execute(self, **kwargs: Any) -> str:
        """Load skill documentation.

        Args:
            **kwargs: Must include name.

        Returns:
            Full skill documentation or an error message.
        """
        name = kwargs["name"]
        t0 = time.monotonic()
        content = self._loader.get_content(name)
        ok = not content.startswith("Error:")
        # Per-attempt skill accounting (surfaces in attempt_stats.skills).
        record_skill(name, ms=int((time.monotonic() - t0) * 1000), ok=ok)
        return json.dumps({
            "status": "ok" if ok else "error",
            "content": content,
        }, ensure_ascii=False)
