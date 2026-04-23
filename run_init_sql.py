"""
Apply database SQL files to PostgreSQL using DATABASE_URL from the environment.

By default applies, in order:
  1. database/init.sql   (core schema)
  2. database/audit.sql  (Master Audit Log: schema, triggers, event trigger)

Usage:
  python run_init_sql.py
  python run_init_sql.py --sql database/init.sql --sql database/audit.sql
  python run_init_sql.py --sql some/other.sql

Requires: psycopg2-binary (see requirements.txt)
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")


def _strip_full_line_comments(sql: str) -> str:
    lines = []
    for line in sql.splitlines():
        if line.lstrip().startswith("--"):
            continue
        lines.append(line)
    return "\n".join(lines)


_DOLLAR_TAG_RE = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$")


def _split_sql_statements(sql: str) -> list[str]:
    """
    Split SQL on top-level semicolons while respecting:
      - single-quoted string literals (with '' escapes),
      - dollar-quoted blocks ($tag$ ... $tag$, $$ ... $$).

    This is required for audit.sql which contains CREATE FUNCTION ... $fn$ ... $fn$
    and DO $bootstrap$ ... $bootstrap$ blocks with inline semicolons.
    """
    cleaned = _strip_full_line_comments(sql)
    out: list[str] = []
    buf: list[str] = []
    i = 0
    n = len(cleaned)
    in_dollar: str | None = None
    in_single = False

    while i < n:
        ch = cleaned[i]
        if in_dollar is not None:
            if cleaned.startswith(in_dollar, i):
                buf.append(in_dollar)
                i += len(in_dollar)
                in_dollar = None
                continue
            buf.append(ch)
            i += 1
            continue
        if in_single:
            buf.append(ch)
            if ch == "'":
                if i + 1 < n and cleaned[i + 1] == "'":
                    buf.append("'")
                    i += 2
                    continue
                in_single = False
            i += 1
            continue
        if ch == "'":
            in_single = True
            buf.append(ch)
            i += 1
            continue
        if ch == "$":
            m = _DOLLAR_TAG_RE.match(cleaned, i)
            if m:
                tag = m.group(0)
                buf.append(tag)
                in_dollar = tag
                i += len(tag)
                continue
        if ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                out.append(stmt)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1

    last = "".join(buf).strip()
    if last:
        out.append(last)
    return out


def apply_sql_file(cur, sql_path: Path) -> int:
    raw = sql_path.read_text(encoding="utf-8")
    statements = _split_sql_statements(raw)
    if not statements:
        print(f"WARN: No SQL statements parsed from {sql_path}", file=sys.stderr)
        return 0
    print(f"Executing {len(statements)} statement(s) from {sql_path} …")
    for i, stmt in enumerate(statements, start=1):
        preview = stmt.replace("\n", " ")[:120]
        if len(stmt) > 120:
            preview += "…"
        print(f"  [{i}/{len(statements)}] {preview}")
        try:
            cur.execute(stmt)
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR on statement {i} of {sql_path}: {exc}", file=sys.stderr)
            print(stmt[:2000], file=sys.stderr)
            raise
    return len(statements)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run init/audit SQL against PostgreSQL.")
    parser.add_argument(
        "--sql",
        type=Path,
        action="append",
        default=None,
        help=(
            "Path to a SQL file (may be repeated). "
            "Default: database/init.sql then database/audit.sql"
        ),
    )
    args = parser.parse_args()

    if args.sql:
        sql_files: list[Path] = list(args.sql)
    else:
        sql_files = [ROOT / "database" / "init.sql", ROOT / "database" / "audit.sql"]

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
