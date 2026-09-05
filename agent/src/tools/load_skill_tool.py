"""Load skill tool: load full skill documentation by name."""

from __future__ import annotations

import json
import time
from typing import Any

from src.agent.skills import SkillsLoader
from src.agent.tools import BaseTool
from src.core.fetch_stats import record_skill

# First line of every successful load_skill result. ``tool_result_store``
# keys its load_skill handling on the tool name, not on this header, but the
# header lets the model (and a human reading the trajectory) tell which skill
# a Markdown blob belongs to.
SKILL_HEADER_PREFIX = "# skill: "


class LoadSkillTool(BaseTool):
    """Load the full documentation for a named skill."""

    name = "load_skill"
    description = (
        "Load full documentation for a named skill. Use this to learn about "
        "unfamiliar strategy patterns or workflows before starting. Returns the "
        "skill's SKILL.md as Markdown (not JSON), headed by '# skill: <name>'. Very "
        "long skills are trimmed by section; the reply then lists the omitted "
        "section headings and the on-disk path to page through with read_file."
    )
    parameters = {
        "type": "object",
        "properties": {
            # Both examples must name skills that actually exist under
            # src/skills/. 'momentum' did not — a phantom example teaches the
            # model to invent plausible-sounding names and burn a call
            # discovering they are not there.
            "name": {
                "type": "string",
                "description": (
                    "Skill name, exactly as listed in the Skills summary of the "
                    "system prompt (e.g. 'strategy-generate', 'technical-basic')"
                ),
            },
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
            The SKILL.md Markdown headed by ``# skill: <name>``. The previous
            ``{"status", "content"}`` JSON wrapper made every skill a single
            JSON line on disk, so ``read_file`` line paging could not reach
            the part the 10k truncation envelope dropped. On failure a
            ``{"status": "error", "error": "Error: ..."}`` envelope is
            returned — both the loop's and the swarm worker's error
            classifiers key on that envelope, plain text would count as a
            successful call.
        """
        name = kwargs["name"]
        t0 = time.monotonic()
        body = self._loader.get_body(name)
        ok = body is not None
        # Per-attempt skill accounting (surfaces in attempt_stats.skills).
        record_skill(name, ms=int((time.monotonic() - t0) * 1000), ok=ok)
        if not ok:
            available = ", ".join(s.name for s in self._loader.skills)
            return json.dumps(
                {
                    "status": "error",
                    "error": f"Error: Unknown skill '{name}'. Available: {available}",
                },
                ensure_ascii=False,
            )
        return f"{SKILL_HEADER_PREFIX}{name}\n\n{body.strip()}\n"
