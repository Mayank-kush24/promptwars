"""
Parse multi-sheet XLSX workbooks for Virtual Prompt Wars challenge submissions.

Sheet tabs must match (case-insensitive) ``Submission <suffix>`` after stripping
an optional leading enumerator like ``1.`` or ``2)`` — the number is never used
to resolve ``challenges.id``. The suffix is matched to each challenge's
``import_sheet_suffix`` (if set) or ``title`` for the virtual event.
"""

from __future__ import annotations

import re
from io import BytesIO
from typing import Any, BinaryIO

import pandas as pd

# Normalized Excel header -> row dict key (DB column base without event/import ids)
HEADER_MAP: dict[str, str] = {
    "team name": "team_name",
    "leader name": "leader_name",
    "leader email": "leader_email",
    "leader phone": "leader_phone",
    "team size": "team_size",
    "problem statements": "problem_statements",
    "total score (latest attempt)": "total_score",
    "deployed link - (cloud run url)": "deployed_link",
    "deployed link (cloud run url)": "deployed_link",
    "linkedin post": "linkedin_post",
    "public github repository link": "github_repository_link",
    "created at": "export_created_at",
    "created by name": "export_created_by_name",
    "created by email": "export_created_by_email",
    "updated at": "export_updated_at",
    "updated by name": "export_updated_by_name",
    "updated by email": "export_updated_by_email",
}

_TEXT_KEYS = (
    "leader_name",
    "leader_phone",
    "problem_statements",
    "deployed_link",
    "linkedin_post",
    "github_repository_link",
    "export_created_by_name",
    "export_created_by_email",
    "export_updated_by_name",
    "export_updated_by_email",
)

_LEADING_ENUM_RE = re.compile(r"^\s*\d+[\.)]\s*")
_SUBMISSION_PREFIX_RE = re.compile(r"(?i)^submission\s+(.+)$")


def normalize_key(s: str) -> str:
    return " ".join(str(s).strip().lower().split())


def _normalize_header(label: str) -> str:
    s = str(label).replace("\ufeff", "").strip()
    s = " ".join(s.split())
    return s.lower()


def strip_leading_sheet_enumerator(sheet_name: str) -> str:
    """Remove optional ``1.`` / ``2)`` style prefixes; discard digits (never used as challenge id)."""
    s = str(sheet_name).strip()
    s = " ".join(s.split())
    return _LEADING_ENUM_RE.sub("", s).strip()


def submission_sheet_suffix(sheet_name: str) -> str | None:
    """
    If the tab is a submission sheet, return the normalized suffix after ``Submission ``.
    Otherwise return None.
    """
    rest = strip_leading_sheet_enumerator(sheet_name)
    rest = " ".join(rest.split())
    m = _SUBMISSION_PREFIX_RE.match(rest)
    if not m:
        return None
    return normalize_key(m.group(1))


def build_challenge_match_map(challenges: list[dict]) -> dict[str, int]:
    """
    Map normalized suffix string -> challenge id.
    ``import_sheet_suffix`` wins over ``title`` when keys collide (suffix registered first).
    """
    key_to_id: dict[str, int] = {}
    for ch in challenges:
        cid = int(ch["id"])
        suff = (ch.get("import_sheet_suffix") or "").strip()
        if not suff:
            continue
        k = normalize_key(suff)
        if k in key_to_id and key_to_id[k] != cid:
            raise ValueError(
                f"Two challenges share import_sheet_suffix matching {k!r}; fix admin data."
            )
        key_to_id[k] = cid

    for ch in challenges:
        cid = int(ch["id"])
        if (ch.get("import_sheet_suffix") or "").strip():
            continue
        k = normalize_key(ch["title"])
        if k not in key_to_id:
            key_to_id[k] = cid
        elif key_to_id[k] != cid:
            raise ValueError(
                f"Challenge title {ch['title']!r} normalizes to {k!r}, which is already used by "
                "another challenge's import_sheet_suffix. Set a distinct import_sheet_suffix on one of them."
            )
    return key_to_id


def map_sheets_to_challenges(
    sheet_names: list[str],
    challenges: list[dict],
) -> dict[str, int]:
    """
    Raw Excel sheet name -> challenge_id.
    Raises ValueError with a clear message if any sheet is invalid or unmatched.
    """
    match_map = build_challenge_match_map(challenges)
    out: dict[str, int] = {}
    errors: list[str] = []

    for raw in sheet_names:
        suf = submission_sheet_suffix(raw)
        if suf is None:
            errors.append(
                f"{raw!r}: expected tab name like 'Submission <label>' "
                "(optional leading '1.' / '2)' is ignored, not used as challenge id)."
            )
            continue
        cid = match_map.get(suf)
        if cid is None:
            known = sorted(set(match_map.keys()))
            errors.append(
                f"{raw!r}: suffix {suf!r} does not match any challenge title or import_sheet_suffix. "
                f"Known keys: {known!r}"
            )
            continue
        out[raw] = int(cid)

    if errors:
        raise ValueError("Workbook sheet / challenge mapping failed:\n- " + "\n- ".join(errors))
    if not out:
        raise ValueError("No submission sheets found in workbook.")
    return out


