"""
Parse Main Data Center exports (CSV or XLSX) for In-person PromptWars.

Headers are matched case-insensitively with normalized whitespace.
Email is required; rows without a usable email are skipped.
Duplicate emails in the file keep the last occurrence.

Audit note:
    This module is invoked by Flask routes which already run inside the
    audit-instrumented engine, so DB writes performed by the caller are
    captured automatically (HTTP_REQUEST + SQL_EXEC + audit_data_changes).

    If you ever turn this into a standalone CLI, enable audit at the top:

        from audit.db import create_engine
        from audit import install_for_script
        engine = create_engine(os.environ["DATABASE_URL"], future=True)
        install_for_script(engine, principal="system:etl_data_center", source="etl")
"""

from __future__ import annotations

import re
from datetime import datetime
from io import BytesIO
from typing import Any, BinaryIO

import pandas as pd

# Trailing "( 2 )" / "(2)" on designation = years of experience (not inner parens like "(Co-founder)").
_DESIGNATION_TRAILING_YEARS_RE = re.compile(r"\s*\(\s*(\d+)\s*\)\s*$")


def split_designation_with_years(raw: Any) -> tuple[str | None, int | None]:
    """Return ``(designation_text, years)`` with trailing ``( N )`` stripped into ``years``."""
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None, None
    s = str(raw).strip()
    if not s:
        return None, None
    m = _DESIGNATION_TRAILING_YEARS_RE.search(s)
    if not m:
        return s, None
    years = int(m.group(1))
    clean = s[: m.start()].rstrip()
    return (clean or None), years


def _normalize_form_timestamp_for_db(val: Any) -> datetime | None:
    """Return a timezone-aware UTC datetime suitable for TIMESTAMPTZ.

    Exports often include an explicit offset (e.g. ``...+05:30``); those are
    parsed as absolute instants. Naive spreadsheet datetimes are treated as
    **Asia/Kolkata** wall time so PostgreSQL does not reinterpret them using
    the server or client session timezone (which would skew IST hour charts).
    """
    if val is None:
        return None
    if isinstance(val, float) and pd.isna(val):
        return None
    ts = pd.Timestamp(val)
    if pd.isna(ts):
        return None
    if ts.tzinfo is None:
        ts = ts.tz_localize("Asia/Kolkata", ambiguous=True, nonexistent="shift_forward")
    else:
        ts = ts.tz_convert("UTC")
    return ts.to_pydatetime()

# Normalized header (lower, collapsed whitespace) -> internal column name
HEADER_MAP: dict[str, str] = {
    "timestamp": "form_timestamp",
    "utm source": "utm_source",
    "utm medium": "utm_medium",
    "utm campaign": "utm_campaign",
    "utm term": "utm_term",
    "utm content": "utm_content",
    "college/school/company/startup name": "org_name",
    "college/school state": "org_state",
    "college/school city": "org_city",
    "class/stream": "class_stream",
    "portfolio": "portfolio",
    "domain": "domain",
    "designation (year of exp.)": "designation",
    "founded in (startup size)": "founded_info",
    "degree (passout year)": "degree",
    "profile name": "profile_name",
    "full name": "full_name",
    "email": "email",
    "mobile number": "mobile",
    "whatsapp": "whatsapp",
    "country": "country",
    "state": "state",
    "city": "city",
    "date of birth": "dob",
    "gender": "gender",
    "occupation": "occupation",
    "github url": "github_url",
    "linkedin url": "linkedin_url",
    "in which city, would you like to attend the in-person promptwars promptathon?": "attendance_city",
}

_TEXT_COLS = (
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "org_name",
    "org_state",
    "org_city",
    "class_stream",
    "portfolio",
    "domain",
    "designation",
    "founded_info",
    "degree",
    "profile_name",
    "full_name",
    "mobile",
    "whatsapp",
    "country",
    "state",
    "city",
    "gender",
    "occupation",
    "github_url",
    "linkedin_url",
    "attendance_city",
)


