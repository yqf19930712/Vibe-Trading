"""Regression coverage for SwarmTool natural-language preset routing."""

from __future__ import annotations

import json

import src.tools.swarm_tool as swarm_tool


def test_explicit_preset_name_wins_over_keyword_scoring() -> None:
    prompt = (
        "[Swarm Team Mode] Use the investment_committee preset to evaluate "
        "whether to go long or short on NVDA given current market conditions"
    )

    assert swarm_tool._match_preset(prompt) == "investment_committee"


def test_plain_given_does_not_trigger_iv_derivatives_match() -> None:
    prompt = "Evaluate whether to go long or short on NVDA given current market conditions"

    assert swarm_tool._match_preset(prompt) != "derivatives_strategy_desk"


def test_explicit_preset_name_accepts_spaces() -> None:
    prompt = "Use the investment committee preset for NVDA"

    assert swarm_tool._match_preset(prompt) == "investment_committee"


def test_explicit_preset_parameter_is_normalized() -> None:
    preset, error = swarm_tool._resolve_preset(
        "Continue and finish the report.",
        explicit_preset="Investment Committee",
    )

    assert error is None
    assert preset == "investment_committee"


def test_ambiguous_continuation_does_not_fallback_to_equity_team() -> None:
    preset, error = swarm_tool._resolve_preset(
        "Continue and finish report. Continue from 'Trim 25% of position if price r'."
    )

    assert preset is None
    assert error is not None
    assert "equity_research_team" in error


def test_swarm_tool_rejects_ambiguous_continuation_before_starting_run() -> None:
    payload = json.loads(
        swarm_tool.SwarmTool().execute(
            prompt="Continue and finish report. Continue from 'Trim 25% of position if price r'."
        )
    )

    assert payload["status"] == "error"
    assert "Ambiguous continuation" in payload["error"]


# --- V1: roster from YAML, tie-break, variable extraction -------------------


def test_previously_unreachable_presets_can_be_named() -> None:
    """The four keyword-less presets were rejected as "Unknown preset" (V1).

    ``_PRESET_NAMES`` was derived from ``_PRESET_KEYWORDS``, so a preset that
    shipped as YAML without a keyword row could not be selected at all.
    """
    for name in (
        "crypto_trading_desk",
        "earnings_research_desk",
        "global_equities_desk",
        "macro_rates_fx_desk",
    ):
        preset, error = swarm_tool._resolve_preset("anything", explicit_preset=name)
        assert error is None, f"{name} still rejected: {error}"
        assert preset == name


def test_macro_rates_fx_desk_prose_no_longer_routes_to_macro_forum() -> None:
    """The war-room button's prose used to score onto the bare word "macro"."""
    prompt = "使用多智能体团队分析，preset 用 macro_rates_fx_desk。评估全球利率与外汇。"

    assert swarm_tool._match_preset(prompt) == "macro_rates_fx_desk"


def test_tie_break_prefers_the_exact_phrase_hit() -> None:
    """On an equal weighted score, the phrase match beats the ambient token.

    "crypto" (crypto_research_lab) and "funding rate" (crypto_trading_desk)
    carry the same 0.95 boost; the phrase is the one that identifies the desk.
    """
    assert swarm_tool._match_preset("funding rate and basis trade for crypto") == (
        "crypto_trading_desk"
    )
    # Without the desk-specific phrase, the research lab still wins.
    assert swarm_tool._match_preset("on-chain crypto research") == "crypto_research_lab"


def test_preset_route_score_distinguishes_named_from_fallback() -> None:
    named = swarm_tool._preset_route_score(
        "use the risk_committee preset", "risk_committee"
    )
    fallback = swarm_tool._preset_route_score(
        "just tell me something", "equity_research_team"
    )

    assert named == swarm_tool._EXPLICIT_NAME_SCORE
    assert fallback == 0.0


def test_commodity_variables_follow_the_prompt_not_a_hard_coded_gold() -> None:
    """commodity_research_team analysed GOLD whatever was asked (V1)."""
    assert _vars("commodity_research_team", "分析铜的供需，未来一年")["commodity"] == "copper"
    assert _vars("commodity_research_team", "crude oil outlook over 6 months") == {
        "commodity": "crude oil",
        "horizon": "6 months",
        "goal": "crude oil outlook over 6 months",
    }
    # Unidentifiable subject: defer to {goal} instead of inventing one.
    unknown = _vars("commodity_research_team", "帮我看看这个品种")
    assert "gold" not in unknown["commodity"]
    assert unknown["goal"] == "帮我看看这个品种"


def test_other_hard_coded_variables_now_read_the_prompt() -> None:
    assert _vars("crypto_research_lab", "分析 ETH 和 SOL")["target"] == "ETH, SOL"
    assert _vars("crypto_research_lab", "bitcoin outlook")["target"] == "BTC"
    assert _vars("derivatives_strategy_desk", "看空标普，用期权对冲")["view"] == "bearish"
    assert _vars("factor_research_committee", "动量因子研究")["factor_type"] == "momentum"
    assert _vars("event_driven_task_force", "并购套利")["event_type"] == "M&A"
    assert _vars("fund_selection_panel", "帮我选债基")["fund_type"] == "bond"
    # Defaults are preserved when the prompt says nothing.
    assert _vars("derivatives_strategy_desk", "期权分析")["view"] == "neutral"
    assert _vars("crypto_research_lab", "加密市场怎么看")["target"] == "BTC, ETH, SOL"


def _vars(preset: str, prompt: str) -> dict[str, str]:
    return swarm_tool._build_variables(preset, prompt)
