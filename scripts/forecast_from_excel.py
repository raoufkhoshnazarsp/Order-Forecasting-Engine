#!/usr/bin/env python
"""Run the full Excel -> forecast pipeline from the command line.

Usage:
    python scripts/forecast_from_excel.py --file data/orders.xlsx --horizon 30
    python scripts/forecast_from_excel.py --file data/orders.xlsx --out reports/run1
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ingestion.excel import ExcelInputError, write_template
from src.pipeline import PipelineError, run_pipeline


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", help="input workbook (.xlsx)")
    parser.add_argument("--horizon", type=int, default=30)
    parser.add_argument("--model", default="auto")
    parser.add_argument("--out", default="reports/excel_runs")
    parser.add_argument("--template", metavar="PATH", help="write a template workbook and exit")
    args = parser.parse_args(argv)

    if args.template:
        path = write_template(args.template)
        print(f"Template written to {path}")
        return 0
    if not args.file:
        parser.error("--file is required unless --template is used")

    try:
        result = run_pipeline(args.file, horizon=args.horizon, model=args.model)
    except (ExcelInputError, PipelineError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    out_dir = Path(args.out) / result.run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    assert result.forecast is not None
    result.forecast.to_csv(out_dir / "forecast.csv", index=False, encoding="utf-8-sig")
    (out_dir / "summary.json").write_text(result.to_json(), encoding="utf-8")

    print(result.to_json())
    print(f"\nSaved to {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
