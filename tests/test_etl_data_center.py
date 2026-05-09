"""Tests for Main Data Center CSV/XLSX parsing."""

from __future__ import annotations

from datetime import date
from io import BytesIO
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from scripts import etl_data_center


def _minimal_csv(extra: str = "") -> BytesIO:
    city_h = '"In which city, would you like to attend the in-person PromptWars promptathon?"'
    header = f"Timestamp,Email,Full Name,{city_h}\n"
    row = "2026-01-15,a@example.com,Ada Lovelace,Mumbai\n"
    return BytesIO((header + row + extra).encode("utf-8"))


def test_coerce_pw_date_cell_nat_to_none():
    import numpy as np
    import pandas as pd

    assert etl_data_center._coerce_pw_date_cell(pd.NaT) is None
    assert etl_data_center._coerce_pw_date_cell(pd.Timestamp("NaT")) is None
    assert etl_data_center._coerce_pw_date_cell(np.datetime64("NaT")) is None


def test_parse_csv_maps_headers_and_email_pk():
    rows, stats = etl_data_center.parse_main_data_center_file(_minimal_csv(), "reg.csv")
    assert stats["rows_read"] == 1
    assert stats["rows_skipped_no_email"] == 0
    assert len(rows) == 1
    assert rows[0]["email"] == "a@example.com"
    assert rows[0]["full_name"] == "Ada Lovelace"
    assert rows[0]["attendance_city"] == "Mumbai"
    assert rows[0]["prompt_war_on"] is None
    assert rows[0]["session_label"] == ""
    assert rows[0]["form_timestamp"] is not None
    assert rows[0]["form_timestamp"].tzinfo is not None


def test_parse_csv_timestamp_with_ist_offset_preserves_local_hour():
    buf = BytesIO(
        (
            "Timestamp,Email,Full Name\n"
            '"2026-04-24 15:06:27.265+05:30",t@example.com,Test User\n'
        ).encode("utf-8")
    )
    rows, stats = etl_data_center.parse_main_data_center_file(buf, "x.csv")
    assert stats["rows_read"] == 1
    fts = rows[0]["form_timestamp"]
    assert fts is not None
    assert fts.tzinfo is not None
    assert fts.astimezone(ZoneInfo("Asia/Kolkata")).hour == 15


def test_parse_csv_skips_empty_email():
    buf = BytesIO(
        b"Email,Full Name\n"
        b"  ,\n"
        b"b@example.com,B\n"
    )
    rows, stats = etl_data_center.parse_main_data_center_file(buf, "x.csv")
    assert stats["rows_skipped_no_email"] == 1
    assert len(rows) == 1
    assert rows[0]["email"] == "b@example.com"


def test_parse_csv_dedupes_by_email_last_wins():
    buf = BytesIO(
        b"Email,Full Name\n"
        b"c@example.com,First\n"
        b"c@example.com,Second\n"
    )
    rows, stats = etl_data_center.parse_main_data_center_file(buf, "x.csv")
    assert stats["duplicate_registration_keys_collapsed"] == 1
    assert len(rows) == 1
    assert rows[0]["full_name"] == "Second"


def test_parse_csv_same_email_two_pw_sessions_keeps_both():
    buf = BytesIO(
        (
            "Email,Full Name,Prompt War Date\n"
            "c@example.com,First,2026-03-28\n"
            "c@example.com,Second,2026-05-04\n"
        ).encode("utf-8")
    )
    rows, stats = etl_data_center.parse_main_data_center_file(buf, "x.csv")
    assert stats["duplicate_registration_keys_collapsed"] == 0
    assert len(rows) == 2
    assert {r["full_name"] for r in rows} == {"First", "Second"}
    assert {r["prompt_war_on"] for r in rows} == {date(2026, 3, 28), date(2026, 5, 4)}


def test_parse_xlsx_roundtrip():
    df = pd.DataFrame(
        [
            {
                "Email": "x@y.com",
                "UTM Source": "newsletter",
                "Mobile Number": "123",
            }
        ]
    )
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False)
    buf.seek(0)
    rows, _stats = etl_data_center.parse_main_data_center_file(buf, "t.xlsx")
    assert len(rows) == 1
    assert rows[0]["email"] == "x@y.com"
    assert rows[0]["utm_source"] == "newsletter"
    assert rows[0]["mobile"] == "123"


def test_parse_rejects_missing_email_column():
    buf = BytesIO(b"Full Name\nAlice\n")
    with pytest.raises(ValueError, match="Email"):
        etl_data_center.parse_main_data_center_file(buf, "bad.csv")


def test_split_designation_with_years_trailing_marker():
    assert etl_data_center.split_designation_with_years("(Co-founder) Gen AI Developer( 2 )") == (
        "(Co-founder) Gen AI Developer",
        2,
    )
    assert etl_data_center.split_designation_with_years("-- ( 1 )") == ("--", 1)
    assert etl_data_center.split_designation_with_years(".NET Developer(1)") == (".NET Developer", 1)
    assert etl_data_center.split_designation_with_years("Intern") == ("Intern", None)
    assert etl_data_center.split_designation_with_years(None) == (None, None)


def test_parse_csv_splits_designation_trailing_years():
    buf = BytesIO(
        (
            "Email,Full Name,Designation (Year of exp.)\n"
            'd@x.com,Dee,"(Co-founder) Gen AI Developer( 2 )"\n'
        ).encode("utf-8")
    )
    rows, stats = etl_data_center.parse_main_data_center_file(buf, "x.csv")
    assert stats["rows_read"] == 1
    assert len(rows) == 1
    assert rows[0]["designation"] == "(Co-founder) Gen AI Developer"
    assert rows[0]["designation_years_experience"] == 2
