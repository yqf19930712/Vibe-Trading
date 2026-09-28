"""File / URL tool edges: edit counts, append writes, path confinement of the
analysis tools, and the read_url host guard's numeric / resolved forms.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.tools import web_reader_tool
from src.tools.alpha_bench_tool import AlphaBenchTool
from src.tools.edit_file_tool import EditFileTool
from src.tools.factor_analysis_tool import FactorAnalysisTool
from src.tools.write_file_tool import WriteFileTool


@pytest.fixture()
def run_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("VIBE_TRADING_ALLOWED_RUN_ROOTS", str(tmp_path / "runs"))
    run = tmp_path / "runs" / "r1"
    run.mkdir(parents=True)
    return run


# ── edit_file ────────────────────────────────────────────────────────────────


def test_edit_reports_occurrences_and_what_was_left(run_dir: Path) -> None:
    (run_dir / "s.py").write_text("w = 20\nx = 1\nw = 20\n", encoding="utf-8")
    out = json.loads(EditFileTool().execute(path="s.py", old_text="w = 20", new_text="w = 30",
                                            run_dir=str(run_dir)))
    assert out["status"] == "ok"
    assert (out["occurrences"], out["replaced"], out["remaining"]) == (2, 1, 1)
    assert "1 other" in out["message"]
    assert (run_dir / "s.py").read_text(encoding="utf-8") == "w = 30\nx = 1\nw = 20\n"


def test_edit_single_occurrence_has_nothing_remaining(run_dir: Path) -> None:
    (run_dir / "s.py").write_text("a = 1\n", encoding="utf-8")
    out = json.loads(EditFileTool().execute(path="s.py", old_text="a = 1", new_text="a = 2",
                                            run_dir=str(run_dir)))
    assert out["remaining"] == 0 and out["message"] == "Edit applied successfully"


def test_edit_refuses_empty_old_text(run_dir: Path) -> None:
    (run_dir / "s.py").write_text("body\n", encoding="utf-8")
    out = json.loads(EditFileTool().execute(path="s.py", old_text="", new_text="PREPENDED",
                                            run_dir=str(run_dir)))
    assert out["status"] == "error"
    assert (run_dir / "s.py").read_text(encoding="utf-8") == "body\n"


# ── write_file append ────────────────────────────────────────────────────────


def test_write_file_appends_in_parts(run_dir: Path) -> None:
    tool = WriteFileTool()
    first = json.loads(tool.execute(path="report.md", content="# 报告\n第一段\n", run_dir=str(run_dir)))
    second = json.loads(tool.execute(path="report.md", content="第二段\n", mode="append",
                                     run_dir=str(run_dir)))
    assert first["mode"] == "overwrite" and second["mode"] == "append"
    assert (run_dir / "report.md").read_text(encoding="utf-8") == "# 报告\n第一段\n第二段\n"


def test_write_file_append_creates_the_file(run_dir: Path) -> None:
    out = json.loads(WriteFileTool().execute(path="new/notes.md", content="x", mode="append",
                                             run_dir=str(run_dir)))
    assert out["status"] == "ok"
    assert (run_dir / "new" / "notes.md").read_text(encoding="utf-8") == "x"


def test_write_file_rejects_unknown_mode(run_dir: Path) -> None:
    out = json.loads(WriteFileTool().execute(path="a.md", content="x", mode="prepend",
                                             run_dir=str(run_dir)))
    assert out["status"] == "error"


# ── analysis tools stay inside the run roots ─────────────────────────────────


def _factor_inputs(run_dir: Path) -> None:
    rng = np.random.default_rng(0)
    idx = pd.date_range("2025-01-01", periods=30, freq="D")
    cols = [f"S{i}" for i in range(8)]
    pd.DataFrame(rng.normal(size=(30, 8)), index=idx, columns=cols).to_csv(run_dir / "factor.csv")
    pd.DataFrame(rng.normal(size=(30, 8)) / 100, index=idx, columns=cols).to_csv(run_dir / "ret.csv")


def test_factor_analysis_works_on_run_relative_paths(run_dir: Path) -> None:
    _factor_inputs(run_dir)
    out = json.loads(FactorAnalysisTool().execute(
        factor_csv="factor.csv", return_csv="ret.csv", output_dir="factor_out", run_dir=str(run_dir)))
    assert out["status"] == "ok"
    assert (run_dir / "factor_out" / "ic_series.csv").exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("output_dir", "../../escaped"),
        ("output_dir", "/etc/vibe-out"),
        ("factor_csv", "/etc/passwd"),
        ("return_csv", "../../../outside.csv"),
    ],
)
def test_factor_analysis_rejects_paths_outside_the_roots(run_dir: Path, field: str, value: str) -> None:
    _factor_inputs(run_dir)
    kwargs = {"factor_csv": "factor.csv", "return_csv": "ret.csv", "output_dir": "out",
              "run_dir": str(run_dir), field: value}
    out = json.loads(FactorAnalysisTool().execute(**kwargs))
    assert out["status"] == "error"
    assert not (run_dir.parent.parent / "escaped").exists()


def test_alpha_bench_rejects_an_output_dir_outside_the_run_roots(run_dir: Path, monkeypatch) -> None:
    import src.tools.alpha_bench_tool as bench_mod

    monkeypatch.setattr(bench_mod, "run_alpha_bench", lambda **kw: {"status": "ok", "kw": kw})
    bad = json.loads(AlphaBenchTool().execute(universe="csi300", period="2024-2025",
                                              output_dir="/usr/lib/evil", run_dir=str(run_dir)))
    assert bad["status"] == "error"
    good = json.loads(AlphaBenchTool().execute(universe="csi300", period="2024-2025",
                                               output_dir="reports", run_dir=str(run_dir)))
    assert good["kw"]["output_dir"] == str((run_dir / "reports").resolve())


# ── read_url host guard ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "http://2852039166/latest/meta-data",      # 169.254.169.254, decimal
        "http://0xa9fea9fe/latest/meta-data",      # hex
        "http://0251.0376.0251.0376/",             # octal dotted
        "http://169.254.43518/",                   # a.b.c form
        "http://0x7f000001:8899/health",           # 127.0.0.1
        "http://0177.1/",                          # 127.0.0.1, a.b form
        "http://017700000001/",                    # 127.0.0.1, octal
        "http://[::ffff:127.0.0.1]/",              # IPv4-mapped IPv6
        "http://100.100.100.200/",                 # Aliyun metadata (CGNAT)
    ],
)
def test_read_url_rejects_encoded_internal_addresses(monkeypatch, url: str) -> None:
    monkeypatch.setattr(web_reader_tool.requests, "get",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("fetched")))
    out = json.loads(web_reader_tool.read_url(url))
    assert out["status"] == "error"


def test_numeric_forms_that_are_public_stay_allowed() -> None:
    assert web_reader_tool._url_allowed("http://134744072/")[0] is True  # 8.8.8.8


def test_hostname_resolving_inside_is_refused_for_tenants(monkeypatch) -> None:
    monkeypatch.setenv("VIBE_TRADING_TENANT_SAFE", "1")
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda host, *a, **k: [(socket.AF_INET, 0, 0, "", ("10.0.3.7", 0))])
    assert web_reader_tool._url_allowed("http://cube-router.internal/")[0] is False


def test_hostname_resolution_is_skipped_outside_the_tenant_profile(monkeypatch) -> None:
    monkeypatch.delenv("VIBE_TRADING_TENANT_SAFE", raising=False)
    monkeypatch.delenv("VIBE_MULTITENANT", raising=False)
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("resolved")))
    assert web_reader_tool._url_allowed("http://example.com/")[0] is True


def test_unresolvable_host_is_not_a_rejection(monkeypatch) -> None:
    monkeypatch.setenv("VIBE_TRADING_TENANT_SAFE", "1")

    def _fail(*a, **k):
        raise socket.gaierror("no such host")

    monkeypatch.setattr(socket, "getaddrinfo", _fail)
    assert web_reader_tool._url_allowed("https://news.example.org/a")[0] is True