def _normalize_header(label: str) -> str:
    s = str(label).replace("\ufeff", "").strip()
    s = " ".join(s.split())
    return s.lower()


def _blank_to_none(val: Any) -> Any:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    if isinstance(val, str) and not val.strip():
        return None
    return val


def parse_main_data_center_file(fileobj: BinaryIO, filename: str) -> tuple[list[dict[str, Any]], dict[str, int]]:
    name = (filename or "").lower()
    raw = fileobj.read()
    if not raw:
        raise ValueError("File is empty")

    if name.endswith(".xlsx") or name.endswith(".xls"):
        df = pd.read_excel(BytesIO(raw), engine="openpyxl")
    else:
        df = pd.read_csv(BytesIO(raw))

    if df.empty:
        raise ValueError("No data rows in file")

    mapped: dict[str, pd.Series] = {}
    for col in df.columns:
        key = HEADER_MAP.get(_normalize_header(col))
        if key:
            mapped[key] = df[col]

    if "email" not in mapped:
        raise ValueError("Missing required column: Email")

    rows_read = len(df)
    out = pd.DataFrame(mapped)
    for c in _TEXT_COLS:
        if c not in out.columns:
            out[c] = None

    out["email"] = out["email"].map(lambda x: str(x).strip() if _blank_to_none(x) is not None else "")
    out = out[out["email"] != ""]
    skipped_no_email = rows_read - len(out)
    with_email = len(out)

    if "form_timestamp" not in out.columns:
        out["form_timestamp"] = pd.NaT
    else:
        out["form_timestamp"] = pd.to_datetime(out["form_timestamp"], errors="coerce")

    if "dob" not in out.columns:
        out["dob"] = pd.NaT
    else:
        out["dob"] = pd.to_datetime(out["dob"], errors="coerce")

    for c in _TEXT_COLS:
        out[c] = out[c].apply(lambda v: None if _blank_to_none(v) is None else str(v).strip())

    out = out.drop_duplicates(subset=["email"], keep="last")
    duplicate_emails_collapsed = with_email - len(out)

    rows: list[dict[str, Any]] = []
    for _, s in out.iterrows():
        fts = s.get("form_timestamp")
        dob = s.get("dob")
        des_t, des_y = split_designation_with_years(s.get("designation"))
        row: dict[str, Any] = {
            "email": str(s["email"]).strip(),
            "form_timestamp": _normalize_form_timestamp_for_db(fts),
            "utm_source": s.get("utm_source"),
            "utm_medium": s.get("utm_medium"),
            "utm_campaign": s.get("utm_campaign"),
            "utm_term": s.get("utm_term"),
            "utm_content": s.get("utm_content"),
            "org_name": s.get("org_name"),
            "org_state": s.get("org_state"),
            "org_city": s.get("org_city"),
            "class_stream": s.get("class_stream"),
            "portfolio": s.get("portfolio"),
            "domain": s.get("domain"),
            "designation": des_t,
            "designation_years_experience": des_y,
            "founded_info": s.get("founded_info"),
            "degree": s.get("degree"),
            "profile_name": s.get("profile_name"),
            "full_name": s.get("full_name"),
            "mobile": s.get("mobile"),
            "whatsapp": s.get("whatsapp"),
            "country": s.get("country"),
            "state": s.get("state"),
            "city": s.get("city"),
            "dob": None if pd.isna(dob) else pd.Timestamp(dob).date(),
            "gender": s.get("gender"),
            "occupation": s.get("occupation"),
            "github_url": s.get("github_url"),
            "linkedin_url": s.get("linkedin_url"),
            "attendance_city": s.get("attendance_city"),
        }
        rows.append(row)

    stats = {
        "rows_read": int(rows_read),
        "rows_skipped_no_email": int(skipped_no_email),
        "rows_after_dedupe": int(len(out)),
        "duplicate_emails_collapsed": int(duplicate_emails_collapsed),
    }
    return rows, stats
