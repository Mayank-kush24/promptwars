"""
Audit maintenance helpers.

Run as a CLI:
    python -m audit.maintenance --ensure-partitions 3
    python -m audit.maintenance --drop-older-than 730d
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import text

from audit.db import create_engine

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


_INTERVAL_RE = re.compile(r"^\s*(\d+)\s*([a-zA-Z]+)?\s*$")


def _parse_interval(s: str) -> str:
    """Accept '730d', '12 months', '90d', 'INTERVAL ...'  -> a Postgres interval string."""
    if s.lower().startswith("interval"):
        return s
    m = _INTERVAL_RE.match(s)
    if not m:
        raise ValueError(f"cannot parse retention interval: {s!r}")
    n, unit = m.group(1), (m.group(2) or "days").lower()
    aliases = {
        "d": "days", "day": "days", "days": "days",
        "w": "weeks", "wk": "weeks", "week": "weeks", "weeks": "weeks",
        "m": "months", "mo": "months", "month": "months", "months": "months",
        "y": "years", "yr": "years", "year": "years", "years": "years",
        "h": "hours", "hr": "hours", "hour": "hours", "hours": "hours",
    }
    canon = aliases.get(unit)
    if canon is None:
        raise ValueError(f"unknown interval unit: {unit!r}")
    return f"{int(n)} {canon}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit maintenance.")
    parser.add_argument("--ensure-partitions", type=int, metavar="MONTHS_AHEAD", default=None,
                        help="Create monthly partitions for the next N months.")
    parser.add_argument("--drop-older-than", type=str, metavar="INTERVAL", default=None,
                        help="Drop audit partitions older than this interval (e.g. '730d', '12 months').")
    args = parser.parse_args()

    if args.ensure_partitions is None and args.drop_older_than is None:
        parser.print_help()
        return 2

    db_url = os.environ.get("DATABASE_URL", "").strip()
    if not db_url:
        print("ERROR: DATABASE_URL is not set", file=sys.stderr)
        return 1

    engine = create_engine(db_url, future=True)
    rc = 0
    with engine.begin() as conn:
        if args.ensure_partitions is not None:
            n = max(0, int(args.ensure_partitions))
            conn.execute(text("SELECT audit.ensure_partitions(:n)"), {"n": n})
            print(f"audit.ensure_partitions({n}) done")
        if args.drop_older_than is not None:
            interval_sql = _parse_interval(args.drop_older_than)
            dropped = conn.execute(
                text("SELECT audit.drop_partitions_older_than(CAST(:i AS INTERVAL))"),
                {"i": interval_sql},
            ).scalar()
            print(f"audit.drop_partitions_older_than('{interval_sql}') -> {dropped} partition(s) dropped")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
