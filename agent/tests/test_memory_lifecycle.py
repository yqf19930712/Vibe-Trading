"""Tests for memory lifecycle batch F items (F7): index-full warning,
created/source frontmatter, 2-gram retrieval + recency, consolidate."""

from __future__ import annotations

import json
import os
import time

from src.memory.persistent import MAX_INDEX_LINES, PersistentMemory
from src.tools.remember_tool import ConsolidateMemoryTool, RememberTool


# ---------------------------------------------------------------------------
# F7③ created/source frontmatter
# ---------------------------------------------------------------------------


class TestFrontmatterFields:
    def test_add_writes_created_and_source(self, tmp_path) -> None:
        pm = PersistentMemory(memory_dir=tmp_path)
        path = pm.add("btc-notes", "BTC halving cycle notes", "project",
                      description="btc", source="session chat 2026-08-28")
        text = path.read_text(encoding="utf-8")
        assert "created: " in text
        assert "source: session chat 2026-08-28" in text

        entry = pm.find("btc-notes")
        assert entry is not None
        assert entry.created  # ISO timestamp parsed back
        assert entry.source == "session chat 2026-08-28"

    def test_source_omitted_when_empty(self, tmp_path) -> None:
        pm = PersistentMemory(memory_dir=tmp_path)
        path = pm.add("plain", "no source", "project")
        assert "source:" not in path.read_text(encoding="utf-8")

    def test_legacy_entry_without_fields_still_reads(self, tmp_path) -> None:
        (tmp_path / "project_old.md").write_text(
            "---\nname: old\ndescription: legacy\ntype: project\n---\n\nold body\n",
            encoding="utf-8",
        )
        pm = PersistentMemory(memory_dir=tmp_path)
        entry = pm.find("old")
        assert entry is not None
        assert entry.created == ""
        assert entry.source == ""


# ---------------------------------------------------------------------------
# F7④ retrieval: CJK 2-grams, single-char down-weighting, recency
# ---------------------------------------------------------------------------


class TestRetrieval:
    def test_two_gram_match_beats_stray_single_chars(self, tmp_path) -> None:
        pm = PersistentMemory(memory_dir=tmp_path)
        pm.add("比特币策略", "比特币动量策略参数", "project", description="比特币策略")
        pm.add("消费板块", "白酒消费复苏分析", "project", description="消费板块")

        results = pm.find_relevant("比特币")
        assert results
        assert results[0].title == "比特币策略"

    def test_recency_breaks_near_ties(self, tmp_path) -> None:
        pm = PersistentMemory(memory_dir=tmp_path)
        old = pm.add("alpha-old", "momentum factor research", "project")
        new = pm.add("alpha-new", "momentum factor research", "project")
        # Age the old entry by 20 days via mtime.
        past = time.time() - 20 * 86400
        os.utime(old, (past, past))
        os.utime(new, (time.time(), time.time()))

        results = pm.find_relevant("momentum factor")
        assert [r.title for r in results[:2]] == ["alpha-new", "alpha-old"]

    def test_english_matching_unchanged(self, tmp_path) -> None:
        pm = PersistentMemory(memory_dir=tmp_path)
        pm.add("mcp_wiring_test", "wiring the mcp adapter", "project")
        assert pm.find_relevant("mcp wiring")


# ---------------------------------------------------------------------------
# F7① index-full warning
# ---------------------------------------------------------------------------


class TestIndexFullWarning:
    def _fill_index(self, pm: PersistentMemory) -> None:
        lines = [f"- [pad{i}](project_pad{i}.md) — pad" for i in range(MAX_INDEX_LINES)]
        (pm._dir / "MEMORY.md").write_text("\n".join(lines), encoding="utf-8")

    def test_add_past_cap_sets_flag(self, tmp_path) -> None:
        pm = PersistentMemory(memory_dir=tmp_path)
        self._fill_index(pm)
        pm.add("overflow-entry", "will not fit the index", "project")
        assert pm.last_add_indexed is False
        assert pm.index_full is True

    def test_remember_save_carries_warning(self, tmp_path) -> None:
        pm = PersistentMemory(memory_dir=tmp_path)
        self._fill_index(pm)
        tool = RememberTool(memory=pm)
        result = json.loads(
            tool.execute(action="save", title="overflow", content="body")
        )
        assert result["status"] == "ok"
        assert "index is full" in result["warning"]

    def test_normal_save_has_no_warning(self, tmp_path) -> None:
        tool = RememberTool(memory=PersistentMemory(memory_dir=tmp_path))
        result = json.loads(tool.execute(action="save", title="fine", content="body"))
        assert result["status"] == "ok"
        assert "warning" not in result


# ---------------------------------------------------------------------------
# F7⑤ consolidate
# ---------------------------------------------------------------------------


class TestConsolidate:
    def test_merges_same_title_across_types(self, tmp_path) -> None:
        pm = PersistentMemory(memory_dir=tmp_path)
        pm.add("dup-topic", "older body", "project")
        newer = pm.add("dup-topic", "newer body", "user")
        past = time.time() - 3600
        os.utime(tmp_path / "project_dup-topic.md", (past, past))
        os.utime(newer, (time.time(), time.time()))

        stats = pm.consolidate()

        assert stats["duplicates_merged"] == 1
        assert stats["entries"] == 1
        survivor = pm.find("dup-topic")
        assert survivor is not None
        assert survivor.path.name == "user_dup-topic.md"
        assert "newer body" in survivor.body
        assert "merged from duplicate" in survivor.body
        assert "older body" in survivor.body
        # Index rebuilt with a single line for the survivor.
        index_lines = (
            (tmp_path / "MEMORY.md").read_text(encoding="utf-8").strip().split("\n")
        )
        assert len(index_lines) == 1
        assert "user_dup-topic.md" in index_lines[0]

    def test_no_duplicates_is_a_noop(self, tmp_path) -> None:
        pm = PersistentMemory(memory_dir=tmp_path)
        pm.add("only-one", "body", "project")
        stats = pm.consolidate()
        assert stats["duplicates_merged"] == 0
        assert stats["entries"] == 1

    def test_consolidate_memory_tool_reports_stats(self, tmp_path) -> None:
        pm = PersistentMemory(memory_dir=tmp_path)
        pm.add("dup", "a", "project")
        pm.add("dup", "b", "user")
        tool = ConsolidateMemoryTool(memory=pm)
        result = json.loads(tool.execute())
        assert result["status"] == "ok"
        assert result["duplicates_merged"] == 1
        assert result["index_full"] is False
