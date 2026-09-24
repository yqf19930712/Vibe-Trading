"""Factor analysis tool: compute IC/IR, layered backtest, and output analysis report."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from src.agent.tools import BaseTool
from src.factors.factor_analysis_core import compute_ic_series, compute_group_equity

# Backward-compatible aliases for any external imports of the private names.
_compute_ic_series = compute_ic_series
_compute_group_equity = compute_group_equity


def run_factor_analysis(
    factor_csv: str, return_csv: str, output_dir: str, n_groups: int = 5
) -> str:
    """Run the full factor analysis pipeline: IC/IR + layered backtest.

    Args:
        factor_csv: Path to factor values CSV (index=date, columns=codes).
        return_csv: Path to returns CSV (same structure).
        output_dir: Directory for output files.
        n_groups: Number of quantile groups; default 5.

    Returns:
        JSON-formatted analysis summary.
    """
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    try:
        factor_df = pd.read_csv(factor_csv, index_col=0, parse_dates=True)
        return_df = pd.read_csv(return_csv, index_col=0, parse_dates=True)
    except Exception as e:
        return json.dumps({"status": "error", "error": f"Failed to read CSV: {e}"}, ensure_ascii=False)

    if factor_df.empty or return_df.empty:
        return json.dumps({"status": "error", "error": "Factor or return data is empty"}, ensure_ascii=False)

    ic_series = compute_ic_series(factor_df, return_df)
    if ic_series.empty:
        return json.dumps(
            {"status": "error", "error": "IC computation failed: insufficient shared dates/assets (need at least 5 per day)"},
            ensure_ascii=False,
        )

    ic_series.to_csv(out_path / "ic_series.csv", header=["IC"])

    ic_mean = float(ic_series.mean())
    ic_std = float(ic_series.std())
    ir = ic_mean / ic_std if ic_std > 0 else 0.0
    ic_positive_ratio = float((ic_series > 0).mean())

    summary = {
        "ic_mean": round(ic_mean, 6),
        "ic_std": round(ic_std, 6),
        "ir": round(ir, 4),
        "ic_positive_ratio": round(ic_positive_ratio, 4),
        "ic_count": len(ic_series),
    }
    (out_path / "ic_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    equity_df = compute_group_equity(factor_df, return_df, n_groups)
    if equity_df.empty:
        return json.dumps(
            {"status": "error", "error": "Layered backtest failed: insufficient valid cross-section dates"},
            ensure_ascii=False,
        )
    equity_df.to_csv(out_path / "group_equity.csv")

    # Long-short spread: last group vs. first group
    long_short_ret = float(equity_df.iloc[-1, -1] - equity_df.iloc[-1, 0])

    result = {
        "status": "ok",
        "ic_mean": summary["ic_mean"],
        "ic_std": summary["ic_std"],
        "ir": summary["ir"],
        "ic_positive_ratio": summary["ic_positive_ratio"],
        "ic_count": summary["ic_count"],
        "n_groups": n_groups,
        "long_short_spread": round(long_short_ret, 4),
        "group_final_equity": {
            col: round(float(equity_df[col].iloc[-1]), 4) for col in equity_df.columns
        },
        "output_dir": str(out_path),
        "files": ["ic_series.csv", "ic_summary.json", "group_equity.csv"],
    }
    return json.dumps(result, ensure_ascii=False, indent=2)


class FactorAnalysisTool(BaseTool):
    """Factor analysis tool: compute IC/IR and layered NAV."""

    name = "factor_analysis"
    description = (
        "Factor analysis: compute IC/IR and quantile-group (layered) NAV from a "
        "factor CSV and a forward-return CSV (both index=date, columns=codes, "
        "same shape), writing ic_series.csv / ic_summary.json / group_equity.csv "
        "to output_dir. Use it once the factor values have been computed and "
        "saved. Returns {status:'ok', ic_mean, ic_std, ir, ic_positive_ratio, "
        "ic_count, n_groups, long_short_spread, group_final_equity, output_dir, "
        "files}; status:'error' when a CSV is unreadable/empty or fewer than 5 "
        "assets share a date (IC needs a cross-section)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "factor_csv": {
                "type": "string",
                "description": "Factor values CSV path, relative to run_dir (index=date, columns=codes)",
            },
            "return_csv": {
                "type": "string",
                "description": "Returns CSV path, relative to run_dir (same structure)",
            },
            "n_groups": {
                "type": "integer",
                "description": "Number of quantile groups",
                "default": 5,
            },
            "output_dir": {
                "type": "string",
                "description": "Output directory for results, relative to run_dir",
            },
        },
        "required": ["factor_csv", "return_csv", "output_dir"],
    }

    def execute(self, **kwargs: Any) -> str:
        """Run factor analysis.

        Args:
            **kwargs: Must include factor_csv, return_csv, output_dir. Optional n_groups.

        Returns:
            JSON-formatted analysis summary.
        """
        from src.tools.path_utils import safe_tool_input, safe_tool_output_dir

        # Same boundary as the other file tools: reads come from the run dir
        # (or an earlier run / an upload), results go into the run dir.
        run_dir = kwargs.get("run_dir")
        try:
            factor_csv = safe_tool_input(str(kwargs["factor_csv"]), run_dir)
            return_csv = safe_tool_input(str(kwargs["return_csv"]), run_dir)
            output_dir = safe_tool_output_dir(str(kwargs["output_dir"]), run_dir)
        except ValueError as exc:
            return json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False)
        return run_factor_analysis(
            factor_csv=str(factor_csv),
            return_csv=str(return_csv),
            output_dir=str(output_dir),
            n_groups=kwargs.get("n_groups", 5),
        )
