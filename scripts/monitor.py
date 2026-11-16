#!/usr/bin/env python
"""Monitoring CLI: health, model registry, promote, rollback.

    python scripts/monitor.py health [--workbook uploads/data-model.xlsx] [--reference YYYY-MM-DD]
    python scripts/monitor.py registry
    python scripts/monitor.py promote [--version ID]
    python scripts/monitor.py rollback
    python scripts/monitor.py job
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.monitoring.health import health_report, job_health  # noqa: E402
from src.monitoring.registry import (  # noqa: E402
    RegistryError,
    current_version,
    history,
    promote,
    rollback,
)


def _print(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=1, default=str))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    health = sub.add_parser("health", help="data freshness, model drift and job state")
    health.add_argument("--workbook", default=None)
    health.add_argument("--horizon", type=int, default=30)
    health.add_argument("--reference", default=None)
    health.add_argument("--prometheus", action="store_true")

    sub.add_parser("registry", help="list model versions")
    promote_cmd = sub.add_parser("promote", help="make a version active")
    promote_cmd.add_argument("--version", default=None)
    sub.add_parser("rollback", help="return to the previous active version")
    sub.add_parser("job", help="age of the last successful scheduled run")

    args = parser.parse_args(argv)

    if args.command == "health":
        mart = None
        backtest = None
        if args.workbook:
            from src.pipeline import run_pipeline

            result = run_pipeline(args.workbook, horizon=args.horizon)
            mart = result.mart
            backtest = result.backtest_rows
        report = health_report(
            mart,
            backtest_frame=backtest,
            reference=date.fromisoformat(args.reference) if args.reference else None,
        )
        if args.prometheus:
            from src.monitoring.health import render_prometheus

            print(render_prometheus(report), end="")
        else:
            _print(report.as_dict())
        return 0 if report.status != "error" else 2

    if args.command == "registry":
        _print({"current": current_version(), "versions": history()})
        return 0

    if args.command == "promote":
        try:
            version = promote(args.version)
        except RegistryError as exc:
            print(f"promote failed: {exc}", file=sys.stderr)
            return 1
        _print({"promoted": version.as_dict()})
        return 0

    if args.command == "rollback":
        try:
            version = rollback()
        except RegistryError as exc:
            print(f"rollback failed: {exc}", file=sys.stderr)
            return 1
        _print({"active": version.as_dict()})
        return 0

    if args.command == "job":
        _print(job_health())
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
