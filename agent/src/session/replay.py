"""Raw-turn layer of the session replay: question/answer pairs under a token budget.

A continued session replays its earlier turns in front of the new request
(``SessionService._convert_messages_to_history``). Filling that budget one
message at a time broke the pairing the follow-up depends on: the newest
answer — usually a long report, and exactly what "the first trade above"
refers to — was the first thing over budget, so it vanished while its
question and older, shorter turns stayed. The model then resolved the
reference against the previous topic.

Rules here:

- A turn is one user message plus the replies that follow it; turns are kept
  or omitted whole, never split.
- The newest turn is always replayed. When it alone exceeds the budget it is
  cut in the middle (head and tail kept, the cut marked), not dropped.
- Older turns are filled newest-first; each run of omitted turns leaves a note
  at the place it was omitted from.
- A failure receipt (``Execution failed: …``) is replayed as a one-line status,
  not as an assistant answer.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from src.core.token_estimate import estimate_text_tokens

# Rough per-message envelope overhead (role, delimiters), estimator units.
PER_MESSAGE_TOKENS = 8
# Share of the budget the newest question may keep when the newest turn has
# to be cut: the answer is what a follow-up points into.
LATEST_QUESTION_SHARE = 0.3
# Share of a cut message kept from its start; the rest comes from its end
# (conclusions and next steps tend to sit at both ends of a report).
_HEAD_SHARE = 0.6
# The prose prefix ``SessionService._format_result_message`` gives a failed
# attempt's receipt.
FAILED_RECEIPT_PREFIX = "Execution failed:"
_FAILED_REASON_MAX_CHARS = 300

Turn = List[Dict[str, Any]]


def failed_receipt_status(content: str) -> str:
    """One-line replay form of a failed attempt's receipt."""
    reason = " ".join(content[len(FAILED_RECEIPT_PREFIX):].split()) or "unknown error"
    if len(reason) > _FAILED_REASON_MAX_CHARS:
        reason = reason[: _FAILED_REASON_MAX_CHARS - 1].rstrip() + "…"
    return f"[This request did not complete: {reason}]"


def group_turns(history: List[Dict[str, Any]]) -> List[Turn]:
    """Split chronological user/assistant messages into turns.

    A user message opens a turn; assistant messages join the open one. Replies
    with no question before them (a session whose first stored message is an
    assistant one) form a turn of their own.
    """
    turns: List[Turn] = []
    for msg in history:
        if msg.get("role") == "user" or not turns:
            turns.append([msg])
        else:
            turns[-1].append(msg)
    return turns


def _message_cost(msg: Dict[str, Any]) -> int:
    return estimate_text_tokens(msg.get("content", "")) + PER_MESSAGE_TOKENS


def turn_cost(turn: Turn) -> int:
    """Estimated tokens of one turn, envelopes included."""
    return sum(_message_cost(m) for m in turn)


def _prefix_within(text: str, max_tokens: int) -> int:
    """Length of the longest prefix of ``text`` within ``max_tokens``."""
    if max_tokens <= 0:
        return 0
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if estimate_text_tokens(text[:mid]) <= max_tokens:
            lo = mid
        else:
            hi = mid - 1
    return lo


def clip_middle(text: str, max_tokens: int, kind: str) -> str:
    """Cut ``text`` to ``max_tokens`` by removing its middle, with a marker.

    Args:
        text: Message text.
        max_tokens: Budget for the result, marker included.
        kind: What the text is ("question" / "answer"), named in the marker.

    Returns:
        ``text`` unchanged when it fits, else head + marker + tail.
    """
    if estimate_text_tokens(text) <= max_tokens:
        return text
    # Sized with the whole length first (an upper bound of the omitted
    # count); the few spare tokens absorb the estimator's rounding.
    room = max(0, max_tokens - estimate_text_tokens(_clip_marker(len(text), kind)) - 2)
    head = _prefix_within(text, int(room * _HEAD_SHARE))
    rest = text[head:]
    tail = _prefix_within(rest[::-1], room - estimate_text_tokens(text[:head]))
    kept_tail = rest[len(rest) - tail:] if tail else ""
    omitted = len(text) - head - len(kept_tail)
    return text[:head].rstrip() + _clip_marker(omitted, kind) + kept_tail.lstrip()


def _clip_marker(omitted: int, kind: str) -> str:
    return (
        f"\n\n[… {omitted} characters omitted from the middle of this {kind} "
        "to fit the replay budget; `session_search` with a keyword from the "
        "omitted part retrieves it …]\n\n"
    )


def clip_turn(turn: Turn, budget: int) -> Turn:
    """Fit the newest turn into ``budget`` by cutting its messages in the middle.

    The question keeps at most :data:`LATEST_QUESTION_SHARE` of the budget
    (less when it is shorter); the replies share the rest.
    """
    questions = [m for m in turn if m.get("role") == "user"]
    replies = [m for m in turn if m.get("role") != "user"]
    envelope = PER_MESSAGE_TOKENS * len(turn)
    room = max(0, budget - envelope)
    question_need = sum(estimate_text_tokens(m.get("content", "")) for m in questions)
    question_room = min(question_need, int(room * LATEST_QUESTION_SHARE)) if replies else room
    reply_room = room - question_room
    out: Turn = []
    for msg in turn:
        if msg.get("role") == "user":
            share = question_room // max(1, len(questions))
            kind = "question"
        else:
            share = reply_room // max(1, len(replies))
            kind = "answer"
        out.append({**msg, "content": clip_middle(msg.get("content", ""), share, kind)})
    return out


def fit_turns(turns: List[Turn], budget: int) -> List[Optional[Turn]]:
    """Choose which turns to replay.

    Args:
        turns: Chronological turns.
        budget: Token budget (estimator units) for all replayed turns.

    Returns:
        One slot per input turn, chronological: the turn to replay (the newest
        possibly cut) or ``None`` where the turn is omitted.
    """
    if not turns:
        return []
    slots: List[Optional[Turn]] = [None] * len(turns)
    newest = turns[-1]
    cost = turn_cost(newest)
    if cost > budget:
        slots[-1] = clip_turn(newest, budget)
        cost = turn_cost(slots[-1])
    else:
        slots[-1] = newest
    remaining = budget - cost
    for index in range(len(turns) - 2, -1, -1):
        cost = turn_cost(turns[index])
        if cost <= remaining:
            # Not a break: a shorter older turn may still fit.
            slots[index] = turns[index]
            remaining -= cost
    return slots
