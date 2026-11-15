#!/usr/bin/env python
"""Phase 0 — inspect ClickHouse sources and write discovery reports.

Usage:
    python scripts/discover_sources.py --start 2024-01-01 --end 2025-01-01
Writes reports/discovery/<query>.csv plus a JSON schema summary.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import config
from src.ingestion.clickhouse_client import ClickHouseUnavailable, build_client
from src.ingestion.render import render_template

DISCOVERY_QUERIES = [
    "discovery/02_date_fields.sql",
    "discovery/03_status_and_deletion.sql",
    "discovery/03b_deletion_behavior.sql",
    "discovery/04_scope_fields.sql",
    "discovery/06_venture_coverage.sql",
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--out", default="reports/discovery")
    args = parser.parse_args(argv)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        client = build_client()
    except ClickHouseUnavailable as exc:
        print(f"ClickHouse unavailable: {exc}", file=sys.stderr)
        return 2

    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    schemas = {}
    for table in ("fms_credit.tbl_order", "fms_credit.tbl_venture", "minio_db.commercial_data"):
        frame = client.query_df(f"DESCRIBE TABLE {table}")
        schemas[table] = frame.to_dict("records")
        frame.to_csv(out_dir / f"schema_{table.replace('.', '_')}.csv", index=False)

    (out_dir / "schemas.json").write_text(json.dumps(schemas, indent=2, default=str))

    for path in DISCOVERY_QUERIES:
        sql = render_template(config.load_sql(path), start_date=start, end_date=end)
        try:
            frame = client.query_df(sql)
        except Exception as exc:  # noqa: BLE001 - report and continue discovery
            print(f"{path} failed: {exc}", file=sys.stderr)
            continue
        name = Path(path).stem
        frame.to_csv(out_dir / f"{name}.csv", index=False)
        print(f"{name}: {len(frame)} rows")

    commercial = render_template(
        config.load_sql("discovery/05_commercial_mapping.sql"),
        start_date=start,
        end_date=end,
    )
    for statement in [s for s in commercial.split(";") if s.strip()]:
        try:
            frame = client.query_df(statement)
            frame.to_csv(out_dir / f"commercial_mapping_{len(list(out_dir.glob('commercial_mapping_*')))}.csv", index=False)
        except Exception as exc:  # noqa: BLE001
            print(f"commercial mapping query failed: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
