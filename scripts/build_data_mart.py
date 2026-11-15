#!/usr/bin/env python
"""Phase 2 — build the daily data mart from an extraction snapshot.

Usage:
    python scripts/build_data_mart.py --snapshot artifacts/extracts/<id>
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.data_mart.entity_master import EntityMaster
from src.data_mart.orders import build_daily_mart, validate_total_reconciliation


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--out", default="artifacts/marts")
    args = parser.parse_args(argv)

    snapshot_dir = Path(args.snapshot)
    meta = json.loads((snapshot_dir / "snapshot.json").read_text())
    facts = pd.read_parquet(snapshot_dir / "daily_leaf_orders.parquet")

    start = date.fromisoformat(meta["start_date"])
    end = date.fromisoformat(meta["end_date"])
    observed = set(pd.to_datetime(facts["date"]).dt.date.unique())

    master = EntityMaster.load()
    mart = build_daily_mart(
        facts,
        master,
        start=start,
        end=end,
        snapshot_id=meta["snapshot_id"],
        observed_dates=observed,
    )
    validate_total_reconciliation(mart)

    out_dir = Path(args.out) / meta["snapshot_id"]
    out_dir.mkdir(parents=True, exist_ok=True)
    mart.to_parquet(out_dir / "fact_daily_entity_metrics.parquet", index=False)

    flags = mart["quality_flag"].value_counts().to_dict()
    print(json.dumps({"rows": len(mart), "quality_flags": flags}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
