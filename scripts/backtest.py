#!/usr/bin/env python
"""Phase 3 — rolling-origin baseline backtest over the data mart.

Usage:
    python scripts/backtest.py --mart artifacts/marts/<id>/fact_daily_entity_metrics.parquet \
        --horizon 30 --step 7
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.evaluation.backtest import BacktestConfig, backtest, horizon_accuracy, summarize
from src.models.baselines import BASELINES


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mart", required=True)
    parser.add_argument("--horizon", type=int, default=30)
    parser.add_argument("--step", type=int, default=7)
    parser.add_argument("--out", default="reports/backtest")
    args = parser.parse_args(argv)

    mart = pd.read_parquet(args.mart)
    target = mart[
        mart["quality_flag"].eq("OBSERVED") & mart["successful_orders"].notna()
    ].copy()
    target["successful_orders"] = target["successful_orders"].astype("int64")

    cfg = BacktestConfig(horizon=args.horizon, step=args.step)
    results = backtest(target, BASELINES, cfg)
    if results.empty:
        print("Not enough history for a rolling-origin backtest", file=sys.stderr)
        return 1

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    results.to_csv(out_dir / "backtest_raw.csv", index=False)
    summary = summarize(results)
    summary.to_csv(out_dir / "baseline_scorecard.csv", index=False)
    acc = horizon_accuracy(results)
    acc.to_csv(out_dir / "horizon_accuracy.csv", index=False)

    overall = (
        summary.groupby("model_name")[["wape", "bias"]]
        .mean()
        .sort_values("wape")
        .reset_index()
    )
    overall.to_csv(out_dir / "model_ranking.csv", index=False)
    print(overall.to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
