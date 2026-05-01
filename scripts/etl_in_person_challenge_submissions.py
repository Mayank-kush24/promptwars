"""
Parse two-tab XLSX workbooks for In-person Prompt Wars Action Center submissions.

Sheet tabs are matched by **name only** after stripping an optional leading
enumerator (``1.`` / ``2)`` / ``3:``) — digits are never used as identifiers.

Expected tab names (case-insensitive, normalized whitespace):
  - Warm Up Challenge / Warmup Challenge / Warm-up Challenge → ``warmup``
  - WarmUp Round App Submission (and hyphen/space variants) → ``warmup``
  - Main Challenge Submission / Main Challenge → ``main``
  - Challenge 1 Submission, Challenge 2 Submission, … (``Challenge <n> Submission``) → ``main``
"""

from __future__ import annotations

import re
from io import BytesIO
from typing import Any, BinaryIO

import pandas as pd

HEADER_MAP: dict[str, str] = {
    "team name": "team_name",
    "leader name": "leader_name",
    "leader email": "leader_email",
    "leader phone": "leader_phone",
    "team size": "team_size",
    "attempts completed": "attempts_completed",
    "problem statements": "problem_statements",
    "total score (latest attempt)": "total_score",
    "deployed link - (cloud run url)": "deployed_link",
    "deployed link (cloud run url)": "deployed_link",
    "describe the changes/updates made in the deployed version": "deployed_changes_notes",
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
    "deployed_changes_notes",
    "github_repository_link",
    "export_created_by_name",
    "export_created_by_email",
    "export_updated_by_name",
    "export_updated_by_email",
)

_LEADING_ENUM_RE = re.compile(r"^\s*\d+[\.)]\s*")
_CHALLENGE_N_SUBMISSION_RE = re.compile(r"^challenge \d+ submission$")

_WARMUP_TAB_KEYS = frozenset(
    {
        "warm up challenge",
        "warmup challenge",
        "warm-up challenge",
        "warmup round app submission",
        "warm up round app submission",
        "warm-up round app submission",
    }
)
_MAIN_TAB_KEYS = frozenset(
    {
        "main challenge submission",
        "main challenge",
    }
)


def normalize_key(s: str) -> str:
    return " ".join(str(s).strip().lower().split())


def strip_leading_sheet_enumerator(sheet_name: str) -> str:
    s = str(sheet_name).strip()
    s = " ".join(s.split())
    return _LEADING_ENUM_RE.sub("", s).strip()


def _normalize_header(label: str) -> str:
    s = str(label).replace("\ufeff", "").strip()
    s = " ".join(s.split())
    return s.lower()


def sheet_kind_from_tab_name(sheet_name: str) -> str | None:
    """
    Return ``warmup``, ``main``, or None if the tab is not a known Action Center sheet.
    """
    rest = strip_leading_sheet_enumerator(sheet_name)
    key = normalize_key(rest)
    if key in _WARMUP_TAB_KEYS:
        return "warmup"
    if key in _MAIN_TAB_KEYS:
        return "main"
    if _CHALLENGE_N_SUBMISSION_RE.match(key):
        return "main"
    return None


def map_sheets_to_kinds(sheet_names: list[str]) -> dict[str, str]:
    """
    Raw Excel sheet name -> ``warmup`` | ``main``.
    Every tab must be recognized; at least one sheet required.
    """
    out: dict[str, str] = {}
    errors: list[str] = []
    kind_to_sheet: dict[str, str] = {}

    for raw in sheet_names:
        kind = sheet_kind_from_tab_name(raw)
        if kind is None:
            errors.append(
                f"{raw!r}: expected tab like 'Warm Up Challenge', 'WarmUp Round App Submission', "
                f"'Main Challenge Submission', or 'Challenge 1 Submission' "
                "(optional leading '1.' / '2)' is ignored, not used as sheet id)."
            )
            continue
        if kind in kind_to_sheet and kind_to_sheet[kind] != raw:
            errors.append(
                f"Duplicate {kind!r} sheets: {kind_to_sheet[kind]!r} and {raw!r}. "
                "Use exactly one Warm Up and one Main tab."
            )
            continue
        kind_to_sheet[kind] = raw
        out[raw] = kind

    if errors:
        raise ValueError("Workbook sheet mapping failed:\n- " + "\n- ".join(errors))
    if not out:
        raise ValueError("No Action Center sheets found in workbook.")
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
    sheet_kind: str,
    source_sheet_name: str,
) -> dict[str, Any]:
    d: dict[str, Any] = {
        "sheet_kind": sheet_kind,
        "source_sheet_name": source_sheet_name,
    }
    for k in HEADER_MAP.values():
        d[k] = None

    for col in series.index:
        key = HEADER_MAP.get(_normalize_header(str(col)))
        if not key:
            continue
        val = series[col]
        if key in ("team_size", "attempts_completed"):
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


def parse_in_person_action_center_workbook(
    fileobj: BinaryIO,
    filename: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """
    Read an .xlsx workbook; return rows ready for DB upsert (no event_id /
    attendance_city / import_job_id / mdc id) plus parse_stats.
    """
    fn = (filename or "").lower()
    if not fn.endswith(".xlsx"):
        raise ValueError("Only .xlsx workbooks are supported for Action Center import.")

    raw = fileobj.read()
    if not raw:
        raise ValueError("File is empty")

    book = pd.read_excel(BytesIO(raw), sheet_name=None, engine="openpyxl")
    if not isinstance(book, dict):
        raise ValueError("Expected a multi-sheet Excel workbook.")

    sheet_plan = map_sheets_to_kinds(list(book.keys()))

    rows_out: list[dict[str, Any]] = []
    per_sheet: dict[str, dict[str, Any]] = {}
    stats: dict[str, Any] = {"sheets": per_sheet, "rows_read": 0, "rows_valid": 0, "rows_skipped": 0}

    for sheet_name, df in book.items():
        sk = sheet_plan.get(sheet_name)
        if sk is None:
            continue
        if df is None or df.empty:
            raise ValueError(f"Sheet {sheet_name!r} has no data rows.")

        mapped_cols = {_normalize_header(str(c)) for c in df.columns}
        if "leader email" not in mapped_cols or "team name" not in mapped_cols:
            raise ValueError(
                f"Sheet {sheet_name!r} is missing required columns (need 'Leader Email' and 'Team Name')."
            )

        sheet_rows = 0
        skipped = 0
        for _, series in df.iterrows():
            stats["rows_read"] += 1
            payload = _row_to_payload(series, sheet_kind=sk, source_sheet_name=sheet_name)
            team = (payload.get("team_name") or "").strip()
            email = (payload.get("leader_email") or "").strip()
            if not team or not email:
                skipped += 1
                continue
            sheet_rows += 1
            rows_out.append(payload)

        per_sheet[sheet_name] = {"sheet_kind": sk, "rows_written": sheet_rows, "rows_skipped": skipped}
        stats["rows_skipped"] += skipped

    dedup: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows_out:
        k = (str(r["sheet_kind"]), str(r["team_name"]).strip().lower())
        dedup[k] = r
    rows_out = list(dedup.values())

    stats["rows_valid"] = len(rows_out)
    if not rows_out:
        raise ValueError("No data rows with both Team Name and Leader Email across Action Center sheets.")

    return rows_out, stats
