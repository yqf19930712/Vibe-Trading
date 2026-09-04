"""A8 (P1, review 2026-09-04): MEMORY.md is written atomically and a corrupt
index is quarantined instead of failing every attempt of the tenant.
"""

from __future__ import annotations

from pathlib import Path

import src.memory.persistent as persistent_mod
from src.core.atomic_write import atomic_write_text
from src.memory.persistent import PersistentMemory


class TestCorruptIndexIsQuarantined:
    def test_half_multibyte_char_does_not_raise_and_file_is_renamed(self, tmp_path: Path) -> None:
        idx = tmp_path / "MEMORY.md"
        # "- [x](user_x.md) — " + the first two bytes of a 3-byte CJK char.
        idx.write_bytes("- [x](user_x.md) — ".encode("utf-8") + b"\xe4\xb8")

        pm = PersistentMemory(memory_dir=tmp_path)

        assert pm.snapshot == ""
        assert not idx.exists()
        quarantined = list(tmp_path.glob("MEMORY.md.corrupt-*"))
        assert len(quarantined) == 1
        assert quarantined[0].read_bytes().endswith(b"\xe4\xb8")

    def test_entries_still_load_and_index_can_be_rebuilt(self, tmp_path: Path) -> None:
        (tmp_path / "user_keep.md").write_text(
            "---\nname: keep\ndescription: still here\ntype: user\n---\n\nbody\n",
            encoding="utf-8",
        )
        (tmp_path / "MEMORY.md").write_bytes(b"\xff\xfe garbage")

        pm = PersistentMemory(memory_dir=tmp_path)

        assert [e.title for e in pm.list_entries()] == ["keep"]
        pm._rebuild_index()
        assert "user_keep.md" in (tmp_path / "MEMORY.md").read_text(encoding="utf-8")

    def test_corrupt_entry_file_is_skipped_not_fatal(self, tmp_path: Path) -> None:
        (tmp_path / "user_good.md").write_text(
            "---\nname: good\ndescription: d\ntype: user\n---\n\nbody\n", encoding="utf-8"
        )
        (tmp_path / "user_bad.md").write_bytes(b"---\nname: bad\n---\n\xe4\xb8")

        pm = PersistentMemory(memory_dir=tmp_path)

        assert [e.title for e in pm.list_entries()] == ["good"]


class TestWritesAreAtomic:
    def test_index_and_entries_go_through_atomic_write(self, tmp_path: Path, monkeypatch) -> None:
        written: list[Path] = []

        def _spy(path: Path, text: str, **kw) -> None:
            written.append(path)
            atomic_write_text(path, text, **kw)

        monkeypatch.setattr(persistent_mod, "atomic_write_text", _spy)
        pm = PersistentMemory(memory_dir=tmp_path)

        pm.add("atomic-check", "content", memory_type="user", description="d")

        names = {p.name for p in written}
        assert "MEMORY.md" in names
        assert any(n.endswith("atomic-check.md") for n in names)
        # No temp residue after a clean write.
        assert not list(tmp_path.glob("*.tmp"))
        assert not list(tmp_path.glob(".*.tmp"))

    def test_failed_write_leaves_old_index_intact(self, tmp_path: Path, monkeypatch) -> None:
        idx = tmp_path / "MEMORY.md"
        idx.write_text("- [old](user_old.md) — keep\n", encoding="utf-8")

        def _boom(path: Path, text: str, **kw) -> None:
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(persistent_mod, "atomic_write_text", _boom)
        pm = PersistentMemory(memory_dir=tmp_path)
        try:
            pm.add("new-one", "content", memory_type="user", description="d")
        except Exception:  # noqa: BLE001 - the write error itself is not under test
            pass

        assert idx.read_text(encoding="utf-8") == "- [old](user_old.md) — keep\n"

    def test_atomic_write_text_replaces_whole_file(self, tmp_path: Path) -> None:
        target = tmp_path / "f.txt"
        target.write_text("old", encoding="utf-8")
        atomic_write_text(target, "new content")
        assert target.read_text(encoding="utf-8") == "new content"
        assert list(tmp_path.iterdir()) == [target]