def _blank_to_none(val: Any) -> Any:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    if isinstance(val, str) and not val.strip():
        return None
    return val


def _row_to_payload(
    series: pd.Series,
    *,
    challenge_id: int,
    source_sheet_name: str,
) -> dict[str, Any]:
    d: dict[str, Any] = {
        "challenge_id": challenge_id,
        "source_sheet_name": source_sheet_name,
    }
    for k in HEADER_MAP.values():
        d[k] = None

    for col in series.index:
        key = HEADER_MAP.get(_normalize_header(str(col)))
        if not key:
            continue
        val = series[col]
        if key in ("team_size",):
            raw = _blank_to_none(val)
            if raw is None:
                d[key] = None
            else:
                try:
                    d[key] = int(float(str(raw).replace(",", "").strip()))
                except (TypeError, ValueError):
                    d[key] = None
        elif key == "total_score":
            raw = _blank_to_none(val)
            if raw is None:
                d[key] = None
            else:
                try:
                    d[key] = float(str(raw).replace(",", "").strip())
                except (TypeError, ValueError):
                    d[key] = None
        elif key in ("export_created_at", "export_updated_at"):
            ts = pd.to_datetime(val, errors="coerce")
            d[key] = None if pd.isna(ts) else ts.to_pydatetime()
        elif key in _TEXT_KEYS:
            raw = _blank_to_none(val)
            d[key] = None if raw is None else str(raw).strip()
        else:
            raw = _blank_to_none(val)
            d[key] = None if raw is None else str(raw).strip()

    return d


def parse_virtual_challenge_submissions_workbook(
    fileobj: BinaryIO,
    filename: str,
    challenges: list[dict],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """
    Read an .xlsx workbook; return rows ready for DB upsert (no event_id / import_job_id / mdc id)
    plus parse_stats (per-sheet counts, etc.).
    """
    fn = (filename or "").lower()
    if not fn.endswith(".xlsx"):
        raise ValueError("Only .xlsx workbooks are supported for challenge submissions import.")

    raw = fileobj.read()
    if not raw:
        raise ValueError("File is empty")

    book = pd.read_excel(BytesIO(raw), sheet_name=None, engine="openpyxl")
    if not isinstance(book, dict):
        raise ValueError("Expected a multi-sheet Excel workbook.")

    sheet_plan = map_sheets_to_challenges(list(book.keys()), challenges)

    rows_out: list[dict[str, Any]] = []
    per_sheet: dict[str, dict[str, int]] = {}
    stats: dict[str, Any] = {"sheets": per_sheet, "rows_read": 0, "rows_valid": 0}

    for sheet_name, df in book.items():
        cid = sheet_plan.get(sheet_name)
        if cid is None:
            continue
        if df is None or df.empty:
            raise ValueError(f"Sheet {sheet_name!r} has no data rows.")

        mapped_cols = {_normalize_header(str(c)) for c in df.columns}
        if "leader email" not in mapped_cols or "team name" not in mapped_cols:
            raise ValueError(
                f"Sheet {sheet_name!r} is missing required columns (need 'Leader Email' and 'Team Name')."
            )

        sheet_rows = 0
        for _, series in df.iterrows():
            stats["rows_read"] += 1
            payload = _row_to_payload(series, challenge_id=cid, source_sheet_name=sheet_name)
            team = (payload.get("team_name") or "").strip()
            email = (payload.get("leader_email") or "").strip()
            if not team or not email:
                continue
            sheet_rows += 1
            rows_out.append(payload)

        per_sheet[sheet_name] = {"challenge_id": cid, "rows_written": sheet_rows}

    # Last row wins for duplicate team name within the same challenge across the workbook.
    dedup: dict[tuple[int, str], dict[str, Any]] = {}
    for r in rows_out:
        k = (int(r["challenge_id"]), str(r["team_name"]).strip().lower())
        dedup[k] = r
    rows_out = list(dedup.values())

    stats["rows_valid"] = len(rows_out)
    if not rows_out:
        raise ValueError("No data rows with both Team Name and Leader Email across submission sheets.")

    return rows_out, stats
