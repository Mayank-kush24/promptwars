"""Parse Bootcamp session CSV / XLSX (full session row: identity, logistics, links, metrics)."""

from __future__ import annotations

from datetime import date, datetime
from io import BytesIO
from typing import Any, BinaryIO

import pandas as pd

_SENTINEL_EPOCH_DATE = date(1970, 1, 1)

# Normalized header (lower, collapsed whitespace) -> internal column key
HEADER_MAP: dict[str, str] = {
    "city": "city",
    "bootcamp_on": "bootcamp_on",
    "bootcamp on": "bootcamp_on",
    "date": "bootcamp_on",
    "slot": "slot",
    "venue_status": "venue_status",
    "venue status": "venue_status",
    "speaker_status": "speaker_status",
    "speaker status": "speaker_status",
    "topic": "topic",
    "speaker_details": "speaker_details",
    "speaker details": "speaker_details",
    "speakers": "speaker_details",
    "audience_size": "audience_size",
    "audience size": "audience_size",
    "audience_type": "audience_type",
    "audience type": "audience_type",
    "location": "location",
    "complete_address": "complete_address",
    "complete address": "complete_address",
    "address": "complete_address",
    "fnb": "food_beverage",
    "f&b": "food_beverage",
    "f & b": "food_beverage",
    "food_beverage": "food_beverage",
    "food beverage": "food_beverage",
    "printables": "printables",
    "design_link": "design_link",
    "design link": "design_link",
    "deck": "deck_link",
    "deck_link": "deck_link",
    "capacity": "capacity",
    "attendees": "attendees",
    "activation": "activations",
    "activations": "activations",
    "students": "students",
    "professionals": "professionals",
}

_TEXT_KEYS = (
    "venue_status",
    "speaker_status",
    "topic",
    "speaker_details",
    "audience_type",
    "location",
    "complete_address",
    "food_beverage",
    "printables",
    "design_link",
    "deck_link",
)

_INT_KEYS = (
    "audience_size",
    "capacity",
    "attendees",
    "activations",
    "students",
    "professionals",
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


def _coerce_date_cell(val: Any) -> date | None:
    if val is None or val is pd.NaT:
        return None
    try:
        if pd.isna(val):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(val, float) and pd.isna(val):
        return None
    if isinstance(val, pd.Timestamp):
        if pd.isna(val):
            return None
        d = val.date()
    elif isinstance(val, datetime):
        d = val.date()
    elif type(val) is date:
        d = val
    else:
        s = str(val).strip()
        if not s or s.lower() == "nat":
            return None
        try:
            d = date.fromisoformat(s[:10])
        except ValueError:
            return None
    if d == _SENTINEL_EPOCH_DATE:
        return None
    return d


def _coerce_int_cell(val: Any) -> int:
    """Blank / NaN → 0; otherwise int (float allowed from spreadsheets)."""
    if _blank_to_none(val) is None:
        return 0
    try:
        if isinstance(val, float) and pd.isna(val):
            return 0
    except (TypeError, ValueError):
        pass
    try:
        return int(float(val))
    except (TypeError, ValueError):
        return 0


def _coerce_text_cell(val: Any) -> str:
    if _blank_to_none(val) is None:
        return ""
    if isinstance(val, float) and pd.isna(val):
        return ""
    return str(val).strip()


def parse_bootcamp_metrics_file(fileobj: BinaryIO, filename: str) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Return ``(rows, stats)``. Each row includes ``row_index`` plus all session fields; required: city, bootcamp_on, slot."""
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

    required = ("city", "bootcamp_on", "slot")
    missing = [k for k in required if k not in mapped]
    if missing:
        raise ValueError("Missing required column(s): " + ", ".join(missing))

    rows_read = len(df)
    out_rows: list[dict[str, Any]] = []
    for i in range(rows_read):
        city_s = _coerce_text_cell(mapped["city"].iloc[i])
        bco = _coerce_date_cell(mapped["bootcamp_on"].iloc[i])
        slot_s = _coerce_text_cell(mapped["slot"].iloc[i])
        row: dict[str, Any] = {
            "row_index": i + 2,
            "city": city_s,
            "bootcamp_on": bco,
            "slot": slot_s,
        }
        for tk in _TEXT_KEYS:
            if tk in mapped:
                row[tk] = _coerce_text_cell(mapped[tk].iloc[i])
            else:
                row[tk] = ""
        for ik in _INT_KEYS:
            if ik in mapped:
                row[ik] = _coerce_int_cell(mapped[ik].iloc[i])
            else:
                row[ik] = 0
        out_rows.append(row)

    stats = {"rows_read": rows_read, "rows_parsed": len(out_rows)}
    return out_rows, stats
