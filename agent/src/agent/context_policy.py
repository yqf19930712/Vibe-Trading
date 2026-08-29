"""Shared protection rules for the three context-compression layers (V2).

Layer 1 (``_microcompact``), Layer 2 (``_context_collapse``) and Layer 3
(``_auto_compact``) each used to carry their own idea of "what may I touch":
L1 had a private protected-tool set, L2 had a single ``startswith("[cleared")``
check, L3 had none at all. The layers therefore contradicted each other — L2
folded the middle out of the grounding results L1 explicitly refuses to prune,
and folded the handoff summary L3 had just paid an LLM call to produce. Every
"can this message be compressed, and how hard" decision now lives here.

Design note — graded rules, not boolean exemptions. A blanket exemption for the
first user message (original request + goal context + ``<recalled-memories>``)
would make the single largest message in a long session permanently
incompressible. Instead each message class gets its own fold parameters, and
only genuinely structural messages (already-folded placeholders, the status
bar, handoff summaries, protected tool results) get ``skip``.

Byte stability (book §2.3.4): the marker prefixes below are matched against
text already written into the trajectory. Changing one silently re-enables
folding of every message written under the old prefix, so treat them as
frozen.
"""

from __future__ import annotations

from typing import Any, NamedTuple

# —— Frozen marker prefixes (see module docstring) ——————————————
# Layer 1's pruned-result placeholder (src.agent.loop._CLEARED_PLACEHOLDER).
CLEARED_PREFIX = "[cleared"
# Layer 3's handoff summary header (src.agent.loop._auto_compact) and the
# session-level replay header (src.session.handoff) — deliberately the same
# string so a summary carried across attempts is recognised inside the run.
HANDOFF_PREFIX = "[Conversation compressed"
# The per-iteration ephemeral status bar (src.agent.loop._STATUS_PREFIX).
STATUS_PREFIX = "<agent_status>"
# The explicit tool-result truncation envelope (src.agent.tool_result_store).
TRUNCATED_TAG = "<tool-result-truncated"

# Tool results that Layer 1 never prunes: they carry the run's grounding data
# (every cited number must trace back to one) or a deliverable whose re-fetch
# costs minutes to tens of minutes. Moved here from ``loop.py`` so Layer 2
# honours the same list; ``loop.py`` keeps the old name as an alias.
PROTECTED_TOOLS = frozenset({
    "backtest",
    "factor_analysis",
    "options_pricing",
    "get_market_data",
    "get_realtime_quotes",
    "run_swarm",
})


class CollapseRule(NamedTuple):
    """How aggressively Layer 2 may fold one message.

    Attributes:
        skip: True = Layer 2 must not touch this message at all.
        min_chars: Fold only when the content is longer than this.
        head: Characters kept from the start.
        tail: Characters kept from the end.
    """

    skip: bool
    min_chars: int
    head: int
    tail: int


# Today's COLLAPSE_* constants, unchanged — the default for ordinary messages.
DEFAULT = CollapseRule(False, 2400, 900, 500)
# The first user message carries the original request, the goal context and the
# recalled-memories block: information density is high and re-deriving it is
# impossible, so it folds later and keeps more on both ends.
FIRST_USER = CollapseRule(False, 9600, 3000, 1200)
# Escape valve for a protected tool result that is pathologically large (only
# reachable on paths the tool_result_store offload does not cover). Without it
# a single malformed result could overflow the window while all three layers
# politely refuse to touch it.
PROTECTED_HARD_CAP = CollapseRule(False, 24000, 6000, 3000)
SKIP = CollapseRule(True, 0, 0, 0)


def collapse_rule(msg: Any, *, index: int, first_user_index: int) -> CollapseRule:
    """Return the Layer 2 folding rule for one message.

    Args:
        msg: OpenAI-format message dict.
        index: Its position in the message list.
        first_user_index: Position of the first ``role == "user"`` message.

    Returns:
        The :class:`CollapseRule` Layer 2 must apply.
    """
    if not isinstance(msg, dict):
        return SKIP
    content = msg.get("content")
    if not isinstance(content, str):
        return SKIP
    if content.startswith((CLEARED_PREFIX, HANDOFF_PREFIX, STATUS_PREFIX, TRUNCATED_TAG)):
        return SKIP
    if msg.get("role") == "tool" and msg.get("name") in PROTECTED_TOOLS:
        # Never blind-fold grounding/deliverable results. They shrink at the
        # source (tool_result_store preview) or semantically (Layer 3), except
        # for the hard-cap escape valve above.
        return PROTECTED_HARD_CAP
    if index == first_user_index:
        return FIRST_USER
    return DEFAULT


def is_prunable_by_microcompact(msg: Any) -> bool:
    """Return whether Layer 1 may replace this tool result with a placeholder.

    Args:
        msg: OpenAI-format tool-result message dict.

    Returns:
        False for results from :data:`PROTECTED_TOOLS`.
    """
    if not isinstance(msg, dict):
        return False
    return msg.get("name") not in PROTECTED_TOOLS


def first_user_index(messages: list) -> int:
    """Index of the first ``role == "user"`` message.

    Normally 1 (index 0 is the system prompt), but after a Layer 3 compaction
    slot 1 holds the handoff summary, which is itself a user message — and is
    ``skip``-ed by prefix, so the graded FIRST_USER rule lands on whichever
    user message actually leads the trajectory.

    Args:
        messages: Message list.

    Returns:
        The index, or 1 when no user message exists.
    """
    for i, msg in enumerate(messages):
        if isinstance(msg, dict) and msg.get("role") == "user":
            return i
    return 1
