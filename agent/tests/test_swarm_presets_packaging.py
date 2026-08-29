"""Regression tests for swarm preset discovery.

These guard against the v0.1.5 packaging bug (issue #55), where preset
YAMLs were declared via ``[tool.setuptools.data-files]`` and ended up at
``<venv>/config/swarm/`` while the loader looked under
``<site-packages>/config/swarm/``. Moving the YAMLs into the
``src.swarm.presets`` package keeps source-installs and built wheels in
sync; these tests fail fast if either side drifts again.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.swarm.presets import PRESETS_DIR, list_presets, load_preset


# Lock to the canonical roster shipped today. Bump intentionally if a preset
# is added or removed so a release that silently drops files is caught here.
EXPECTED_PRESET_COUNT = 29


def test_presets_dir_lives_inside_swarm_package() -> None:
    """PRESETS_DIR must be a sibling of presets.py so wheels can find it."""
    import src.swarm.presets as presets_module

    module_dir = Path(presets_module.__file__).resolve().parent
    assert PRESETS_DIR == module_dir / "presets"
    assert PRESETS_DIR.is_dir(), f"presets dir missing: {PRESETS_DIR}"


def test_list_presets_returns_full_roster() -> None:
    presets = list_presets()
    assert len(presets) == EXPECTED_PRESET_COUNT, (
        f"expected {EXPECTED_PRESET_COUNT} presets, got {len(presets)} — "
        "check pyproject package-data and that YAMLs were not dropped"
    )


def test_every_preset_yaml_is_loadable() -> None:
    """Every YAML in the bundle must parse and expose required keys."""
    for entry in list_presets():
        name = entry["name"]
        data = load_preset(name)
        assert isinstance(data, dict), f"preset {name} did not parse to dict"
        assert data.get("agents"), f"preset {name} has no agents"
        assert data.get("tasks"), f"preset {name} has no tasks"


@pytest.mark.parametrize(
    "preset_name",
    ["investment_committee", "quant_strategy_desk", "risk_committee"],
)
def test_known_presets_load(preset_name: str) -> None:
    """Spot-check a few headline presets advertised in docs/UI."""
    data = load_preset(preset_name)
    assert data["agents"], f"{preset_name} has no agents"


def test_preset_names_are_derived_from_the_yaml_roster() -> None:
    """``_PRESET_NAMES`` must be the YAML roster, not the keyword table (V1).

    It used to be derived from ``_PRESET_KEYWORDS``, so the four presets with
    no keyword row (crypto_trading_desk / earnings_research_desk /
    global_equities_desk / macro_rates_fx_desk) were rejected as "Unknown
    preset" even when named outright.
    """
    from src.tools import swarm_tool

    roster = {entry["name"] for entry in list_presets()}
    missing = sorted(roster - set(swarm_tool._PRESET_NAMES))
    assert not missing, f"presets shipped as YAML but not accepted: {missing}"
    for name in sorted(roster):
        assert swarm_tool._normalize_preset_name(name) == name


def test_every_preset_has_keyword_and_builder_coverage() -> None:
    """Keyword table and variable builders must cover every shipped preset.

    A preset missing from ``_PRESET_KEYWORDS`` can never be auto-routed and
    (before V1) could not even be named; a preset missing from the
    ``_build_variables`` builders table falls through to the ``{market, goal}``
    default and ships its own template variables unsubstituted. Both failures
    are silent at runtime, which is why they are asserted here.
    """
    from src.tools import swarm_tool

    keyword_presets = {name for name, _, _ in swarm_tool._PRESET_KEYWORDS}
    uncovered: dict[str, list[str]] = {}
    for entry in list_presets():
        name = entry["name"]
        assert name in keyword_presets, f"{name} has no _PRESET_KEYWORDS row"
        declared = {
            (item["name"] if isinstance(item, dict) else str(item))
            for item in (entry["variables"] or [])
        }
        built = set(swarm_tool._build_variables(name, "analyze A-share opportunities"))
        missing = sorted(declared - built)
        if missing:
            uncovered[name] = missing
    assert not uncovered, f"_build_variables does not supply declared variables: {uncovered}"
