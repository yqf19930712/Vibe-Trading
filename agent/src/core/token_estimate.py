"""Character-class weighted token estimation.

Shared by the main ReAct loop (``src.agent.loop``) and the swarm worker
(``src.swarm.worker``). Lives in ``src.core`` so both can import it without
touching the loop↔swarm import cycle.

The old heuristic (``len(text) // 4``) assumes English: ~4 chars/token.
Chinese/Japanese/Korean text tokenizes at roughly 0.6 tokens *per character*
on modern BPE vocabularies, so the flat ``// 4`` under-estimated CJK-heavy
contexts by 2-3x — compaction thresholds fired far too late for Chinese
research sessions. Weights used here:

  - ASCII: 1/4 token per char (English prose, code, JSON scaffolding).
  - CJK:   0.6 token per char (ideographs, kana, hangul, fullwidth forms).
  - Other: 1/3 token per char (accented Latin, Cyrillic, emoji, ...).

These are estimates for *thresholding* only — billing always prefers real
provider ``usage_metadata`` when present.
"""

from __future__ import annotations

import json
import re

# Runs of ASCII characters (matched in bulk for speed).
_ASCII_RUN_RE = re.compile(r"[\x00-\x7f]+")

# Runs of CJK-weight characters. Ranges (common blocks only):
#   2E80-9FFF   CJK radicals, Kangxi, CJK punctuation, kana, bopomofo,
#               Hangul compat jamo, CJK ext-A, CJK unified ideographs
#   AC00-D7AF   Hangul syllables
#   F900-FAFF   CJK compatibility ideographs
#   FE30-FE4F   CJK compatibility forms
#   FF00-FFEF   Fullwidth / halfwidth forms
#   20000-2EBEF CJK ext B..F (supplementary plane)
_CJK_RUN_RE = re.compile(
    r"[⺀-鿿가-힯豈-﫿︰-﹏＀-￯"
    r"\U00020000-\U0002ebef]+"
)


def estimate_text_tokens(text: str) -> int:
    """Estimate the token count of ``text`` with per-character-class weights.

    Args:
        text: Raw text (any language mix).

    Returns:
        Estimated token count (non-negative int).
    """
    if not text:
        return 0
    ascii_chars = sum(len(m) for m in _ASCII_RUN_RE.findall(text))
    cjk_chars = sum(len(m) for m in _CJK_RUN_RE.findall(text))
    other_chars = len(text) - ascii_chars - cjk_chars
    return int(ascii_chars / 4 + cjk_chars * 0.6 + other_chars / 3)


def estimate_messages_tokens(messages: list) -> int:
    """Estimate the token count of an OpenAI-format message list.

    Serializes the whole list (roles, tool_calls, content) so structural
    overhead is included, then applies the weighted character estimate.

    Args:
        messages: Message list.

    Returns:
        Estimated token count.
    """
    try:
        serialized = json.dumps(messages, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        serialized = str(messages)
    return estimate_text_tokens(serialized)
