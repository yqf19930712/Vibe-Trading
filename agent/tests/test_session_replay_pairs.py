"""Continued-session replay keeps question/answer pairs together.

The follow-up "那第一笔怎么操作" points into the newest answer. Filling the
replay one message at a time dropped exactly that answer (the first thing
over budget) while its question and an older topic stayed, so the reference
resolved against the older topic. These tests pin the pair unit, the cut
(rather than dropped) newest turn and the omission notes' placement.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.core.token_estimate import estimate_text_tokens
from src.session import replay

BUDGET = 6_000


@pytest.fixture(autouse=True)
def _tenant_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("VIBE_DATA_DIR", str(tmp_path))
    return tmp_path


def _convert(messages: list) -> list:
    from src.session.service import SessionService

    return SessionService._convert_messages_to_history(messages, session_id="no-summary")


def _tokens(out: list) -> int:
    return sum(estimate_text_tokens(m["content"]) + replay.PER_MESSAGE_TOKENS for m in out)


def _is_note(msg: dict) -> bool:
    return "were omitted from this replay" in msg["content"]


def test_newest_long_answer_is_cut_not_dropped() -> None:
    """The review's repro: Q1 茅台 / A1 4k 字 / Q2 宁德时代 / A2 1.1 万字 / follow-up."""
    a1 = "## 贵州茅台估值分析\n" + "茅台估值" * 1000
    a2 = "## 宁德时代分析\n第一笔：回调到 180 元分批建仓。" + "宁德时代" * 2750 + "结论：维持增持。"
    msgs = [
        {"role": "user", "content": "帮我分析一下贵州茅台的估值"},
        {"role": "assistant", "content": a1},
        {"role": "user", "content": "那宁德时代呢？"},
        {"role": "assistant", "content": a2},
        {"role": "user", "content": "那第一笔怎么操作"},
    ]

    out = _convert(msgs)

    assert out[-2] == {"role": "user", "content": "那宁德时代呢？"}
    answer = out[-1]
    assert answer["role"] == "assistant"
    assert answer["content"].startswith("## 宁德时代分析\n第一笔：回调到 180 元分批建仓。")
    assert answer["content"].endswith("结论：维持增持。")
    assert "characters omitted from the middle of this answer" in answer["content"]
    # The older topic did not fit next to it and is noted where it was.
    assert _is_note(out[0]) and len(out) == 3
    assert all("茅台估值" not in m["content"] for m in out)
    assert _tokens(out[1:]) <= BUDGET + 4


def test_an_omitted_middle_turn_leaves_its_note_in_place() -> None:
    msgs = [
        {"role": "user", "content": "Q1"},
        {"role": "assistant", "content": "A1"},
        {"role": "user", "content": "Q2 long"},
        {"role": "assistant", "content": "长" * 12_000},
        {"role": "user", "content": "Q3"},
        {"role": "assistant", "content": "A3"},
        {"role": "user", "content": "current"},
    ]

    out = _convert(msgs)

    contents = [m["content"] for m in out]
    assert contents[:2] == ["Q1", "A1"]
    assert _is_note(out[2]) and out[2]["content"].startswith("[1 earlier turns")
    assert contents[3:] == ["Q3", "A3"]
    # The question never survives without its answer.
    assert "Q2 long" not in contents


def test_turns_are_never_split() -> None:
    msgs = []
    for i in range(6):
        msgs += [
            {"role": "user", "content": f"问题{i}"},
            {"role": "assistant", "content": f"回答{i} " + "内容" * (900 if i % 2 else 50)},
        ]
    msgs.append({"role": "user", "content": "current"})

    out = [m for m in _convert(msgs) if not _is_note(m)]

    for q, a in zip(out[::2], out[1::2]):
        assert q["role"] == "user" and a["role"] == "assistant"
        assert q["content"].replace("问题", "") == a["content"].split(" ")[0].replace("回答", "")
    assert _tokens(out) <= BUDGET


def test_long_newest_question_keeps_at_most_its_share() -> None:
    msgs = [
        {"role": "user", "content": "持仓明细" * 3000},
        {"role": "assistant", "content": "分析" * 6000},
        {"role": "user", "content": "current"},
    ]

    q, a = _convert(msgs)

    assert estimate_text_tokens(q["content"]) <= int(BUDGET * replay.LATEST_QUESTION_SHARE)
    assert "middle of this question" in q["content"]
    assert "middle of this answer" in a["content"]
    assert _tokens([q, a]) <= BUDGET + 4


def test_short_newest_turn_is_replayed_verbatim() -> None:
    msgs = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "a"},
        {"role": "user", "content": "current"},
    ]
    assert [m["content"] for m in _convert(msgs)] == ["q", "a"]


def test_failed_receipt_is_a_one_line_status() -> None:
    msgs = [
        {"role": "user", "content": "跑一下回测"},
        {"role": "assistant", "content": "Execution failed: deadline_exhausted:\nthe attempt's time budget ran out " + "x" * 800},
        {"role": "user", "content": "current"},
    ]

    out = _convert(msgs)

    status = out[-1]["content"]
    assert status.startswith("[This request did not complete: deadline_exhausted: the attempt's")
    assert "\n" not in status and len(status) < 400
    assert "Execution failed" not in status


def test_clip_middle_counts_what_it_removed() -> None:
    text = "HEAD" + "m" * 40_000 + "TAIL"
    clipped = replay.clip_middle(text, 1000, "answer")

    assert clipped.startswith("HEAD") and clipped.endswith("TAIL")
    assert estimate_text_tokens(clipped) <= 1000
    head, rest = clipped.split("\n\n[… ", 1)
    omitted, _ = rest.split(" characters", 1)
    tail = rest.split("…]\n\n", 1)[1]
    assert len(head) + int(omitted) + len(tail) == len(text)


def test_assistant_first_session_forms_its_own_turn() -> None:
    turns = replay.group_turns([
        {"role": "assistant", "content": "welcome"},
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "a"},
        {"role": "assistant", "content": "a2"},
    ])
    assert [[m["content"] for m in t] for t in turns] == [["welcome"], ["q", "a", "a2"]]
