"""Install (or print) the daily cron entry for the forecast job.

Kept as a script rather than a hand-edited crontab so the schedule is
reviewable in git and reproducible on a new server.

    python scripts/install_cron.py --workbook /opt/order-forecasting-engine/uploads/data-model.xlsx
    python scripts/install_cron.py --dry-run
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

MARKER = "# bnpl-forecast-daily"

DEFAULT_WORKBOOK = "/opt/order-forecasting-engine/uploads/data-model.xlsx"
DEFAULT_LOG = "/opt/order-forecasting-engine/artifacts/scheduled/cron.log"


def build_line(
    *, workbook: str, hour: int, minute: int, log: str, project_dir: str
) -> str:
    command = (
        f"cd {project_dir} && "
        f"docker compose exec -T forecast-studio "
        f"python scripts/scheduled_run.py --workbook {workbook} "
        f">> {log} 2>&1"
    )
    return f"{minute} {hour} * * * {command} {MARKER}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workbook", default=DEFAULT_WORKBOOK)
    parser.add_argument("--project-dir", default="/opt/order-forecasting-engine")
    parser.add_argument("--log", default=DEFAULT_LOG)
    parser.add_argument("--hour", type=int, default=6, help="UTC hour (06:00 UTC = 09:30 Tehran)")
    parser.add_argument("--minute", type=int, default=30)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    line = build_line(
        workbook=args.workbook,
        hour=args.hour,
        minute=args.minute,
        log=args.log,
        project_dir=args.project_dir,
    )

    if args.dry_run:
        print(line)
        return 0

    try:
        current = subprocess.run(
            ["crontab", "-l"], capture_output=True, text=True, check=False
        )
    except FileNotFoundError:
        print("crontab is not installed on this host", file=sys.stderr)
        return 1
    existing = [
        row
        for row in (current.stdout or "").splitlines()
        if MARKER not in row and row.strip()
    ]
    existing.append(line)
    payload = "\n".join(existing) + "\n"

    install = subprocess.run(
        ["crontab", "-"], input=payload, text=True, capture_output=True, check=False
    )
    if install.returncode != 0:
        print(f"crontab install failed: {install.stderr}", file=sys.stderr)
        return 1
    Path(args.log).parent.mkdir(parents=True, exist_ok=True)
    print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
