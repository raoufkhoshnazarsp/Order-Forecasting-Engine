#!/usr/bin/env python
"""End-to-end smoke run on synthetic data (no ClickHouse required).

Proves the pipeline path: synthetic facts -> data mart -> backtest ->
daily forecast output, with all coherence checks enforced.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import config
from src.data_mart.entity_master import EntityMaster
from src.data_mart.orders import build_daily_mart, validate_total_reconciliation
from src.evaluation.backtest import BacktestConfig, backtest, summarize
from src.models.baselines import BASELINES
from src.serving.service import RunContext, run_forecast

ENTITIES = [("FASHION", "VERTICAL", 420), ("GOLD", "VERTICAL", 130),
            ("H&B", "VERTICAL", 90), ("V001", "VENTURE", 60), ("V002", "VENTURE", 35)]


def synthetic_facts(start: date, days: int, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rows = []
    for entity_id, entity_type, base in ENTITIES:
        for i in range(days):
            day = start + timedelta(days=i)
            weekly = 1 + 0.18 * np.sin(2 * np.pi * i / 7)
            trend = 1 + 0.0025 * i
            noise = rng.normal(0, 0.06)
            rows.append(
                {
                    "date": day,
                    "entity_id": entity_id,
                    "entity_type": entity_type,
                    "successful_orders": max(0, int(round(base * weekly * trend * (1 + noise)))),
                }
            )
    return pd.DataFrame(rows)


def smoke_entity_master(tmp_dir: Path) -> EntityMaster:
    """Temp master with synthetic ventures; the real master stays untouched."""
    rows = ["entity_id,entity_name,entity_type,parent_entity_id,is_active,valid_from,valid_to",
            "TOTAL,Total Business,TOTAL,,true,2023-01-01,"]
    for name in ("DIGITAL", "FASHION", "FMCG", "GOLD", "H&B", "Home", "Services"):
        entity_id = {"H&B": "H_B", "Home": "HOME"}.get(name, name.upper())
        rows.append(f"{entity_id},{name},VERTICAL,TOTAL,true,2023-01-01,")
    rows.append("V001,Venture One,VENTURE,TOTAL,true,2023-01-01,")
    rows.append("V002,Venture Two,VENTURE,TOTAL,true,2023-01-01,")
    path = tmp_dir / "entity_master_smoke.csv"
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return EntityMaster.load(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=400)
    parser.add_argument("--out", default="artifacts/smoke")
    args = parser.parse_args(argv)

    start = date.today() - timedelta(days=args.days)
    end = date.today()
    facts = synthetic_facts(start, args.days)
    observed = set(facts["date"].unique())

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    master = smoke_entity_master(out_dir)
    vertical_map = config.load_yaml("hierarchy.yaml").get("vertical_entity_map", {})
    facts["entity_id"] = facts.apply(
        lambda r: vertical_map.get(r["entity_id"], r["entity_id"])
        if r["entity_type"] == "VERTICAL" else r["entity_id"],
        axis=1,
    )
    mart = build_daily_mart(
        facts, master, start=start, end=end,
        snapshot_id="smoke_snapshot", observed_dates=observed,
    )
    validate_total_reconciliation(mart)
    print(f"mart rows: {len(mart)} | total on last day: "
          f"{mart[(mart['entity_type']=='TOTAL') & (mart['date']==max(observed))]['successful_orders'].iloc[0]}")

    observed_rows = mart[mart["quality_flag"].eq("OBSERVED") & mart["successful_orders"].notna()].copy()
    observed_rows["successful_orders"] = observed_rows["successful_orders"].astype("int64")

    bt = backtest(observed_rows, BASELINES, BacktestConfig(horizon=30, step=30, min_train_days=120))
    scorecard = summarize(bt)
    overall = scorecard.groupby("model_name")[["wape", "bias"]].mean().sort_values("wape")
    print("\nbaseline ranking (mean WAPE / Bias over entities & horizons):")
    print(overall.round(4).to_string())

    ctx = RunContext(
        forecast_run_id="run_smoke",
        forecast_origin=max(observed),
        model_name="seasonal_naive",
        model_version="baseline_seasonal_naive_v1",
        feature_version="features_v1",
        data_snapshot_id="smoke_snapshot",
    )
    forecast = run_forecast(observed_rows, master, {"seasonal_naive": BASELINES["seasonal_naive"]},
                            context=ctx, horizon=30)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    forecast.to_parquet(out_dir / "forecast.parquet", index=False)
    scorecard.to_csv(out_dir / "scorecard.csv", index=False)

    total = forecast[forecast["entity_id"] == "TOTAL"].set_index("horizon")["reconciled_yhat"]
    print(f"\nforecast rows: {len(forecast)} | entities: {forecast['entity_id'].nunique()}")
    print("TOTAL reconciled forecast D+1, D+7, D+30:",
          int(total.get(1, 0)), int(total.get(7, 0)), int(total.get(30, 0)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
