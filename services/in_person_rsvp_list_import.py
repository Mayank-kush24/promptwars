"""
Parse CSV/XLSX for in-person PW session RSVP email lists (invite sent vs accepted).

No Flask imports. Used by preview + commit import routes.
"""

from __future__ import annotations

import io
from typing import Any

import pandas as pd

LIST_KIND_INVITE_SENT = "invite_sent"
LIST_KIND_ACCEPTED = "accepted"
LIST_KINDS = frozenset({LIST_KIND_INVITE_SENT, LIST_KIND_ACCEPTED})

_TARGET_FIELDS: tuple[dict[str, Any], ...] = (
    {"key": "email", "label": "Email", "required": True},
    {"key": "display_name", "label": "Display name", "required": False},
    {"key": "source_timestamp", "label": "Timestamp", "required": False},
)

_EMAIL_HEADER_CANDIDATES = frozenset(
    {
        "email",
        "e-mail",
        "e mail",
        "mail",
        "registered email",
        "registered_email",
        "registeredemail",
        "user email",
        "user_email",
        "useremail",
        "leader email",
        "leader_email",
        "leaderemail",
        "email address",
        "email_address",
        "contact email",
        "contact_email",
        "work email",
        "work_email",
        "primary email",
        "primary_email",
        "participant email",
        "participant_email",
    }
)

_NAME_HEADER_CANDIDATES = frozenset(
    {
        "name",
        "full name",
        "fullname",
        "full_name",
        "display name",
        "display_name",
        "displayname",
        "participant name",
        "participant_name",
        "user name",
        "user_name",
        "username",
        "first name",
        "firstname",
    }
)

_TS_HEADER_CANDIDATES = frozenset(
    {
        "timestamp",
        "time",
        "date",
        "created at",
        "created_at",
        "createdat",
        "rsvped at",
        "rsvped_at",
        "submitted at",
        "submitted_at",
        "registered at",
        "registered_at",
    }
)

def _looks_like_email(s: str) -> bool:
    if "@" not in s or s.startswith("@") or s.endswith("@"):
        return False
    local, _, domain = s.partition("@")
    if not local or not domain or " " in s:
        return False
    return "." in domain and len(domain) >= 3


def norm_header(label: Any) -> str:
    s = str(label).replace("\ufeff", "").strip()
    s = " ".join(s.split())
    return s.lower()


def _headers_from_df(df: pd.DataFrame) -> list[str]:
    return [str(c) for c in df.columns]


def _find_header(columns: list[str], candidates: frozenset[str]) -> str | None:
    for c in columns:
        k = norm_header(c)
        if k in candidates:
            return c
        k2 = k.replace(" ", "_")
        if k2 in candidates:
            return c
    for c in columns:
        k = norm_header(c)
        for cand in candidates:
            if cand in k or k in cand:
                if "email" in cand and "email" not in k:
                    continue
                return c
    return None


def suggest_column_mapping(headers: list[str]) -> dict[str, str | None]:
    """Map target field key -> source column name (exact header string) or None."""
    email_col = _find_header(headers, _EMAIL_HEADER_CANDIDATES)
    if not email_col:
        for c in headers:
            nk = norm_header(c)
            if "email" in nk or nk.endswith("@") or "mail" == nk:
                email_col = c
                break
    name_col = _find_header(headers, _NAME_HEADER_CANDIDATES)
    ts_col = _find_header(headers, _TS_HEADER_CANDIDATES)
    return {
        "email": email_col,
        "display_name": name_col,
        "source_timestamp": ts_col,
    }


def _read_dataframe(raw: bytes, filename: str) -> pd.DataFrame:
    fn = (filename or "").lower()
    buf = io.BytesIO(raw)
    if not raw:
        raise ValueError("File is empty")
    if fn.endswith(".xlsx") or fn.endswith(".xls"):
        return pd.read_excel(buf, header=0, engine="openpyxl")
    return pd.read_csv(buf, header=0)


def preview_file(raw: bytes, filename: str, *, sample_limit: int = 8) -> dict[str, Any]:
    """
    Return ``headers``, ``sample_rows`` (list of dicts keyed by original header),
    ``suggested_mapping``, ``target_fields``.
    """
    df = _read_dataframe(raw, filename)
    if df is None or df.empty:
        raise ValueError("No data rows in file")
    headers = _headers_from_df(df)
    if not headers:
        raise ValueError("No columns in file")
    suggested = suggest_column_mapping(headers)
    sample = df.head(sample_limit)
    sample_rows = sample.to_dict(orient="records")
    # JSON-serialize friendly: convert Timestamp etc.
    for row in sample_rows:
        for k, v in list(row.items()):
            if hasattr(v, "isoformat"):
                row[k] = v.isoformat()
            elif pd.isna(v):
                row[k] = None
            else:
                row[k] = v
    return {
        "headers": headers,
        "sample_rows": sample_rows,
        "suggested_mapping": suggested,
        "target_fields": list(_TARGET_FIELDS),
    }


def _norm_email(value: Any) -> str | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    s = str(value).strip().lower()
    if not s or s in ("nan", "none"):
        return None
    if _looks_like_email(s):
        return s
    return None


def parse_emails_with_mapping(
    raw: bytes,
    filename: str,
    column_mapping: dict[str, str | None],
) -> tuple[list[str], dict[str, int]]:
    """
    Apply user mapping; return (deduped normalized emails in file order, stats).

    ``column_mapping`` maps target keys to exact source header strings; unmapped
    targets may be absent or map to empty/ignored.
    """
    df = _read_dataframe(raw, filename)
    if df is None or df.empty:
        raise ValueError("No data rows in file")
    headers = _headers_from_df(df)
    em_header = (column_mapping.get("email") or "").strip()
    if not em_header:
        raise ValueError("Column mapping must include email")
    if em_header not in headers:
        raise ValueError(f"Mapped email column {em_header!r} not found in file")

    rows_read = len(df)
    blank = 0
    invalid = 0
    seen: dict[str, None] = {}
    for _, series in df.iterrows():
        raw_v = series.get(em_header)
        if raw_v is None or (isinstance(raw_v, float) and pd.isna(raw_v)) or not str(raw_v).strip():
            blank += 1
            continue
        em = _norm_email(raw_v)
        if em is None:
            invalid += 1
            continue
        seen[em] = None
    emails = list(seen.keys())
    stats = {
        "rows_read": rows_read,
        "rows_blank_email": blank,
        "rows_invalid_email": invalid,
        "rows_after_dedupe": len(emails),
    }
    return emails, stats
