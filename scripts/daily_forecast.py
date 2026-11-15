#!/usr/bin/env python
"""Daily forecast job: read the latest mart, forecast D+1..D+30, save output.

Usage:
    python scripts/daily_forecast.py --mart artifacts/marts/<id>/fact_daily_entity_metrics.parquet \
        --origin 2026-11-14 --out artifacts/forecasts
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.data_mart.entity_master import EntityMaster
from src.models.baselines import BASELINES
from src.serving.service import RunContext, run_forecast, to_records


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mart", required=True)
    parser.add_argument("--origin", default=None, help="forecast origin (default: last date in mart)")
    parser.add_argument("--horizon", type=int, default=30)
    parser.add_argument("--model", default="seasonal_naive", choices=sorted(BASELINES))
    parser.add_argument("--out", default="artifacts/forecasts")
    args = parser.parse_args(argv)

    mart = pd.read_parquet(args.mart)
    snapshot_id = str(mart["data_snapshot_id"].iloc[0]) if "data_snapshot_id" in mart else "unknown"
    origin = date.fromisoformat(args.origin) if args.origin else pd.to_datetime(mart["date"]).max().date()

    context = RunContext(
        forecast_run_id=f"run_{origin:%Y%m%d}_" + datetime.now().strftime("%H%M%S"),
        forecast_origin=origin,
        model_name=args.model,
        model_version=f"baseline_{args.model}_v1",
        feature_version="features_v1",
        data_snapshot_id=snapshot_id,
    )
    forecaster = {args.model: BASELINES[args.model]}
    try:
        forecast = run_forecast(
            mart, EntityMaster.load(), forecaster, context=context, horizon=args.horizon
        )
    except Exception as exc:  # noqa: BLE001 - fail loudly, never publish silently
        print(f"Forecast run failed: {exc}", file=sys.stderr)
        return 1

    out_dir = Path(args.out) / context.forecast_run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    forecast.to_parquet(out_dir / "forecast.parquet", index=False)
    (out_dir / "forecast.json").write_text(
        json.dumps(to_records(forecast), indent=2, default=str)
    )
    print(
        json.dumps(
            {
                "forecast_run_id": context.forecast_run_id,
                "rows": len(forecast),
                "entities": int(forecast["entity_id"].nunique()),
                "horizon": args.horizon,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
