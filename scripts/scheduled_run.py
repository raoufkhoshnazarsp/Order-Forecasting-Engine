#!/usr/bin/env python
"""Scheduled daily job: forecast, record health, register the model version.

This is what cron runs. It is deliberately the whole chain rather than a
forecast alone, because a daily job that produces a forecast but records
nothing cannot be checked afterwards:

    run_pipeline -> health check -> model registry -> job log

Exit codes are the alert channel: 0 success, 1 the forecast itself failed,
2 the forecast succeeded but the health check is not ok. Cron only needs to
mail on non-zero, so a silent-stale-data day still reaches a human.

Usage:
    python scripts/scheduled_run.py --workbook uploads/data-model.xlsx --horizon 30
    python scripts/scheduled_run.py --workbook ... --reference 2026-11-16
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.monitoring.health import health_report, record_job_run  # noqa: E402
from src.monitoring.registry import record_run_version  # noqa: E402
from src.pipeline import run_pipeline  # noqa: E402


def _parse_date(raw: str | None) -> date | None:
    return date.fromisoformat(raw) if raw else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workbook", required=True)
    parser.add_argument("--horizon", type=int, default=30)
    parser.add_argument("--model", default="auto")
    parser.add_argument("--reference", default=None, help="date to judge data freshness against")
    parser.add_argument("--out", default="artifacts/scheduled")
    parser.add_argument("--no-registry", action="store_true")
    args = parser.parse_args(argv)

    started = datetime.now()
    reference = _parse_date(args.reference)

    try:
        result = run_pipeline(
            args.workbook, horizon=args.horizon, model=args.model
        )
    except Exception as exc:  # noqa: BLE001 - the job must report, not crash opaquely
        record_job_run(
            {
                "status": "failed",
                "workbook": Path(args.workbook).name,
                "error": str(exc)[:500],
            }
        )
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 1

    summary = result.summary()
    selected = summary["backtest_ranking"][0] if summary["backtest_ranking"] else {}
    health = health_report(
        result.mart,
        backtest_frame=result.backtest_rows,
        # Wall clock by default: the point of the check is that the workbook
        # is not older than today. Passing --reference overrides it for a
        # backfill, where "today" is the day being reconstructed.
        reference=reference,
    ).as_dict()

    out_dir = Path(args.out) / summary["run_id"]
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1, default=str), encoding="utf-8"
    )
    if result.forecast is not None:
        result.forecast.to_parquet(out_dir / "forecast.parquet", index=False)
    # The mart and the selected backtest rows are what the health endpoint
    # needs later; saving them here means /api/health is a read, not a rerun.
    if result.mart is not None:
        result.mart.to_parquet(out_dir / "mart.parquet", index=False)
    if result.backtest_rows is not None:
        result.backtest_rows.to_parquet(out_dir / "backtest.parquet", index=False)
    (out_dir / "health.json").write_text(
        json.dumps(health, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    registry_note = ""
    if not args.no_registry:
        try:
            entry = record_run_version(
                model_name=summary["model"],
                backend=summary.get("global_model", {}).get("backend", "baseline"),
                run_id=summary["run_id"],
                metrics={"wape": selected.get("wape"), "bias": selected.get("bias")},
                note=f"run {summary['run_id']}",
            )
            registry_note = entry["version_id"]
        except Exception as exc:  # noqa: BLE001 - registry failure must not lose the forecast
            registry_note = f"registry failed: {exc}"

    ok = health["status"] != "error"
    record_job_run(
        {
            "status": "success" if ok else "unhealthy",
            "workbook": Path(args.workbook).name,
            "run_id": summary["run_id"],
            "model": summary["model"],
            "version_id": registry_note,
            "health": health["status"],
            "duration_s": round((datetime.now() - started).total_seconds(), 1),
        }
    )

    print(
        json.dumps(
            {
                "status": health["status"],
                "run_id": summary["run_id"],
                "model": summary["model"],
                "version_id": registry_note,
                "output": str(out_dir),
                "alerts": [a["code"] for a in health["alerts"]],
            },
            ensure_ascii=False,
            indent=1,
        )
    )
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
