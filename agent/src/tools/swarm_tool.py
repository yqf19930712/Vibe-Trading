"""SwarmTool: tool for the main agent to invoke a swarm multi-agent team.

The user provides a natural-language prompt; the tool auto-selects the best preset and extracts variables.
Blocks synchronously until the run completes and returns a JSON summary.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

from src.agent.tools import BaseTool

logger = logging.getLogger(__name__)

_POLL_INTERVAL_SECONDS = 5
_MAX_WAIT_SECONDS = int(os.getenv("SWARM_TIMEOUT", "7200"))

# Nesting invariant (V1). The tool's own wait keeps back MORE than the loop's
# watchdog does, so on a bounded attempt budget the tool ALWAYS expires first
# and gets to return ``wait_budget_exhausted`` + run_id + the partial report.
# The loop's watchdog then only ever fires on a real hang (e.g. a wedged
# store.load_run). Module constants rather than literals so the scaled-down
# nesting regression can patch them — see tests/test_swarm_timeout_nesting.py.
_WAIT_RESERVE_S = 90.0
_WAIT_FLOOR_S = 60.0
# Margin added on top of _MAX_WAIT_SECONDS when declaring the loop-side
# watchdog bound. Covers the unbounded-budget case, where cap_timeout returns
# the raw value on both sides and the two would otherwise expire in a dead heat.
_LOOP_WATCHDOG_MARGIN_S = 120.0

# F3 (batch F): code-level enforcement of the "salvage, don't re-run" rule the
# system prompt only stated as prose. After a preset FAILS, an identical-preset
# re-run within this window is refused with a structured rejection carrying the
# failed run's completed-worker products — a systemic upstream issue would kill
# the re-run too, burning tens of minutes for nothing (incident 2026-08-24).
_FAILURE_COOLDOWN_SECONDS = 30 * 60
# Caps for the salvage payload embedded in the rejection (keeps it prompt-sized).
_SALVAGE_REPORT_MAX_CHARS = 4000
_SALVAGE_TASK_MAX_CHARS = 1200
_SALVAGE_MAX_TASKS = 12

# Routing score reported for a preset the caller named outright — deliberately
# above any achievable keyword score so "named" is distinguishable at a glance.
_EXPLICIT_NAME_SCORE = 99.0

# Preset matching: (preset_name, keyword_patterns, weight_boost). Patterns match user intent (EN + ZH).
_PRESET_KEYWORDS: list[tuple[str, list[str], float]] = [
    (
        "global_allocation_committee",
        [
            r"cross[- ]?market",
            r"global\s+alloc",
            "multi[- ]asset",
            "三市场",
            "全球配置",
            "资产配置",
            "港美.*A股",
            "A股.*加密",
            "加密.*A股",
            "多市场",
        ],
        1.0,
    ),
    (
        "risk_committee",
        [
            r"risk\s+audit",
            "drawdown",
            r"tail\s+risk",
            r"stress\s+test",
            r"\bVaR\b",
            "风控",
            "风险审计",
            "回撤",
            "尾部风险",
            "压力测试",
            "风险评估",
        ],
        1.0,
    ),
    (
        "quant_strategy_desk",
        [
            r"\bquant\b",
            "alpha",
            "factor",
            "backtest",
            "多因子",
            "量化策略",
            "因子",
            "选股",
            "策略.*回测",
        ],
        1.0,
    ),
    (
        "equity_research_team",
        [
            "equity research",
            "stock research",
            "研报",
            "研究报告",
            "行业分析",
            "个股分析",
            "投资分析",
            "macro.*sector",
            "投资机会",
        ],
        0.85,
    ),
    (
        "factor_research_committee",
        [
            r"factor\s+research",
            r"\bIC\b",
            "ICIR",
            "因子委员会",
            "因子研究",
        ],
        0.9,
    ),
    (
        "event_driven_task_force",
        [
            r"M&A",
            "merger",
            "insider",
            r"earnings\s+surprise",
            "事件驱动",
            "并购",
            "财报",
        ],
        0.9,
    ),
    (
        "etf_allocation_desk",
        [
            r"\bETF\b",
            r"index\s+fund",
            "指数基金",
            "ETF配置",
        ],
        0.9,
    ),
    (
        "derivatives_strategy_desk",
        [
            r"\boptions?\b",
            r"\bcall\s+options?\b",
            r"\bput\s+options?\b",
            r"\bGreeks?\b",
            r"implied\s+vol",
            r"\bIV\b",
            "期权",
            "衍生品",
        ],
        0.9,
    ),
    (
        "crypto_research_lab",
        [
            r"\bBTC\b",
            r"\bETH\b",
            r"\bSOL\b",
            "crypto",
            "bitcoin",
            "加密",
            "数字货币",
        ],
        0.95,
    ),
    (
        "credit_research_team",
        [
            r"credit\s+bond",
            "LGFV",
            r"\bYTM\b",
            "利差",
            "信用债",
            "城投",
        ],
        0.9,
    ),
    (
        "convertible_bond_team",
        [
            "convertible",
            "可转债",
            r"\bCB\b",
        ],
        0.9,
    ),
    (
        "fundamental_research_team",
        [
            "fundamental",
            r"deep\s+dive",
            "财务",
            "基本面",
        ],
        0.85,
    ),
    (
        "commodity_research_team",
        [
            "commodity",
            "crude",
            "gold",
            "copper",
            r"iron\s+ore",
            "商品",
            "原油",
            "黄金",
        ],
        0.9,
    ),
    (
        "fund_selection_panel",
        [
            r"\bFOF\b",
            r"mutual\s+fund",
            "基金筛选",
            "选基",
        ],
        0.85,
    ),
    (
        "social_alpha_team",
        [
            r"social\s+media",
            "twitter",
            "reddit",
            "社媒",
            "舆情",
        ],
        0.85,
    ),
    (
        "geopolitical_war_room",
        [
            "geopolitical",
            r"war\s+risk",
            "sanction",
            "地缘",
            "危机场景",
        ],
        0.9,
    ),
    (
        "pairs_research_lab",
        [
            r"pairs\s+trading",
            "cointegration",
            "配对",
            "统计套利",
        ],
        0.9,
    ),
    (
        "investment_committee",
        [
            r"investment\s+committee",
            "投委会",
            "投资决策",
        ],
        0.85,
    ),
    (
        "macro_strategy_forum",
        [
            r"\bFed\b",
            r"\bCPI\b",
            r"\bPMI\b",
            "macro",
            "货币政策",
            "宏观",
        ],
        0.9,
    ),
    (
        "statistical_arbitrage_desk",
        [
            r"statistical\s+arbitrage",
            r"stat\s+arb",
            "统计套利",
        ],
        0.9,
    ),
    (
        "sentiment_intelligence_team",
        [
            "sentiment",
            r"fear\s+and\s+greed",
            "情绪",
            "恐慌",
        ],
        0.85,
    ),
    (
        "technical_analysis_panel",
        [
            r"technical\s+analysis",
            r"\bRSI\b",
            r"\bMACD\b",
            "技术分析",
            "K线",
        ],
        0.85,
    ),
    (
        "sector_rotation_team",
        [
            r"sector\s+rotation",
            "板块轮动",
            "行业轮动",
        ],
        0.85,
    ),
    (
        "portfolio_review_board",
        [
            r"portfolio\s+review",
            "组合复盘",
            "业绩归因",
        ],
        0.85,
    ),
    (
        "ml_quant_lab",
        [
            r"\bML\b",
            r"machine\s+learning",
            "LSTM",
            "XGBoost",
            "机器学习",
            "深度学习",
        ],
        0.9,
    ),
    # V1: the four presets below shipped as YAML but had no keyword row, so
    # they were unreachable — ``_PRESET_NAMES`` was derived from THIS table, so
    # even an explicit ``preset_name="crypto_trading_desk"`` came back as
    # "Unknown preset", and prose naming them silently scored onto a neighbour
    # (「preset 用 macro_rates_fx_desk」 routed to macro_strategy_forum on the
    # bare word "macro"). Their boosts sit ABOVE those neighbours because each
    # is the more specific desk for its phrases.
    (
        "macro_rates_fx_desk",
        [
            r"rates?\s+and\s+fx",
            r"\bFX\b",
            r"foreign\s+exchange",
            r"yield\s+curve",
            r"cross[- ]asset\s+macro",
            "外汇",
            "汇率",
            "利率曲线",
            "国债收益率",
            "跨资产宏观",
        ],
        0.95,
    ),
    (
        "earnings_research_desk",
        [
            r"earnings\s+season",
            r"earnings\s+preview",
            r"earnings\s+research",
            r"consensus\s+revision",
            "财报季",
            "业绩预告",
            "一致预期",
            "盈利预测",
        ],
        0.95,
    ),
    (
        "global_equities_desk",
        [
            r"global\s+equit",
            r"international\s+equit",
            r"cross[- ]?market\s+stock",
            "全球选股",
            "跨市场选股",
            "全球股票",
        ],
        0.95,
    ),
    (
        "crypto_trading_desk",
        [
            r"funding\s+rate",
            r"basis\s+trade",
            r"\bperp(?:etual)?s?\b",
            r"liquidation\s+(?:map|heat)",
            r"crypto\s+(?:trading\s+)?desk",
            "资金费率",
            "永续合约",
            "清算热力",
            "加密.*执行",
        ],
        0.95,
    ),
]

# Market labels used in YAML templates (English, compatible with {market} placeholders).
_MARKET_PATTERNS: list[tuple[str, list[str]]] = [
    ("A-shares", [r"A股", r"a股", "沪深", "上证", "深证", "创业板", "科创板", "中证", r"\bCSI\b"]),
    ("crypto", ["加密", r"\bcrypto\b", r"\bBTC\b", r"\bETH\b", "币", "USDT", "数字货币"]),
    ("Hong Kong", ["港股", "恒生", r"H股", "港交所", r"\.HK\b"]),
    ("US", ["美股", "纳斯达克", "标普", "道琼斯", r"S&P", r"\.US\b"]),
]

# Risk tolerance for global_allocation_committee (English).
_RISK_PATTERNS: list[tuple[str, list[str]]] = [
    ("conservative", ["保守", r"低风险", "稳健偏保守", r"conservative"]),
    ("moderate", ["稳健", "中等风险", r"moderate", r"balanced"]),
    ("aggressive", ["激进", "高风险", "进取", r"aggressive"]),
]


_STRATEGY_TYPE_PATTERNS: list[tuple[str, list[str]]] = [
    ("low-price", [r"\blow[- ]price\b", r"\bcheap\b", r"\bdiscount\b", r"\blow premium\b"]),
    ("dual-low", [r"\bdual[- ]low\b", r"\bdouble[- ]low\b"]),
    ("high-convexity", [r"\bhigh[- ]convexity\b", r"\bconvexity\b"]),
    ("rotation", [r"\brotation\b", r"\brotate\b", r"\brebalance\b"]),
]

_TARGET_VARIABLE_PATTERNS: list[tuple[str, list[str]]] = [
    ("volatility", [r"\bvolatility\b", r"\bvol\b", r"\bvariance\b", r"\brisk\b"]),
    ("direction", [r"\bdirection(?:al)?\b", r"\bup[- ]down\b", r"\bclassification\b"]),
    ("return", [r"\breturns?\b", r"\balpha\b", r"\bpredict\b", r"\bforecast\b"]),
]

_REVIEW_PERIOD_PATTERNS: list[tuple[str, list[str]]] = [
    ("monthly", [r"\bmonthly\b", r"\bmonth(?:ly)?\b"]),
    ("quarterly", [r"\bquarter(?:ly)?\b", r"\bq[1-4]\b"]),
]

# V1: the pattern tables below close a class of silent bugs where a variable
# was hard-coded in ``_build_variables`` — commodity_research_team analysed
# GOLD whatever the user asked about, crypto_research_lab always looked at
# "BTC, ETH, SOL", derivatives always took a NEUTRAL view, factor research
# always benched VALUE, the event desk always scanned "all types" and fund
# selection always screened EQUITY funds. Each now reads the prompt first and
# falls back to the old constant only when nothing matches; the presets that
# most depended on it additionally receive ``{goal}`` (the user's own words)
# so a miss degrades to "here is what was actually asked" rather than a
# confidently wrong subject.

_COMMODITY_PATTERNS: list[tuple[str, list[str]]] = [
    ("crude oil", [r"\bcrude\b", r"\bbrent\b", r"\bWTI\b", r"\boil\b", "原油", "石油"]),
    ("natural gas", [r"natural\s+gas", r"\bLNG\b", "天然气"]),
    ("gold", [r"\bgold\b", r"\bXAU\b", "黄金", "金价"]),
    ("silver", [r"\bsilver\b", r"\bXAG\b", "白银"]),
    ("copper", [r"\bcopper\b", "铜"]),
    ("aluminium", [r"\balumin(?:i)?um\b", "铝"]),
    ("nickel", [r"\bnickel\b", "镍"]),
    ("lithium", [r"\blithium\b", "锂"]),
    ("iron ore", [r"iron\s+ore", "铁矿"]),
    ("rebar", [r"\brebar\b", "螺纹钢"]),
    ("soybeans", [r"\bsoybeans?\b", "大豆"]),
    ("corn", [r"\bcorn\b", "玉米"]),
    ("coal", [r"\bcoal\b", "煤炭", "动力煤"]),
]

# Horizon labels for commodity_research_team ({horizon} in the YAML).
_HORIZON_PATTERNS: list[tuple[str, list[str]]] = [
    ("1 month", [r"\b1\s*month\b", r"\bone\s+month\b", "一个月", "1个月"]),
    ("6 months", [r"\b6\s*months\b", r"\bsix\s+months\b", "半年", "六个月", "6个月"]),
    ("1 year", [r"\b1\s*year\b", r"\bone\s+year\b", r"\b12\s*months\b", "一年", "12个月"]),
    ("3 months", [r"\b3\s*months\b", r"\bthree\s+months\b", r"\bquarter\b", "三个月", "3个月", "季度"]),
]

# Timeframe labels for crypto_research_lab / crypto_trading_desk /
# macro_rates_fx_desk ({timeframe} in those YAMLs).
_TIMEFRAME_PATTERNS: list[tuple[str, list[str]]] = [
    ("intraday", [r"\bintraday\b", r"\bday\s+trad", "日内", "盘中"]),
    ("short-term 1-4 weeks", [r"\bshort[- ]term\b", r"\bnext\s+few\s+weeks\b", "短线", "短期", "未来几周"]),
    ("long-term 6-12 months", [r"\blong[- ]term\b", r"\b6-12\s*months\b", "长线", "长期"]),
    ("medium-term 1-3 months", [r"\bmedium[- ]term\b", r"\bmid[- ]term\b", "中线", "中期"]),
]

# Directional view for derivatives_strategy_desk ({view} in the YAML).
_VIEW_PATTERNS: list[tuple[str, list[str]]] = [
    ("bullish", [r"\bbullish\b", r"\bupside\b", r"\brally\b", r"\blong\b", "看多", "看涨", "做多"]),
    ("bearish", [r"\bbearish\b", r"\bdownside\b", r"\bcrash\b", r"\bshort\b", "看空", "看跌", "做空"]),
    ("volatile", [r"\bvolatil", r"\bwhipsaw\b", r"\bbig\s+move\b", "波动放大", "大波动"]),
    ("neutral", [r"\bneutral\b", r"\brange[- ]bound\b", "震荡", "中性"]),
]

# Factor family for factor_research_committee ({factor_type} in the YAML).
_FACTOR_TYPE_PATTERNS: list[tuple[str, list[str]]] = [
    ("momentum", [r"\bmomentum\b", r"\breversal\b", "动量", "反转"]),
    ("quality", [r"\bquality\b", r"\bROE\b", r"\bprofitability\b", "质量", "盈利质量"]),
    ("growth", [r"\bgrowth\b", "成长"]),
    ("size", [r"\bsize\b", r"\bsmall[- ]cap\b", r"\bmarket\s+cap\b", "市值", "小盘"]),
    ("volatility", [r"\bvolatility\s+factor\b", r"\blow[- ]vol\b", "低波", "波动率因子"]),
    ("liquidity", [r"\bliquidity\b", "流动性"]),
    ("value", [r"\bvalue\b", r"\bPB\b", r"\bPE\b", r"\bcheapness\b", "价值", "估值因子"]),
]

# Event family for event_driven_task_force ({event_type} in the YAML).
_EVENT_TYPE_PATTERNS: list[tuple[str, list[str]]] = [
    ("M&A", [r"M&A", r"\bmerger", r"\bacquisition", r"\btakeover\b", "并购", "重组"]),
    ("earnings", [r"\bearnings\b", r"\bresults\s+announcement\b", "财报", "业绩"]),
    ("regulatory / policy", [r"\bregulat", r"\bpolicy\b", r"\bsanction", "监管", "政策"]),
    ("insider / ownership", [r"\binsider\b", r"\bbuyback\b", r"\bstake\b", "增持", "减持", "回购"]),
    ("spin-off / restructuring", [r"\bspin[- ]off\b", r"\bdivest", "分拆", "剥离"]),
    ("index rebalance", [r"index\s+rebalanc", r"\binclusion\b", "指数调样", "纳入"]),
]

# Fund family for fund_selection_panel ({fund_type} in the YAML).
_FUND_TYPE_PATTERNS: list[tuple[str, list[str]]] = [
    ("bond", [r"\bbond\s+fund", r"\bfixed[- ]income\b", "债基", "债券型", "固收"]),
    ("money market", [r"money\s+market", "货币基金", "货基"]),
    ("index / ETF", [r"\bindex\s+fund", r"\bETF\b", r"\bpassive\b", "指数基金", "被动"]),
    ("QDII / overseas", [r"\bQDII\b", r"\boverseas\b", "海外基金", "跨境"]),
    ("hybrid", [r"\bhybrid\b", r"\bbalanced\s+fund", "混合型", "偏股混合"]),
    ("equity", [r"\bequity\s+fund", r"\bstock\s+fund", "股票型", "权益基金"]),
]

# Crypto tickers recognised for crypto_research_lab / crypto_trading_desk.
_CRYPTO_TICKER_PATTERN = re.compile(
    r"\b(BTC|XBT|ETH|SOL|BNB|XRP|ADA|DOGE|AVAX|DOT|MATIC|LINK|TON|TRX|LTC|ATOM|ARB|OP|SUI|APT)\b",
    re.IGNORECASE,
)
_CRYPTO_NAME_ALIASES: list[tuple[str, str]] = [
    (r"\bbitcoin\b|比特币", "BTC"),
    (r"\bether(?:eum)?\b|以太坊", "ETH"),
    (r"\bsolana\b", "SOL"),
]
_CRYPTO_DEFAULT_TARGET = "BTC, ETH, SOL"

_SECTOR_PATTERNS: list[tuple[str, list[str]]] = [
    ("banks", [r"\bbank(?:s|ing)?\b", r"\bfinancials?\b"]),
    ("consumer", [r"\bconsumer\b", r"\bretail\b", r"\bstaples\b", r"\bdiscretionary\b"]),
    ("semiconductors", [r"\bsemi(?:s|conductors?)?\b", r"\bchip(?:s)?\b"]),
    ("technology", [r"\btech(?:nology)?\b", r"\bsoftware\b", r"\binternet\b"]),
    ("energy", [r"\benergy\b", r"\boil\b", r"\bgas\b", r"\bpower\b"]),
    ("healthcare", [r"\bhealth ?care\b", r"\bbiotech\b", r"\bpharma\b"]),
    ("industrials", [r"\bindustrial(?:s)?\b", r"\bmanufacturing\b"]),
    ("real estate", [r"\breal estate\b", r"\bproperty\b", r"\breit(?:s)?\b"]),
    ("utilities", [r"\butilit(?:y|ies)\b"]),
    ("materials", [r"\bmaterials?\b", r"\bmetals?\b", r"\bmining\b"]),
]


def _discover_preset_names() -> frozenset[str]:
    """Return the preset roster, sourced from the bundled YAML files (V1).

    The single source of truth is ``agent/src/swarm/presets/*.yaml``, NOT the
    keyword table. Deriving the roster from ``_PRESET_KEYWORDS`` (as it used to
    be) silently made every preset without a keyword row unreachable: four
    shipped YAMLs were rejected as "Unknown preset" on an explicit
    ``preset_name``, and the system prompt's "29 swarm teams" was really 25.

    The keyword names are unioned in as a floor so a hypothetical missing
    package-data install degrades to the old behaviour instead of an empty
    roster (``load_preset`` reports a missing file clearly anyway).

    Returns:
        Frozen set of valid preset names.
    """
    names: set[str] = set()
    try:
        from src.swarm.presets import PRESETS_DIR

        names = {path.stem for path in PRESETS_DIR.glob("*.yaml")}
    except Exception:  # noqa: BLE001 - fall back to the keyword table
        logger.warning("SwarmTool: preset directory unreadable", exc_info=True)
    return frozenset(names | {preset_name for preset_name, _, _ in _PRESET_KEYWORDS})


_PRESET_NAMES = _discover_preset_names()
# Longest-first so a name that is a prefix of another can never shadow it.
_PRESET_NAMES_BY_LENGTH = sorted(_PRESET_NAMES, key=lambda n: (-len(n), n))


def _exact_preset_name(prompt: str) -> str | None:
    """Return the preset explicitly named in ``prompt``, if any.

    Iterates the YAML-derived roster rather than the keyword table so newly
    added presets are nameable the moment their YAML ships.

    Args:
        prompt: User's natural language prompt.

    Returns:
        The named preset, or None.
    """
    normalized_prompt = re.sub(r"[\s-]+", "_", prompt.strip().lower())
    for preset_name in _PRESET_NAMES_BY_LENGTH:
        if re.search(rf"(?<![a-z0-9]){re.escape(preset_name)}(?![a-z0-9])", normalized_prompt):
            return preset_name
    return None


def _is_phrase_hit(matched_text: str) -> bool:
    """Whether a keyword match counts as an exact-phrase (high-confidence) hit.

    A multi-word English phrase ("funding rate", "sector rotation") or a CJK
    term of 3+ characters ("资金费率") is specific evidence of intent; a single
    short token ("crypto", "macro", "因子") is ambient vocabulary that shows up
    in unrelated prompts too. Used only to break score ties (V1) — it never
    changes a decision that the weighted score already settles.

    Args:
        matched_text: The substring a keyword pattern actually matched.

    Returns:
        True when the match is a phrase-level hit.
    """
    text = matched_text.strip()
    if not text:
        return False
    if re.search(r"\s", text):
        return True
    return len(text) >= 3 and not text.isascii()


def _score_presets(prompt: str) -> dict[str, tuple[float, int]]:
    """Score every preset against ``prompt``.

    Args:
        prompt: User's natural language prompt.

    Returns:
        Mapping of preset name to (weighted score, exact-phrase hit count).
    """
    scores: dict[str, tuple[float, int]] = {}
    for preset_name, keywords, boost in _PRESET_KEYWORDS:
        score = 0.0
        phrase_hits = 0
        for kw in keywords:
            match = re.search(kw, prompt, re.IGNORECASE)
            if match is None:
                continue
            score += boost
            if _is_phrase_hit(match.group(0)):
                phrase_hits += 1
        scores[preset_name] = (score, phrase_hits)
    return scores


def _match_preset_scored(prompt: str) -> tuple[str, float]:
    """Match a prompt to the best preset, returning the routing score too.

    Args:
        prompt: User's natural language prompt.

    Returns:
        Tuple of (preset name, routing score). An explicitly named preset
        scores ``_EXPLICIT_NAME_SCORE``; the ``equity_research_team`` fallback
        scores 0.0.
    """
    named = _exact_preset_name(prompt)
    if named is not None:
        return named, _EXPLICIT_NAME_SCORE

    scores = _score_presets(prompt)
    order = {name: idx for idx, (name, _, _) in enumerate(_PRESET_KEYWORDS)}
    # Tie-break (V1): on equal weighted score, prefer the preset with more
    # exact-phrase hits — "crypto funding rate" ties crypto_research_lab (the
    # bare word "crypto") against crypto_trading_desk ("funding rate"), and the
    # phrase is the one that actually identifies the desk. Table order is the
    # final, deterministic fallback.
    best = min(
        scores,
        key=lambda name: (-scores[name][0], -scores[name][1], order[name]),
    )
    if scores[best][0] > 0:
        return best, scores[best][0]

    return "equity_research_team", 0.0


def _match_preset(prompt: str) -> str:
    """Match user prompt to best preset using keyword scoring.

    Args:
        prompt: User's natural language prompt.

    Returns:
        Best matching preset name.
    """
    return _match_preset_scored(prompt)[0]


def _preset_route_score(prompt: str, preset_name: str) -> float:
    """Routing confidence for ``preset_name`` given ``prompt`` (V1).

    Surfaced as ``preset_score`` next to ``auto_variables`` so a reader of the
    result (model, trace, ops tab) can tell a confident keyword match from the
    ``equity_research_team`` fallback, which today look identical.

    Args:
        prompt: The prompt the run was routed from.
        preset_name: The preset that was actually resolved.

    Returns:
        ``_EXPLICIT_NAME_SCORE`` when the preset was named outright, otherwise
        the weighted keyword score (0.0 for a pure fallback).
    """
    if _exact_preset_name(prompt) == preset_name:
        return _EXPLICIT_NAME_SCORE
    return _score_presets(prompt).get(preset_name, (0.0, 0))[0]
_CONTINUATION_PATTERNS = (
    r"^\s*continue\b",
    r"^\s*resume\b",
    r"^\s*finish\b",
    r"\bcontinue\s+(?:and\s+)?finish\b",
    r"\bcontinue\s+from\b",
    r"\bfinish\s+(?:the\s+)?report\b",
    r"\bcomplete\s+(?:the\s+)?report\b",
    r"\bpick\s+up\s+from\b",
    r"^\s*继续",
    r"^\s*接着",
)


def _normalize_preset_name(value: str) -> str | None:
    """Normalize an explicit preset name and validate it against bundled presets."""
    normalized = re.sub(r"[\s-]+", "_", value.strip().lower())
    return normalized if normalized in _PRESET_NAMES else None


def _has_preset_signal(prompt: str) -> bool:
    """Return whether prompt contains an explicit preset name or routing keyword."""
    if _exact_preset_name(prompt) is not None:
        return True
    for _, keywords, _ in _PRESET_KEYWORDS:
        for kw in keywords:
            if re.search(kw, prompt, re.IGNORECASE):
                return True
    return False


def _looks_like_continuation_prompt(prompt: str) -> bool:
    """Detect prompts that refer to prior work instead of a fresh swarm task."""
    return any(re.search(pattern, prompt, re.IGNORECASE) for pattern in _CONTINUATION_PATTERNS)


def _resolve_preset(prompt: str, explicit_preset: str | None = None) -> tuple[str | None, str | None]:
    """Resolve the preset to run, returning an error string when ambiguous."""
    if explicit_preset:
        preset = _normalize_preset_name(explicit_preset)
        if preset is None:
            available = ", ".join(sorted(_PRESET_NAMES))
            return None, f"Unknown preset_name '{explicit_preset}'. Available presets: {available}"
        return preset, None

    if _looks_like_continuation_prompt(prompt) and not _has_preset_signal(prompt):
        return (
            None,
            "Ambiguous continuation swarm prompt. Reuse the previous swarm result, "
            "or call run_swarm with preset_name and the original full request. "
            "Refusing to auto-route this continuation to equity_research_team.",
        )

    return _match_preset(prompt), None


def _extract_market(prompt: str) -> str:
    """Extract target market label from prompt.

    Args:
        prompt: User's natural language prompt.

    Returns:
        Market label for template variables, default A-shares.
    """
    for market, patterns in _MARKET_PATTERNS:
        for pat in patterns:
            if re.search(pat, prompt, re.IGNORECASE):
                return market
    return "A-shares"


def _extract_risk_tolerance(prompt: str) -> str:
    """Extract risk tolerance from prompt (English labels).

    Args:
        prompt: User's natural language prompt.

    Returns:
        conservative | moderate | aggressive.
    """
    for level, patterns in _RISK_PATTERNS:
        for pat in patterns:
            if re.search(pat, prompt, re.IGNORECASE):
                return level
    return "moderate"


def _risk_to_etf_profile(risk: str) -> str:
    """Map tolerance to etf_allocation_desk risk_profile values."""
    return {"conservative": "conservative", "moderate": "balanced", "aggressive": "aggressive"}.get(risk, "balanced")


def _extract_strategy_type(prompt: str) -> str:
    """Extract convertible bond strategy type from prompt.

    Args:
        prompt: User's natural language prompt.

    Returns:
        Strategy type label used by the convertible bond preset.
    """
    for strategy_type, patterns in _STRATEGY_TYPE_PATTERNS:
        for pat in patterns:
            if re.search(pat, prompt, re.IGNORECASE):
                return strategy_type
    return "rotation"


def _extract_target_variable(prompt: str) -> str:
    """Extract ML prediction target from prompt.

    Args:
        prompt: User's natural language prompt.

    Returns:
        Prediction target label for ml_quant_lab.
    """
    for target_variable, patterns in _TARGET_VARIABLE_PATTERNS:
        for pat in patterns:
            if re.search(pat, prompt, re.IGNORECASE):
                return target_variable
    return "return"


def _extract_review_period(prompt: str) -> str:
    """Extract portfolio review cadence from prompt.

    Args:
        prompt: User's natural language prompt.

    Returns:
        Review cadence label for portfolio_review_board.
    """
    for review_period, patterns in _REVIEW_PERIOD_PATTERNS:
        for pat in patterns:
            if re.search(pat, prompt, re.IGNORECASE):
                return review_period
    return "quarterly"


def _extract_sector(prompt: str) -> str:
    """Extract sector constraint from prompt.

    Args:
        prompt: User's natural language prompt.

    Returns:
        Sector filter value, or empty string for full-market scans.
    """
    broad_market_patterns = [
        r"\bfull market\b",
        r"\bbroad market\b",
        r"\ball sectors\b",
        r"\bacross sectors\b",
    ]
    for pat in broad_market_patterns:
        if re.search(pat, prompt, re.IGNORECASE):
            return ""

    for sector, patterns in _SECTOR_PATTERNS:
        for pat in patterns:
            if re.search(pat, prompt, re.IGNORECASE):
                return sector
    return ""


def _first_label(prompt: str, table: list[tuple[str, list[str]]], default: str) -> str:
    """Return the first label in ``table`` whose patterns match ``prompt``.

    Args:
        prompt: User's natural language prompt.
        table: (label, patterns) rows, most specific first.
        default: Label used when nothing matches.

    Returns:
        The matched label, or ``default``.
    """
    for label, patterns in table:
        for pat in patterns:
            if re.search(pat, prompt, re.IGNORECASE):
                return label
    return default


def _extract_commodity(prompt: str) -> str | None:
    """Extract the commodity under discussion (V1).

    Returns:
        A commodity label, or None when the prompt names none — the caller
        then leans on ``{goal}`` instead of silently analysing gold.
    """
    found = _first_label(prompt, _COMMODITY_PATTERNS, "")
    return found or None


def _extract_horizon(prompt: str) -> str:
    """Extract an investment horizon label (commodity_research_team)."""
    return _first_label(prompt, _HORIZON_PATTERNS, "3 months")


def _extract_timeframe(prompt: str, default: str = "medium-term 1-3 months") -> str:
    """Extract a trading/analysis timeframe label."""
    return _first_label(prompt, _TIMEFRAME_PATTERNS, default)


def _extract_view(prompt: str) -> str:
    """Extract a directional view (derivatives_strategy_desk)."""
    return _first_label(prompt, _VIEW_PATTERNS, "neutral")


def _extract_factor_type(prompt: str) -> str:
    """Extract a factor family (factor_research_committee)."""
    return _first_label(prompt, _FACTOR_TYPE_PATTERNS, "value")


def _extract_event_type(prompt: str) -> str:
    """Extract an event family (event_driven_task_force)."""
    return _first_label(prompt, _EVENT_TYPE_PATTERNS, "all types")


def _extract_fund_type(prompt: str) -> str:
    """Extract a fund family (fund_selection_panel)."""
    return _first_label(prompt, _FUND_TYPE_PATTERNS, "equity")


def _extract_crypto_targets(prompt: str) -> str:
    """Extract the crypto assets named in ``prompt`` (V1).

    Args:
        prompt: User's natural language prompt.

    Returns:
        Comma-separated uppercase tickers in first-appearance order, or the
        ``BTC, ETH, SOL`` majors when the prompt names none.
    """
    found: list[str] = []
    for match in _CRYPTO_TICKER_PATTERN.finditer(prompt):
        ticker = match.group(1).upper()
        ticker = "BTC" if ticker == "XBT" else ticker
        if ticker not in found:
            found.append(ticker)
    for pattern, ticker in _CRYPTO_NAME_ALIASES:
        if re.search(pattern, prompt, re.IGNORECASE) and ticker not in found:
            found.append(ticker)
    return ", ".join(found) if found else _CRYPTO_DEFAULT_TARGET


def _snippet(prompt: str, max_len: int = 240) -> str:
    """Trim prompt for auxiliary fields."""
    s = prompt.strip()
    return s if len(s) <= max_len else s[: max_len - 3] + "..."


def _build_variables(preset_name: str, prompt: str) -> dict[str, str]:
    """Build template variables from prompt for the matched preset.

    Args:
        preset_name: Matched preset name.
        prompt: User's original prompt.

    Returns:
        Dict of template variables required by the YAML preset.
    """
    market = _extract_market(prompt)
    risk = _extract_risk_tolerance(prompt)
    goal = prompt.strip()
    g = _snippet(goal, 2000)

    # Preset-specific variable sets (see agent/src/swarm/presets/*.yaml).
    builders: dict[str, dict[str, str]] = {
        "global_allocation_committee": {"goal": g, "risk_tolerance": risk},
        "equity_research_team": {"market": market, "goal": g},
        "quant_strategy_desk": {"market": market, "goal": g},
        "risk_committee": {"goal": g},
        "factor_research_committee": {
            "market": market,
            "factor_type": _extract_factor_type(prompt),
        },
        "event_driven_task_force": {
            "market": market,
            "event_type": _extract_event_type(prompt),
        },
        "etf_allocation_desk": {"risk_profile": _risk_to_etf_profile(risk), "market": market},
        "derivatives_strategy_desk": {"target": g, "view": _extract_view(prompt)},
        "crypto_research_lab": {
            "target": _extract_crypto_targets(prompt),
            "timeframe": _extract_timeframe(prompt),
            # The extractor falls back to the majors when no ticker is named;
            # {goal} keeps the user's actual ask in front of the workers so a
            # fallback reads as "these majors, for THIS question".
            "goal": g,
        },
        "credit_research_team": {"target": g, "market": "China credit bonds"},
        "convertible_bond_team": {
            "market": "A-share convertible bonds",
            "goal": g,
            "strategy_type": _extract_strategy_type(prompt),
        },
        "fundamental_research_team": {"target": g, "market": market},
        "commodity_research_team": {
            # No hard-coded "gold": an unnamed commodity now defers to {goal}
            # rather than sending a three-agent team to research bullion the
            # user never mentioned.
            "commodity": _extract_commodity(prompt) or "the commodity named in the request below",
            "horizon": _extract_horizon(prompt),
            "goal": g,
        },
        "fund_selection_panel": {"fund_type": _extract_fund_type(prompt), "goal": g},
        "social_alpha_team": {"target": g, "timeframe": "daily"},
        "geopolitical_war_room": {"crisis": g, "market": market},
        "pairs_research_lab": {"market": market, "sector": _extract_sector(prompt)},
        "investment_committee": {"target": g, "market": market},
        "macro_strategy_forum": {"market": market, "horizon": "quarterly"},
        "statistical_arbitrage_desk": {"market": market, "goal": g, "sector": _extract_sector(prompt)},
        "sentiment_intelligence_team": {"market": market, "timeframe": "daily"},
        "technical_analysis_panel": {"target": g, "timeframe": "daily"},
        "sector_rotation_team": {"market": market, "goal": g},
        "portfolio_review_board": {"portfolio": g, "review_period": _extract_review_period(prompt), "goal": g},
        "ml_quant_lab": {"market": market, "target_variable": _extract_target_variable(prompt), "goal": g},
        # V1: the four presets below had YAMLs but no builder row, so even once
        # reachable they would have fallen through to the {market, goal}
        # default and left their own template variables unsubstituted.
        "macro_rates_fx_desk": {"goal": g, "timeframe": _extract_timeframe(prompt, "quarterly")},
        "earnings_research_desk": {"target": g},
        "global_equities_desk": {"goal": g, "risk_tolerance": risk},
        "crypto_trading_desk": {
            "target": _extract_crypto_targets(prompt),
            "timeframe": _extract_timeframe(prompt),
        },
    }

    return builders.get(preset_name, {"market": market, "goal": g})


class SwarmTool(BaseTool):
    """Launch a swarm multi-agent team to execute complex tasks.

    Accepts a natural-language prompt, auto-selects the best preset,
    and blocks synchronously until the swarm run completes or times out.
    """

    name = "run_swarm"
    description = (
        "Run a multi-agent swarm team for complex analysis tasks. "
        "Provide a natural language prompt and, when known, an explicit preset_name from agent/src/swarm/presets "
        "(e.g. equity_research_team, quant_strategy_desk, global_allocation_committee, risk_committee) "
        "so follow-up/continuation prompts do not lose routing context. "
        "A swarm normally runs for tens of minutes; this call blocks until the run "
        "finishes or the wait budget runs out. If it returns wait_budget_exhausted=true, "
        "the run is STILL RUNNING in the background — call run_swarm again with that "
        "run_id (no prompt needed) to keep waiting on the SAME run instead of starting "
        "a new one. "
        "Example: run_swarm(prompt='Analyze A-share new energy opportunities for Q2 2026', preset_name='equity_research_team')"
    )
    parameters = {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "Natural language description of the analysis task. Omit only when resuming via run_id.",
            },
            "preset_name": {
                "type": "string",
                "description": "Optional explicit swarm preset name when the user named one or this is a continuation.",
            },
            "run_id": {
                "type": "string",
                "description": (
                    "Resume waiting on an existing background run (the run_id returned "
                    "alongside wait_budget_exhausted=true). Starts no new run and costs "
                    "no extra tokens; prompt/preset_name are ignored when set."
                ),
            },
        },
        "required": [],
    }
    is_readonly = False
    repeatable = True  # loop.py dedups by tool name; each prompt is a distinct run (#42)

    @property
    def timeout_seconds(self) -> float:
        """Loop-side watchdog bound for run_swarm (V1).

        A swarm is a multi-layer DAG of LLM workers; tens of minutes is its
        NORMAL runtime, not a hang. The tool runs its own budget-clamped wait
        (``cap_timeout(SWARM_TIMEOUT, reserve_s=_WAIT_RESERVE_S)``) and returns
        ``wait_budget_exhausted`` with the run_id when that expires — the
        loop's watchdog must sit strictly OUTSIDE it so it only ever fires on a
        real hang. Pinning the watchdog to the tenant-wide tool timeout instead
        made it fire at 600s and discard the run_id, which is the P0 this
        property fixes.

        A property (not a class attribute) so ``SWARM_TIMEOUT`` / test
        monkeypatching of ``_MAX_WAIT_SECONDS`` still apply at call time;
        ``getattr(instance, "timeout_seconds")`` evaluates it normally.

        Returns:
            The swarm wait budget plus the dead-heat margin, in seconds.
        """
        return float(_MAX_WAIT_SECONDS) + _LOOP_WATCHDOG_MARGIN_S

    def __init__(
        self,
        *,
        include_shell_tools: bool = False,
        event_callback: Any | None = None,
    ) -> None:
        """Initialize the swarm launcher.

        Args:
            include_shell_tools: Whether worker registries may include shell
                execution tools requested by presets.
            event_callback: Optional session event bridge used by the web chat.
        """
        self.include_shell_tools = include_shell_tools
        self._event_callback = event_callback
        # preset -> {"ts", "run_id", "salvage"} for the failure cooldown (F3).
        # Instance-scoped: one SwarmTool lives per session registry, so the
        # cooldown naturally covers "the same run/session".
        self._recent_failures: dict[str, dict[str, Any]] = {}

    def _record_preset_failure(self, preset: str, run_obj: Any) -> None:
        """Remember a failed run's completed-worker products for salvage (F3)."""
        completed: list[dict[str, Any]] = []
        for task in (getattr(run_obj, "tasks", None) or [])[:_SALVAGE_MAX_TASKS * 2]:
            status = getattr(task, "status", None)
            status = status.value if hasattr(status, "value") else str(status)
            if status != "completed":
                continue
            summary = str(getattr(task, "summary", "") or "")
            completed.append({
                "id": getattr(task, "id", ""),
                "agent_id": getattr(task, "agent_id", ""),
                "summary": summary[:_SALVAGE_TASK_MAX_CHARS],
            })
            if len(completed) >= _SALVAGE_MAX_TASKS:
                break
        self._recent_failures[preset] = {
            "ts": time.monotonic(),
            "run_id": getattr(run_obj, "id", None),
            "salvage": {
                "final_report": str(getattr(run_obj, "final_report", "") or "")[
                    :_SALVAGE_REPORT_MAX_CHARS
                ],
                "completed_tasks": completed,
            },
        }

    def _cooldown_rejection(self, preset: str) -> str | None:
        """Return a structured refusal when ``preset`` failed recently (F3)."""
        record = self._recent_failures.get(preset)
        if record is None:
            return None
        elapsed = time.monotonic() - record["ts"]
        if elapsed >= _FAILURE_COOLDOWN_SECONDS:
            self._recent_failures.pop(preset, None)
            return None
        retry_after = int(_FAILURE_COOLDOWN_SECONDS - elapsed)
        return json.dumps(
            {
                "status": "rejected",
                "error_code": "swarm_preset_cooldown",
                "preset": preset,
                "failed_run_id": record.get("run_id"),
                "retry_after_s": retry_after,
                "salvage": record.get("salvage") or {},
                "message": (
                    f"Preset '{preset}' failed {int(elapsed)}s ago in this run; "
                    "an immediate identical re-run is refused because a systemic "
                    "upstream issue would very likely kill it again. Salvage the "
                    "completed workers' products above (salvage.completed_tasks / "
                    "salvage.final_report), fill gaps with your own research, and "
                    "answer from that — or use a different preset. The cooldown "
                    f"lifts in {retry_after}s."
                ),
            },
            ensure_ascii=False,
        )

    def _emit_swarm_usage(self, run_id: str, run_obj: Any) -> None:
        """Report swarm worker token totals through the same ``llm_usage``
        event channel the main loop uses. Without this the caller's billing /
        daily-quota accounting only ever saw main-loop usage — swarm-heavy
        attempts under-reported by orders of magnitude (incident 2026-08-25:
        run #8 recorded 21k output tokens while its two swarm runs burned
        hundreds of thousands)."""
        tin = int(getattr(run_obj, "total_input_tokens", 0) or 0)
        tout = int(getattr(run_obj, "total_output_tokens", 0) or 0)
        if tin <= 0 and tout <= 0:
            return
        self._emit_session_event(
            "llm_usage",
            {
                "input_tokens": tin,
                "output_tokens": tout,
                "total_tokens": tin + tout,
                "source": "swarm",
                "run_id": run_id,
            },
        )

    def _emit_session_event(self, event_type: str, data: dict[str, Any]) -> None:
        """Forward swarm status to the hosting session SSE channel if present."""
        if self._event_callback is None:
            return
        try:
            self._event_callback(event_type, data)
        except Exception:
            logger.warning("Failed to forward %s to session event stream", event_type, exc_info=True)

    def execute(self, **kwargs: Any) -> str:
        """Start a swarm run: auto-match preset, extract variables, wait for completion.

        Args:
            **kwargs: Must include prompt (str), unless run_id is given to
                resume waiting on an existing background run.

        Returns:
            JSON string with status, preset, variables, final_report, tasks, token_usage.
        """
        # V1: resume takes precedence — a caller holding a run_id wants MORE
        # waiting on that run, never a second run.
        resume_run_id = str(kwargs.get("run_id") or "").strip()
        if resume_run_id:
            return self._resume_run(resume_run_id)

        prompt = kwargs.get("prompt", "")

        if not prompt:
            return json.dumps(
                {
                    "status": "error",
                    "error": (
                        "Missing 'prompt' parameter. Pass a prompt to start a new "
                        "swarm, or run_id to resume waiting on an existing run."
                    ),
                },
                ensure_ascii=False,
            )

        preset, preset_error = _resolve_preset(prompt, kwargs.get("preset_name"))
        if preset_error:
            return json.dumps(
                {"status": "error", "error": preset_error},
                ensure_ascii=False,
            )
        assert preset is not None

        # F3: refuse an identical-preset re-run inside the failure cooldown.
        rejection = self._cooldown_rejection(preset)
        if rejection is not None:
            logger.warning("SwarmTool: preset %s rejected by failure cooldown", preset)
            return rejection

        variables = _build_variables(preset, prompt)
        # An explicitly passed preset_name is by definition an explicit choice;
        # otherwise report how confidently the keywords picked it (V1).
        preset_score = (
            _EXPLICIT_NAME_SCORE
            if kwargs.get("preset_name")
            else _preset_route_score(prompt, preset)
        )

        # Per-attempt swarm accounting (surfaces in attempt_stats.swarm_runs).
        from src.core.fetch_stats import record_swarm

        t_exec = time.monotonic()

        def _record(status: str, rid: str | None = None, **extra: Any) -> None:
            record_swarm(
                preset, rid, status, int((time.monotonic() - t_exec) * 1000), **extra
            )

        logger.info(
            "SwarmTool: resolved preset=%s, variables=%s from prompt: %s",
            preset,
            variables,
            prompt[:100],
        )

        from src.config import load_swarm_agent_config
        from src.swarm.runtime import SwarmRuntime
        from src.swarm.store import SwarmStore, swarm_runs_root

        # Single source of truth (honors VIBE_DATA_DIR for per-tenant isolation).
        swarm_base_dir = swarm_runs_root()
        swarm_base_dir.mkdir(parents=True, exist_ok=True)
        store = SwarmStore(base_dir=swarm_base_dir)
        # Boot-time / operator-trusted: even when reached via the in-process
        # agent tool, the config path is resolved from disk / env, never from
        # the calling LLM's prompt (R-06).
        agent_config = load_swarm_agent_config()
        runtime = SwarmRuntime(
            store=store,
            max_workers=int(os.getenv("SWARM_MAX_WORKERS", "4")),
            agent_config=agent_config,
        )

        pending_live_events: list[dict[str, Any]] = []
        run_id_holder: dict[str, str | None] = {"run_id": None}

        try:
            def _live_callback(event: Any) -> None:
                payload = event.model_dump()
                current_run_id = run_id_holder["run_id"]
                if current_run_id is None:
                    pending_live_events.append(payload)
                    return
                self._emit_session_event(
                    "swarm.event",
                    {"run_id": current_run_id, "event": payload},
                )

            run = runtime.start_run(
                preset,
                variables,
                live_callback=_live_callback if self._event_callback is not None else None,
                include_shell_tools=self.include_shell_tools,
            )
        except FileNotFoundError as exc:
            _record("start_failed")
            return json.dumps(
                {"status": "error", "error": f"Preset not found: {exc}"},
                ensure_ascii=False,
            )
        except ValueError as exc:
            _record("start_failed")
            return json.dumps(
                {"status": "error", "error": f"Invalid DAG: {exc}"},
                ensure_ascii=False,
            )
        except Exception as exc:
            _record("start_failed")
            return json.dumps(
                {"status": "error", "error": f"Failed to start swarm: {exc}"},
                ensure_ascii=False,
            )

        run_id = run.id
        run_id_holder["run_id"] = run_id
        run_agents = len(run.agents)
        run_tasks = len(run.tasks)
        logger.info("SwarmTool: started run %s (preset=%s)", run_id, preset)
        self._emit_session_event(
            "swarm.started",
            {
                "run_id": run_id,
                "preset": preset,
                "variables": variables,
                "status": run.status.value,
                "agents": [agent.model_dump() for agent in run.agents],
                "tasks": [task.model_dump() for task in run.tasks],
            },
        )
        for event_payload in pending_live_events:
            self._emit_session_event(
                "swarm.event",
                {"run_id": run_id, "event": event_payload},
            )
        pending_live_events.clear()

        return self._wait_for_run(
            store=store,
            run_id=run_id,
            preset=preset,
            variables=variables,
            run_agents=run_agents,
            run_tasks=run_tasks,
            record=_record,
            preset_score=preset_score,
        )

    def _wait_for_run(
        self,
        *,
        store: Any,
        run_id: str,
        preset: str,
        variables: dict[str, str],
        run_agents: int,
        run_tasks: int,
        record: Any,
        resumed: bool = False,
        preset_score: float | None = None,
    ) -> str:
        """Poll an in-flight swarm run until terminal state or wait budget.

        Shared by a fresh ``run_swarm`` call and a ``run_id`` resume so both
        paths produce byte-identical result envelopes and identical accounting.

        Args:
            store: SwarmStore used to load/reconcile the run.
            run_id: Run being waited on.
            preset: Preset name (for accounting and the result envelope).
            variables: Template variables the run was started with.
            run_agents: Agent count, for accounting.
            run_tasks: Task count, for accounting.
            record: ``_record``-shaped callable for per-attempt swarm stats.
            resumed: Whether this wait resumed an existing background run.
            preset_score: Routing confidence for the chosen preset (V1).

        Returns:
            JSON result string (terminal result, wait_budget_exhausted, or error).
        """
        # Cap the wait by the attempt's remaining wall-clock budget (batch 3):
        # a swarm wait must never outlive the caller's own deadline — keep a
        # reserve so the main loop can still turn partial results into an answer.
        # V1: the loop's own watchdog now sits OUTSIDE this (see
        # ``SwarmTool.timeout_seconds``), so this is the wait that actually
        # expires first and the salvage return below is reachable again.
        from src.core.budget import cap_timeout

        max_wait = cap_timeout(
            float(_MAX_WAIT_SECONDS), reserve_s=_WAIT_RESERVE_S, floor_s=_WAIT_FLOOR_S
        )
        t0 = time.monotonic()
        while time.monotonic() - t0 < max_wait:
            time.sleep(_POLL_INTERVAL_SECONDS)

            loaded = store.load_run(run_id)
            if loaded is None:
                record("error", run_id, agents=run_agents, tasks=run_tasks)
                return json.dumps(
                    {"status": "error", "error": f"Run {run_id} disappeared"},
                    ensure_ascii=False,
                )

            reconciled = store.reconcile_run(loaded, write=True)
            if reconciled.status.value in ("completed", "failed", "cancelled"):
                record(
                    reconciled.status.value, run_id, agents=run_agents, tasks=run_tasks,
                    llm_ms=reconciled.total_llm_ms, tool_ms=reconciled.total_tool_ms,
                    input_tokens=reconciled.total_input_tokens,
                    output_tokens=reconciled.total_output_tokens,
                )
                self._emit_swarm_usage(run_id, reconciled)
                if reconciled.status.value == "failed":
                    # F3: arm the cooldown with salvageable worker products.
                    self._record_preset_failure(preset, reconciled)
                return _format_result(
                    reconciled, preset, variables,
                    resumed=resumed, preset_score=preset_score,
                )

        # Wait budget elapsed but the run is still in flight. Do NOT cancel —
        # the daemon thread keeps working and the agent can decide to wait
        # more (re-invoke with the returned run_id) or hand off partial state
        # to the user. Cancelling here used to throw away minutes of LLM cost
        # whenever a preset legitimately ran past the budget.
        loaded = store.load_run(run_id)
        if loaded is not None:
            record(
                "wait_budget_exhausted", run_id, agents=run_agents, tasks=run_tasks,
                llm_ms=loaded.total_llm_ms, tool_ms=loaded.total_tool_ms,
                input_tokens=loaded.total_input_tokens,
                output_tokens=loaded.total_output_tokens,
            )
            # Tokens burned so far still get billed; the run keeps burning in
            # the background and that tail is knowingly under-reported.
            self._emit_swarm_usage(run_id, loaded)
            return _format_result(
                store.reconcile_run(loaded, write=True),
                preset,
                variables,
                timed_out=True,
                resumed=resumed,
                preset_score=preset_score,
            )

        record("timeout", run_id, agents=run_agents, tasks=run_tasks)
        return json.dumps(
            {"status": "timeout", "error": f"Swarm run {run_id} timed out after {_MAX_WAIT_SECONDS}s"},
            ensure_ascii=False,
        )

    def _resume_run(self, run_id: str) -> str:
        """Resume waiting on an existing background run (V1 / F).

        ``wait_budget_exhausted`` has always told the model to "re-invoke with
        the returned run_id", but ``parameters`` carried no such field, so the
        only executable option was starting a whole new run. This closes that
        gap: no new workers, no new tokens, just more waiting on the run that
        is already burning in the background.

        Args:
            run_id: The run to resume waiting on.

        Returns:
            JSON result string, or a structured error when the run is unknown.
        """
        from src.core.fetch_stats import record_swarm
        from src.swarm.store import SwarmStore, swarm_runs_root

        store = SwarmStore(base_dir=swarm_runs_root())
        loaded = store.load_run(run_id)
        if loaded is None:
            return json.dumps(
                {
                    "status": "error",
                    "error_code": "swarm_run_not_found",
                    "run_id": run_id,
                    "message": (
                        f"No swarm run '{run_id}' in this workspace. Only a run_id "
                        "returned by an earlier run_swarm call in this session can be "
                        "resumed; to start fresh work, call run_swarm with a prompt."
                    ),
                },
                ensure_ascii=False,
            )

        preset = getattr(loaded, "preset_name", "") or ""
        variables = dict(getattr(loaded, "user_vars", None) or {})
        t_exec = time.monotonic()

        def _record(status: str, rid: str | None = None, **extra: Any) -> None:
            record_swarm(
                preset, rid, status, int((time.monotonic() - t_exec) * 1000),
                resumed=True, **extra,
            )

        logger.info("SwarmTool: resuming wait on run %s (preset=%s)", run_id, preset)
        return self._wait_for_run(
            store=store,
            run_id=run_id,
            preset=preset,
            variables=variables,
            run_agents=len(getattr(loaded, "agents", None) or []),
            run_tasks=len(getattr(loaded, "tasks", None) or []),
            record=_record,
            resumed=True,
            # The route was decided by the original call; a resume neither
            # re-routes nor re-scores.
            preset_score=_EXPLICIT_NAME_SCORE if preset else None,
        )


def _format_result(
    run: Any,
    preset: str,
    variables: dict[str, str],
    timed_out: bool = False,
    resumed: bool = False,
    preset_score: float | None = None,
) -> str:
    """Format a SwarmRun into a JSON result string.

    Args:
        run: SwarmRun instance.
        preset: Matched preset name.
        variables: Extracted variables.
        timed_out: Whether the run was terminated due to timeout.
        resumed: Whether this result came from a run_id resume.
        preset_score: Routing confidence for the chosen preset (V1).

    Returns:
        JSON string with run status, report, task summaries, and token usage.
    """
    from src.swarm.serialization import run_level_error, serialize_task

    task_summaries = [serialize_task(task) for task in run.tasks]

    # ``timed_out`` only means the SwarmTool's wait budget elapsed — the run
    # itself is still progressing in the background. Surface the run's real
    # status so a downstream agent can re-invoke with the run_id (or end its
    # turn with a "still working" message) instead of treating it as failure.
    result: dict[str, Any] = {
        "status": run.status.value,
        "wait_budget_exhausted": timed_out,
        "run_id": run.id,
        "preset": preset,
        "auto_variables": variables,
        # V1: routing confidence next to the variables it produced. 99.0 = the
        # preset was named outright; a low positive score = a weak keyword
        # match; 0.0 = nothing matched and this is the equity_research_team
        # fallback. Previously indistinguishable from a confident route.
        "preset_score": preset_score,
        "final_report": run.final_report or "",
        "error": run_level_error(run),
        "tasks": task_summaries,
        "token_usage": {
            "total_input_tokens": run.total_input_tokens,
            "total_output_tokens": run.total_output_tokens,
        },
        # Cumulative worker effort (layers run in parallel, so these can
        # exceed the run's wall-clock).
        "time_split_ms": {
            "llm": run.total_llm_ms,
            "tools": run.total_tool_ms,
        },
    }
    if resumed:
        result["resumed"] = True
    if timed_out:
        # Spell out the executable next step: the run_id above is now an
        # accepted parameter, so "wait more" is a real option (V1).
        result["next_step"] = (
            "This run is still executing in the background. To keep waiting, call "
            f"run_swarm(run_id='{run.id}') — it starts no new run and costs no extra "
            "tokens. Otherwise report the partial results above as partial."
        )
    return json.dumps(result, ensure_ascii=False, indent=2)
