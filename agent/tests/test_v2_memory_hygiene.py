"""V2 memory hygiene: write failures, overwrite history, and auto-consolidation.

Three gaps the memory review found: a full tenant volume raised ``OSError``
straight out of ``PersistentMemory.add`` and killed the whole attempt; a
same-name-same-type save destroyed the previous body with no trace (the Mem0
write-time UPDATE failure mode); and the only tidy-up was a manual tool call
the model had to think to make.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import src.memory.persistent as persistent_mod
from src.memory.persistent import (
    AUTO_CONSOLIDATE_INDEX_LINES,
    MAX_INDEX_LINES,
    MemoryWriteError,
    PersistentMemory,
)
from src.tools.remember_tool import RememberTool


@pytest.fixture
def memory(tmp_path: Path) -> PersistentMemory:
    return PersistentMemory(memory_dir=tmp_path / "memory")


class TestWriteFailureIsStructured:
    def test_add_raises_a_typed_error_not_a_bare_oserror(
        self, memory: PersistentMemory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(*_args, **_kwargs):
            raise OSError(28, "No space left on device")

        # Entry/index writes go through the atomic writer (tmp + os.replace,
        # review 2026-09-04), so that seam is the one to break.
        monkeypatch.setattr(persistent_mod, "atomic_write_text", _boom)

        with pytest.raises(MemoryWriteError) as exc:
            memory.add("prefs", "risk averse", "user")
        assert "memory store unavailable" in str(exc.value)

    def test_remember_tool_returns_an_error_instead_of_exploding(
        self, memory: PersistentMemory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Losing one memory write must not lose the answer."""
        tool = RememberTool(memory=memory)

        def _boom(*_args, **_kwargs):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(persistent_mod, "atomic_write_text", _boom)

        payload = json.loads(
            tool.execute(action="save", title="prefs", content="risk averse")
        )

        assert payload["status"] == "error"
        assert payload["error_code"] == "memory_write_failed"
        assert "do not retry the same save" in payload["message"]

    def test_an_index_write_failure_does_not_lose_the_entry(
        self, memory: PersistentMemory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The entry file is the asset; the index is derived from it."""
        real_write = persistent_mod.atomic_write_text

        def _fail_index_only(path: Path, *args, **kwargs):
            if path.name == "MEMORY.md":
                raise OSError(28, "No space left on device")
            return real_write(path, *args, **kwargs)

        monkeypatch.setattr(persistent_mod, "atomic_write_text", _fail_index_only)

        path = memory.add("prefs", "risk averse", "user")

        assert path.exists()
        assert memory.last_add_indexed is False


class TestOverwriteKeepsHistory:
    def test_same_name_same_type_folds_the_old_body_in(
        self, memory: PersistentMemory
    ) -> None:
        """P2-7: the old body used to be destroyed with no version trace."""
        memory.add("btc_view", "ORIGINAL: bullish above 60k", "project")
        memory.add("btc_view", "UPDATED: neutral", "project")

        entries = memory._scan_entries()
        assert len(entries) == 1
        body = entries[0].body
        assert "UPDATED: neutral" in body
        assert "ORIGINAL: bullish above 60k" in body
        assert "superseded body" in body

    def test_the_new_body_comes_first(self, memory: PersistentMemory) -> None:
        """Recall previews read from the top — the current view must lead."""
        memory.add("btc_view", "OLD", "project")
        memory.add("btc_view", "NEW", "project")
        body = memory._scan_entries()[0].body
        assert body.index("NEW") < body.index("OLD")

    def test_a_fresh_entry_has_no_merge_marker(self, memory: PersistentMemory) -> None:
        memory.add("eth_view", "first take", "project")
        assert "superseded body" not in memory._scan_entries()[0].body

    def test_different_type_still_creates_a_parallel_entry(
        self, memory: PersistentMemory
    ) -> None:
        """Unchanged pre-V2 semantics: consolidate_memory merges those."""
        memory.add("shared_name", "as project", "project")
        memory.add("shared_name", "as user", "user")
        assert len(memory._scan_entries()) == 2

    def test_repeated_overwrites_stay_within_the_entry_size_cap(
        self, memory: PersistentMemory
    ) -> None:
        """Folding must not grow without bound."""
        from src.memory.persistent import MAX_ENTRY_CHARS

        for i in range(12):
            memory.add("churn", f"revision {i} " + "z" * 2000, "project")
        assert len(memory._scan_entries()[0].body) <= MAX_ENTRY_CHARS + 200


class TestAutoConsolidate:
    def test_below_the_threshold_nothing_runs(self, memory: PersistentMemory) -> None:
        memory.add("one", "content", "project")
        assert memory.maybe_auto_consolidate() is None

    def test_at_the_threshold_a_pass_runs(self, memory: PersistentMemory) -> None:
        """P2-11: tidying must not wait for the model to notice the warning."""
        assert AUTO_CONSOLIDATE_INDEX_LINES < MAX_INDEX_LINES, (
            "the pass has to happen BEFORE new entries stop being indexed"
        )
        for i in range(AUTO_CONSOLIDATE_INDEX_LINES + 5):
            memory.add(f"entry_{i}", f"content {i}", "project")

        stats = memory.maybe_auto_consolidate()

        assert stats is not None
        assert "duplicates_merged" in stats
        assert "index_lines" in stats

    def test_duplicates_across_types_are_merged_by_the_auto_pass(
        self, memory: PersistentMemory
    ) -> None:
        for i in range(AUTO_CONSOLIDATE_INDEX_LINES):
            memory.add(f"entry_{i}", f"content {i}", "project")
        memory.add("dupe", "as project", "project")
        memory.add("dupe", "as user", "user")

        stats = memory.maybe_auto_consolidate()

        assert stats is not None
        assert stats["duplicates_merged"] >= 1

    def test_failure_is_swallowed(
        self, memory: PersistentMemory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for i in range(AUTO_CONSOLIDATE_INDEX_LINES + 2):
            memory.add(f"entry_{i}", f"content {i}", "project")
        monkeypatch.setattr(
            PersistentMemory,
            "consolidate",
            lambda _self: (_ for _ in ()).throw(OSError("disk gone")),
        )
        assert memory.maybe_auto_consolidate() is None

    def test_index_line_count_is_zero_when_absent(self, tmp_path: Path) -> None:
        empty = PersistentMemory(memory_dir=tmp_path / "nothing-here")
        assert empty.index_line_count() == 0
        assert empty.index_full is False


class TestRelatedLinksGuidance:
    def test_remember_description_asks_for_related_links(self) -> None:
        """P2-8: copy F8's skills requirement onto memory entries."""
        description = RememberTool.description
        assert "Related" in description
        assert "at least 2" in description

    def test_remember_description_documents_the_merge_on_overwrite(self) -> None:
        assert "folded into the tail" in RememberTool.description
