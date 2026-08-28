"""Tests for read_file offset/limit paging (batch F, F4)."""

from __future__ import annotations

import json
from pathlib import Path

from src.tools.read_file_tool import ReadFileTool


def _setup(tmp_path: Path, monkeypatch, lines: int = 10) -> Path:
    monkeypatch.setenv("VIBE_TRADING_ALLOWED_RUN_ROOTS", str(tmp_path))
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "data.txt").write_text(
        "".join(f"line{i}\n" for i in range(1, lines + 1)), encoding="utf-8"
    )
    return run_dir


def _read(run_dir: Path, **kwargs) -> dict:
    return json.loads(ReadFileTool().execute(path="data.txt", run_dir=str(run_dir), **kwargs))


def test_offset_starts_at_given_line(tmp_path, monkeypatch) -> None:
    run_dir = _setup(tmp_path, monkeypatch)
    body = _read(run_dir, offset=8)
    assert body["status"] == "ok"
    assert body["content"].startswith("line8\n")
    assert "line7" not in body["content"]


def test_offset_with_limit_pages_and_hints_continuation(tmp_path, monkeypatch) -> None:
    run_dir = _setup(tmp_path, monkeypatch)
    body = _read(run_dir, offset=3, limit=2)
    assert body["content"].startswith("line3\nline4\n")
    assert "line5" not in body["content"].split("...")[0]
    # Continuation hint: 6 lines remain, next offset is 5.
    assert "6 more lines" in body["content"]
    assert "offset=5" in body["content"]


def test_limit_only_keeps_legacy_behavior_with_hint(tmp_path, monkeypatch) -> None:
    run_dir = _setup(tmp_path, monkeypatch)
    body = _read(run_dir, limit=4)
    assert body["content"].startswith("line1\n")
    assert "line4\n" in body["content"]
    assert "6 more lines" in body["content"]
    assert "offset=5" in body["content"]


def test_no_offset_no_limit_reads_all_without_hint(tmp_path, monkeypatch) -> None:
    run_dir = _setup(tmp_path, monkeypatch)
    body = _read(run_dir)
    assert body["content"].count("line") == 10
    assert "more lines" not in body["content"]


def test_offset_past_eof_returns_empty_with_no_hint(tmp_path, monkeypatch) -> None:
    run_dir = _setup(tmp_path, monkeypatch)
    body = _read(run_dir, offset=99)
    assert body["status"] == "ok"
    assert "line" not in body["content"]
    assert "more lines" not in body["content"]
