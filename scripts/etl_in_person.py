"""
Pandas ETL for In-Person funnel: Data Center (RSVPs) + Action Center (Submissions).

Expected CSV columns (case-insensitive):
  - user_id (required)
  - city_id (required, must match existing cities.id for the target event)
  - display_name (optional)
  - rsvped_at / submitted_at (optional, ISO-like strings parseable by pandas)

Audit note:
    Invoked from Flask routes that share the audit-instrumented engine, so
    inserts performed by the caller are captured automatically (HTTP_REQUEST +
    SQL_EXEC + audit_data_changes row triggers). For a standalone CLI, call
    `audit.install_for_script(engine, principal=..., source='etl')` first.
"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from typing import Any, BinaryIO

import pandas as pd


REQUIRED = {"user_id", "city_id"}


def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [str(c).strip().lower() for c in df.columns]
    return df


def _read_csv(fileobj: BinaryIO) -> pd.DataFrame:
    raw = fileobj.read()
    return pd.read_csv(BytesIO(raw))


def _validate_required(df: pd.DataFrame, label: str) -> None:
    missing = REQUIRED - set(df.columns)
    if missing:
        raise ValueError(f"{label}: missing required columns: {sorted(missing)}")


def _coerce_ids(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["user_id"] = out["user_id"].astype(str).str.strip()
    out["city_id"] = pd.to_numeric(out["city_id"], errors="raise").astype(int)
    return out


@dataclass
class InPersonETLResult:
    rsvp_rows: list[dict[str, Any]]
    submission_rows: list[dict[str, Any]]
    join_stats: dict[str, int]


def parse_rsvps_csv(fileobj: BinaryIO) -> pd.DataFrame:
    df = _normalize_columns(_read_csv(fileobj))
    _validate_required(df, "RSVPs")
    df = _coerce_ids(df)
    if "display_name" in df.columns:
        df["display_name"] = df["display_name"].astype(str)
    else:
        df["display_name"] = None
    if "rsvped_at" in df.columns:
        df["rsvped_at"] = pd.to_datetime(df["rsvped_at"], errors="coerce")
    else:
        df["rsvped_at"] = None
    df = df.drop_duplicates(subset=["user_id", "city_id"], keep="last")
    return df


def parse_submissions_csv(fileobj: BinaryIO) -> pd.DataFrame:
    df = _normalize_columns(_read_csv(fileobj))
    _validate_required(df, "Submissions")
    df = _coerce_ids(df)
    if "display_name" in df.columns:
        df["display_name"] = df["display_name"].astype(str)
    else:
        df["display_name"] = None
    if "submitted_at" in df.columns:
        df["submitted_at"] = pd.to_datetime(df["submitted_at"], errors="coerce")
    else:
        df["submitted_at"] = None
    df = df.drop_duplicates(subset=["user_id", "city_id"], keep="last")
    return df


def build_join_stats(df_r: pd.DataFrame, df_s: pd.DataFrame) -> dict[str, int]:
    keys_r = set(zip(df_r["user_id"], df_r["city_id"]))
    keys_s = set(zip(df_s["user_id"], df_s["city_id"]))
    converted = keys_r & keys_s
    return {
        "rsvp_unique_pairs": len(keys_r),
        "submission_unique_pairs": len(keys_s),
        "converted_pairs": len(converted),
    }


def validate_city_ids_for_event(
    df_r: pd.DataFrame, df_s: pd.DataFrame, allowed_city_ids: set[int]
) -> None:
    bad_r = sorted(set(df_r["city_id"].tolist()) - allowed_city_ids)
    bad_s = sorted(set(df_s["city_id"].tolist()) - allowed_city_ids)
    if bad_r or bad_s:
        parts = []
        if bad_r:
            parts.append(f"RSVPs reference unknown city_id(s) for this event: {bad_r}")
        if bad_s:
            parts.append(f"Submissions reference unknown city_id(s) for this event: {bad_s}")
        raise ValueError("; ".join(parts))


def to_etl_result(df_r: pd.DataFrame, df_s: pd.DataFrame) -> InPersonETLResult:
    rsvp_rows = []
    for _, row in df_r.iterrows():
        rsvp_rows.append(
            {
                "user_id": row["user_id"],
                "city_id": int(row["city_id"]),
                "display_name": row["display_name"],
                "rsvped_at": row["rsvped_at"].isoformat() if pd.notna(row["rsvped_at"]) else None,
            }
        )
    submission_rows = []
    for _, row in df_s.iterrows():
        submission_rows.append(
            {
                "user_id": row["user_id"],
                "city_id": int(row["city_id"]),
                "display_name": row["display_name"],
                "submitted_at": row["submitted_at"].isoformat() if pd.notna(row["submitted_at"]) else None,
            }
        )
    return InPersonETLResult(
        rsvp_rows=rsvp_rows,
        submission_rows=submission_rows,
        join_stats=build_join_stats(df_r, df_s),
    )
