"""Shared protection rules for the three context-compression layers.

Layer 1 (``_microcompact``), Layer 2 (``_context_collapse``) and Layer 3
(``_auto_compact``) must agree on "what may I touch": a private per-layer rule
set lets L2 fold the middle out of the grounding results L1 refuses to prune,
or fold the handoff summary L3 has just paid an LLM call to produce. Every
"can this message be compressed, and how hard" decision therefore lives here.

Design note — graded rules, not boolean exemptions. Each message class gets
its own fold parameters; only structural messages (already-folded
placeholders, the status bar, handoff summaries, protected tool results) and
the **current request** get ``skip``. The current request — the user message
``ContextBuilder.build_messages`` appends for this attempt, carrying the
original question, the goal context, the recalled memories and whatever
context the caller attached (a 15k-character War Room plan prompt, a
holdings dump) — is the one message the model needs on every turn and the
one Layer 2 cannot rebuild; folding its middle silently removes the task
constraints. Layer 3 remains its backstop: when the whole trajectory exceeds
the token threshold, the structured summary covers it like any other head
message. Messages are classified by role and the ``vibe_class`` mark, not by
position: in a continued thread slot 1 is the handoff summary or a replayed
history turn, so position cannot identify the request.

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

# —— Message class mark ————————————————————————————————————————————————
# Extra key on an OpenAI-format message dict naming its class. LangChain
# folds unknown keys into ``additional_kwargs``, which neither the OpenAI nor
# the Anthropic serializer emits for user messages, so the mark never reaches
# a provider and never changes request bytes.
MESSAGE_CLASS_KEY = "vibe_class"
# The user message that carries this attempt's request (see module docstring).
REQUEST_CLASS = "request"

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


# The default for ordinary messages.
DEFAULT = CollapseRule(False, 2400, 900, 500)
# The earliest replayed user turn of a continued thread (the original request
# the whole thread grew from, replayed by the session service): high
# information density, so it folds later and keeps more on both ends. The
# current attempt's request is not this class — it is ``skip`` by mark.
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
    if is_request_message(msg):
        return SKIP
    if index == first_user_index:
        return FIRST_USER
    return DEFAULT


def mark_request_message(msg: dict) -> dict:
    """Tag ``msg`` as the current attempt's request (mutates and returns it)."""
    msg[MESSAGE_CLASS_KEY] = REQUEST_CLASS
    return msg


def is_request_message(msg: Any) -> bool:
    """Return whether ``msg`` is the current attempt's request message."""
    return (
        isinstance(msg, dict)
        and msg.get("role") == "user"
        and msg.get(MESSAGE_CLASS_KEY) == REQUEST_CLASS
    )


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

    Position only — it does not identify the current request (that is the
    ``vibe_class`` mark, see :func:`is_request_message`). In a fresh session
    it is 1 and points at the request itself, which ``collapse_rule`` skips by
    mark before the position rule is consulted. In a continued thread slot 1
    holds the handoff summary (skipped by prefix) or the earliest replayed
    user turn, which is what the graded FIRST_USER rule is for. After a
    Layer 3 compaction slot 1 is the in-run summary (skipped by prefix) and
    FIRST_USER applies to nothing.

    Args:
        messages: Message list.

    Returns:
        The index, or 1 when no user message exists.
    """
    for i, msg in enumerate(messages):
        if isinstance(msg, dict) and msg.get("role") == "user":
            return i
    return 1
