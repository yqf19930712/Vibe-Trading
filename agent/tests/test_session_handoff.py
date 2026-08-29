"""V2: the Layer 3 handoff summary must survive across attempts.

``AgentLoop._previous_summary`` is reset on every ``run()``. Everything an
attempt compressed away was therefore invisible to the next one, and a laicai
thread bound to the same ``vibe_session_id`` only ever replayed a sliding
window of raw text. These tests pin the sidecar that closes that gap and the
two-layer history it feeds.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.agent.context_policy import HANDOFF_PREFIX
from src.session import handoff


@pytest.fixture(autouse=True)
def _tenant_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the per-tenant data root at a temp dir (VIBE_DATA_DIR contract)."""
    monkeypatch.setenv("VIBE_DATA_DIR", str(tmp_path))
    return tmp_path


class TestRoundTrip:
    def test_save_then_load(self) -> None:
        assert handoff.save("s1", "## Goal\nbuy low") is True
        assert handoff.load("s1") == "## Goal\nbuy low"

    def test_missing_session_returns_empty_not_error(self) -> None:
        assert handoff.load("never-existed") == ""

    def test_empty_session_id_is_a_no_op(self) -> None:
        assert handoff.save("", "text") is False
        assert handoff.load("") == ""

    def test_blank_summary_is_not_persisted(self) -> None:
        assert handoff.save("s1", "   \n ") is False
        assert handoff.load("s1") == ""

    def test_later_save_overwrites(self) -> None:
        handoff.save("s1", "first")
        handoff.save("s1", "second")
        assert handoff.load("s1") == "second"

    def test_clear_removes_the_sidecar(self) -> None:
        handoff.save("s1", "text")
        handoff.clear("s1")
        assert handoff.load("s1") == ""

    def test_lives_inside_the_session_directory(self, _tenant_root: Path) -> None:
        """So /forget and any retention sweep delete it with the session."""
        handoff.save("s1", "text")
        assert (_tenant_root / "sessions" / "s1" / "handoff.json").exists()


class TestRobustness:
    def test_corrupt_json_returns_empty(self, _tenant_root: Path) -> None:
        path = _tenant_root / "sessions" / "s1" / "handoff.json"
        path.parent.mkdir(parents=True)
        path.write_text("{not json", encoding="utf-8")
        assert handoff.load("s1") == ""

    def test_wrong_shape_returns_empty(self, _tenant_root: Path) -> None:
        path = _tenant_root / "sessions" / "s1" / "handoff.json"
        path.parent.mkdir(parents=True)
        path.write_text('["a list"]', encoding="utf-8")
        assert handoff.load("s1") == ""

    def test_stale_summary_is_not_carried_over(self, _tenant_root: Path) -> None:
        """A summary from a long-abandoned topic is worse than none."""
        path = _tenant_root / "sessions" / "s1" / "handoff.json"
        path.parent.mkdir(parents=True)
        old = datetime.now(timezone.utc) - timedelta(
            days=handoff.HANDOFF_TTL_DAYS + 1
        )
        path.write_text(
            json.dumps({"summary": "ancient", "updated_at": old.isoformat()}),
            encoding="utf-8",
        )
        assert handoff.load("s1") == ""

    def test_fresh_summary_within_ttl_is_carried_over(self, _tenant_root: Path) -> None:
        path = _tenant_root / "sessions" / "s1" / "handoff.json"
        path.parent.mkdir(parents=True)
        recent = datetime.now(timezone.utc) - timedelta(days=1)
        path.write_text(
            json.dumps({"summary": "recent", "updated_at": recent.isoformat()}),
            encoding="utf-8",
        )
        assert handoff.load("s1") == "recent"

    def test_save_failure_is_swallowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Persistence is an enhancement — a full disk must not fail the run."""

        def _boom(*_args, **_kwargs):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(Path, "mkdir", _boom)
        assert handoff.save("s1", "text") is False

    def test_oversized_summary_is_clipped_with_a_marker(self) -> None:
        runaway = "word " * 40_000
        handoff.save("s1", runaway)
        loaded = handoff.load("s1")
        assert loaded.endswith("[handoff summary clipped at the size cap]")
        assert len(loaded) < len(runaway)


class TestHistoryInjection:
    """The session replay must offer summary + budgeted raw turns."""

    @staticmethod
    def _convert(messages: list, session_id: str = "s1") -> list:
        from src.session.service import SessionService

        return SessionService._convert_messages_to_history(
            messages, session_id=session_id
        )

    def test_summary_is_prepended_with_the_shared_marker(self) -> None:
        handoff.save("s1", "## Key Decisions\nuse a 20-day window")
        msgs = [
            {"role": "user", "content": "earlier question"},
            {"role": "assistant", "content": "earlier answer"},
            {"role": "user", "content": "current turn (dropped)"},
        ]

        out = self._convert(msgs)

        # HANDOFF_PREFIX so the in-run Layer 2 recognises and skips this block.
        assert out[0]["content"].startswith(HANDOFF_PREFIX)
        assert "use a 20-day window" in out[0]["content"]
        assert "NOT" in out[0]["content"]  # non-instruction declaration
        assert out[-1]["content"] == "earlier answer"

    def test_no_summary_means_no_extra_message(self) -> None:
        msgs = [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "a"},
            {"role": "user", "content": "current"},
        ]
        out = self._convert(msgs, session_id="no-summary-here")
        assert [m["content"] for m in out] == ["q", "a"]

    def test_dropped_turns_get_an_explicit_placeholder(self) -> None:
        """P2-9: silently vanishing turns read as "nothing happened"."""
        msgs = [{"role": "user", "content": "老" * 20_000}]
        msgs += [{"role": "assistant", "content": "recent answer"}]
        msgs += [{"role": "user", "content": "current turn"}]

        out = self._convert(msgs, session_id="no-summary-here")

        assert any("were omitted from this replay" in m["content"] for m in out)
        assert out[-1]["content"] == "recent answer"

    def test_budget_is_token_based_not_character_based(self) -> None:
        """CJK-heavy history must not sneak 2.4x past the intended budget."""
        from src.core.token_estimate import estimate_text_tokens

        msgs = [
            {"role": "user", "content": "中文内容测试" * 400} for _ in range(20)
        ]
        msgs.append({"role": "user", "content": "current"})

        out = self._convert(msgs, session_id="no-summary-here")
        kept = [m for m in out if "omitted from this replay" not in m["content"]]
        total = sum(estimate_text_tokens(m["content"]) for m in kept)

        assert total <= 6_000
        assert kept, "the budget must still admit at least the newest turn"
