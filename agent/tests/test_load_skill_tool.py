"""``load_skill`` hands the model the SKILL.md as Markdown, not a JSON blob.

The previous ``{"status": "ok", "content": "..."}`` wrapper turned every
skill into ONE physical line; combined with the 10k truncation envelope the
model got 9k of a 100k skill and a ``read_file`` pointer whose line paging
could not reach the rest (27 of 79 bundled skills exceed 10k).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.agent.skills import SkillsLoader
from src.agent.tool_result_store import prepare_for_context
from src.core import fetch_stats
from src.tools.load_skill_tool import LoadSkillTool


@pytest.fixture
def loader(tmp_path: Path) -> SkillsLoader:
    skills = tmp_path / "skills"
    (skills / "alpha").mkdir(parents=True)
    (skills / "alpha" / "SKILL.md").write_text(
        "---\nname: alpha\ndescription: test\n---\n# Alpha\n\n## Usage\n\ncall it\n",
        encoding="utf-8",
    )
    return SkillsLoader(skills_dir=skills, user_skills_dir=tmp_path / "user")


def test_success_returns_headed_markdown(loader: SkillsLoader) -> None:
    out = LoadSkillTool(loader).execute(name="alpha")
    assert out.startswith("# skill: alpha\n")
    assert "## Usage" in out and "call it" in out
    assert "<skill" not in out
    with pytest.raises(ValueError):
        json.loads(out)


def test_unknown_skill_returns_an_error_envelope_listing_names(loader: SkillsLoader) -> None:
    out = json.loads(LoadSkillTool(loader).execute(name="nope"))
    assert out["status"] == "error"
    assert out["error"].startswith("Error: Unknown skill 'nope'")
    assert "alpha" in out["error"]


def test_record_skill_still_fires_for_attempt_stats(loader: SkillsLoader) -> None:
    collector = fetch_stats.start_collect()
    LoadSkillTool(loader).execute(name="alpha")
    LoadSkillTool(loader).execute(name="nope")
    skills = {s["name"]: s for s in collector.snapshot_skills()}
    assert set(skills) == {"alpha", "nope"}
    assert skills["alpha"].get("errors", 0) == 0
    assert skills["nope"].get("errors", 0) == 1


def test_bundled_skills_survive_the_context_budget(tmp_path: Path) -> None:
    """Every bundled skill reaches the model whole or section-trimmed — never
    as a mid-line character cut."""
    tool = LoadSkillTool()
    for skill in tool._loader.skills:
        raw = tool.execute(name=skill.name)
        payload, _ = prepare_for_context(
            raw, base_dir=tmp_path, iteration=1, tool_name="load_skill", call_id=skill.name
        )
        assert "chars omitted from the middle" not in payload, skill.name
        assert payload.startswith("# skill: ") or 'unit="sections"' in payload, skill.name
