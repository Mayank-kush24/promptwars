"""
Parse challenge-wide attempt export (CSV/XLSX): Leader Email + Attempts Completed.

Headers match the platform export (spaces / casing normalized).
"""

from __future__ import annotations

import io
from typing import Any

import pandas as pd

_ATTEMPT_HEADER_KEYS = frozenset(
    {
        "attempts completed",
        "attempts_completed",
        "attemptscompleted",
    }
)
_EMAIL_HEADER_KEYS = frozenset(
    {
        "leader email",
        "leader_email",
        "leaderemail",
    }
)


def _norm_header(h: Any) -> str:
    s = str(h).replace("\ufeff", "").strip()
    s = " ".join(s.split())
    return s.lower()


def _find_col(columns: list[str], candidates: frozenset[str]) -> str | None:
    for c in columns:
        k = _norm_header(c)
        if k in candidates:
            return c
    for c in columns:
        k = _norm_header(c).replace(" ", "_")
        if k in candidates:
            return c
    return None


def _headers_from_df(df: Any) -> list[str]:
    return [str(c) for c in df.columns]


def suggest_column_mapping(headers: list[str]) -> dict[str, str | None]:
    """Suggest ``email`` and ``attempts`` source columns (exact header strings) or None."""
    cols = list(headers)
    em = _find_col(cols, _EMAIL_HEADER_KEYS)
    ac = _find_col(cols, _ATTEMPT_HEADER_KEYS)
    if not em:
        for c in cols:
            nk = _norm_header(c)
            if "email" in nk.replace(" ", "") or nk.endswith("mail"):
                em = c
                break
    if not ac:
        for c in cols:
            nk = _norm_header(c)
            if "attempt" in nk and ("complete" in nk or "count" in nk or "done" in nk):
                ac = c
                break
    return {"email": em, "attempts": ac}


def preview_attempts_sheet(raw: bytes, filename: str, *, sample_limit: int = 8) -> dict[str, Any]:
    """
    Return ``headers``, ``sample_rows``, ``suggested_mapping``, ``target_fields``
    for building Email / Attempts column mapping UI.
    """
    fn = (filename or "").strip().lower()
    if not raw:
        raise ValueError("Empty file.")
    buf = io.BytesIO(raw)
    try:
        if fn.endswith(".xlsx"):
            df = pd.read_excel(buf, header=0, engine="openpyxl")
        elif fn.endswith(".csv"):
            df = pd.read_csv(buf, header=0)
        else:
            raise ValueError("Upload a .csv or .xlsx file.")
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"Could not read file: {exc}") from exc
    if df is None or df.empty:
        raise ValueError("No data rows.")
    headers = _headers_from_df(df)
    if not headers:
        raise ValueError("No columns in file.")
    suggested = suggest_column_mapping(headers)
    sample = df.head(sample_limit)
    sample_rows = sample.to_dict(orient="records")
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
        "target_fields": ["email", "attempts"],
    }


def parse_challenge_attempts_sheet(
    raw: bytes,
    filename: str,
    column_mapping: dict[str, str | None] | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """
    Return ``(rows, error)`` where each row is
    ``{"leader_email": str, "attempts_completed": int, "team_name": str|None}``.
    """
    fn = (filename or "").strip().lower()
    if not raw:
        return [], "Empty file."
    buf = io.BytesIO(raw)
    try:
        if fn.endswith(".xlsx"):
            df = pd.read_excel(buf, header=0, engine="openpyxl")
        elif fn.endswith(".csv"):
            df = pd.read_csv(buf, header=0)
        else:
            return [], "Upload a .csv or .xlsx file."
    except Exception as exc:  # noqa: BLE001
        return [], f"Could not read file: {exc}"

    if df is None or df.empty:
        return [], "No data rows."

    cols = [str(c) for c in df.columns]
    mapping = column_mapping or {}
    email_m = (str(mapping.get("email") or "")).strip()
    attempts_m = (str(mapping.get("attempts") or "")).strip()
    use_mapping = bool(email_m or attempts_m)
    if use_mapping:
        if not email_m or not attempts_m:
            return (
                [],
                "When using column mapping, both Email and Attempts columns must be selected.",
            )
        if email_m not in cols:
            return [], f"Mapped email column {email_m!r} not found in file."
        if attempts_m not in cols:
            return [], f"Mapped attempts column {attempts_m!r} not found in file."
        em_col, ac_col = email_m, attempts_m
    else:
        em_col = _find_col(cols, _EMAIL_HEADER_KEYS)
        ac_col = _find_col(cols, _ATTEMPT_HEADER_KEYS)
        if not em_col:
            return [], "Missing required column: Leader Email (add column mapping or rename the header)."
        if not ac_col:
            return [], "Missing required column: Attempts Completed (add column mapping or rename the header)."

    team_col = None
    for c in cols:
        if _norm_header(c) in ("team name", "team_name", "teamname"):
            team_col = c
            break

    out: list[dict[str, Any]] = []
    for _, series in df.iterrows():
        em_raw = series.get(em_col)
        ac_raw = series.get(ac_col)
        if em_raw is None or (isinstance(em_raw, float) and pd.isna(em_raw)):
            continue
        email = str(em_raw).strip()
        if not email or "@" not in email:
            continue
        try:
            n = int(float(str(ac_raw).replace(",", "").strip()))
        except (TypeError, ValueError):
            continue
        if n < 1:
            continue
        team_name = None
        if team_col is not None:
            tr = series.get(team_col)
            if tr is not None and not (isinstance(tr, float) and pd.isna(tr)):
                team_name = str(tr).strip() or None
        out.append(
            {
                "leader_email": email,
                "attempts_completed": n,
                "team_name": team_name,
            }
        )

    if not out:
        return [], "No rows with a valid leader email and attempts completed (>= 1)."
    return out, None
