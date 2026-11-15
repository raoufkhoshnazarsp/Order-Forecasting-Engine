#!/usr/bin/env python
"""Phase 1 — extract daily leaf orders into a snapshot artifact.

Usage:
    python scripts/extract_history.py --start 2024-01-01 --end 2025-01-01 \
        [--monthly] [--snapshot-id snap_001] [--allow-draft]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import config
from src.ingestion.clickhouse_client import ClickHouseUnavailable, build_client
from src.ingestion.extract import (
    DAILY_LEAF_ORDERS,
    build_query,
    build_snapshot_metadata,
    measure_unmapped,
    normalize_orders_frame,
    split_leaves,
)
from src.validation.target import TargetDefinition, TargetDefinitionError


def month_chunks(start: date, end: date) -> list[tuple[date, date]]:
    chunks, cursor = [], start
    while cursor < end:
        nxt = date(cursor.year + (cursor.month // 12), cursor.month % 12 + 1, 1)
        chunk_end = min(nxt, end)
        chunks.append((cursor, chunk_end))
        cursor = chunk_end
    return chunks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--monthly", action="store_true", help="extract one month at a time")
    parser.add_argument("--snapshot-id", default=None)
    parser.add_argument("--out", default="artifacts/extracts")
    parser.add_argument("--allow-draft", action="store_true", help="permit draft target (dev only)")
    args = parser.parse_args(argv)

    target = TargetDefinition.from_config(config.load_yaml("target.yaml"))
    if not args.allow_draft:
        try:
            target.assert_production_ready()
        except TargetDefinitionError as exc:
            print(f"Blocked: {exc}", file=sys.stderr)
            return 3

    try:
        client = build_client()
    except ClickHouseUnavailable as exc:
        print(f"ClickHouse unavailable: {exc}", file=sys.stderr)
        return 2

    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    snapshot_id = args.snapshot_id or datetime.now().strftime("snap_%Y%m%d_%H%M%S")
    out_dir = Path(args.out) / snapshot_id
    out_dir.mkdir(parents=True, exist_ok=True)

    hierarchy = config.load_yaml("hierarchy.yaml")
    verticals = config.load_yaml("target.yaml")["scope"]["verticals"]
    ranges = month_chunks(start, end) if args.monthly else [(start, end)]

    import pandas as pd

    frames = []
    for chunk_start, chunk_end in ranges:
        sql = build_query(
            DAILY_LEAF_ORDERS,
            target,
            start_date=chunk_start,
            end_date=chunk_end,
            verticals=verticals,
            venture_vertical_value=hierarchy["resolution"]["venture_vertical_value"],
            venture_category_value=hierarchy["resolution"]["venture_category_value"],
        )
        chunk = normalize_orders_frame(
            client.query_df(sql), hierarchy.get("vertical_entity_map")
        )
        chunk["chunk_start"] = chunk_start.isoformat()
        frames.append(chunk)
        print(f"{chunk_start} -> {chunk_end}: {len(chunk)} rows")

    full = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    leaves, unmapped = split_leaves(full)
    unmapped_stats = measure_unmapped(full)

    leaves.to_parquet(out_dir / "daily_leaf_orders.parquet", index=False)
    unmapped.to_csv(out_dir / "unmapped_orders.csv", index=False)
    source_max = leaves["date"].max() if not leaves.empty else None
    meta = build_snapshot_metadata(
        snapshot_id=snapshot_id,
        start_date=start,
        end_date=end,
        source_max_date=source_max,
        query_version=DAILY_LEAF_ORDERS.version,
        record_count=len(full),
        validation_status="PASSED" if unmapped_stats["unmapped_orders"] == 0 else "WARN_UNMAPPED",
    )
    meta.update(unmapped_stats)
    (out_dir / "snapshot.json").write_text(json.dumps(meta, indent=2, default=str))
    print(json.dumps(meta, indent=2, default=str))

    if unmapped_stats["unmapped_orders"] > 0:
        print(
            f"WARNING: {unmapped_stats['unmapped_orders']} unmapped orders - "
            "reported, not dropped.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
