"""
Run one or more .sql files against PostgreSQL using DATABASE_URL (from .env).

Uses the same statement splitter as run_init_sql.py (dollar-quoted blocks, etc.).

Usage:
  python run_sql_file.py database/migrate_mdc_split.sql
  python run_sql_file.py a.sql b.sql

Requires: psycopg2-binary (see requirements.txt)
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent


def main() -> int:
    load_dotenv(ROOT / ".env")
    from run_init_sql import apply_sql_file
    parser = argparse.ArgumentParser(
        description="Execute SQL file(s) against the database in DATABASE_URL."
    )
    parser.add_argument(
        "sql_files",
        nargs="+",
        type=Path,
        help="Path(s) to .sql files (relative or absolute).",
    )
    args = parser.parse_args()

    sql_files = [p.resolve() for p in args.sql_files]
    for sql_path in sql_files:
        if not sql_path.is_file():
            print(f"ERROR: SQL file not found: {sql_path}", file=sys.stderr)
            return 1

    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        print("ERROR: DATABASE_URL is not set (use .env next to this script).", file=sys.stderr)
        return 1

    try:
        import psycopg2
    except ImportError:
        print("ERROR: psycopg2 is not installed. Run: pip install -r requirements.txt", file=sys.stderr)
        return 1

    print("Connecting to database…")
    conn = psycopg2.connect(database_url)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            for sql_path in sql_files:
                try:
                    apply_sql_file(cur, sql_path)
                except Exception:
                    return 1
    finally:
        conn.close()

    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
