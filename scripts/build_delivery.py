#!/usr/bin/env python
"""Produce the delivery workbook: 30 days after the last day in the source.

    python scripts/build_delivery.py --file uploads/data-model.xlsx \
        --out outputs/forecast.xlsx --horizon 30

The output has exactly these columns:

    date | level | vertical | category | order | GMV
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ingestion.excel import ExcelInputError, load_category_facts
from src.serving.delivery import (
    DeliveryError,
    build_delivery_frame,
    validate_delivery,
    write_delivery,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", required=True, help="source workbook (.xlsx)")
    parser.add_argument("--out", default="outputs/forecast.xlsx")
    parser.add_argument("--horizon", type=int, default=30)
    parser.add_argument("--model", default="auto")
    args = parser.parse_args(argv)

    try:
        facts = load_category_facts(args.file)
        result = build_delivery_frame(
            facts, origin=max(facts["date"]), horizon=args.horizon, model=args.model
        )
        validate_delivery(result.frame)
    except (ExcelInputError, DeliveryError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    path = write_delivery(result.frame, args.out)
    print(json.dumps(result.summary(), ensure_ascii=False, indent=2))
    print(f"\nWritten to {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
