"""In-person Action Center XLSX ETL: sheet naming + parsing (no DB required)."""

from __future__ import annotations

from io import BytesIO

import pandas as pd
import pytest

from scripts import etl_in_person_challenge_submissions as ip


def test_strip_leading_enumerator():
    assert ip.strip_leading_sheet_enumerator("1.Warm Up Challenge") == "Warm Up Challenge"
    assert ip.strip_leading_sheet_enumerator("2)Main Challenge Submission") == "Main Challenge Submission"


def test_sheet_kind_from_tab_name_variants():
    assert ip.sheet_kind_from_tab_name("Warm Up Challenge") == "warmup"
    assert ip.sheet_kind_from_tab_name("warmup challenge") == "warmup"
    assert ip.sheet_kind_from_tab_name("Warm-up Challenge") == "warmup"
    assert ip.sheet_kind_from_tab_name("Main Challenge Submission") == "main"
    assert ip.sheet_kind_from_tab_name("main challenge") == "main"
    assert ip.sheet_kind_from_tab_name("1.Main Challenge Submission") == "main"
    assert ip.sheet_kind_from_tab_name("Summary") is None


def test_map_sheets_two_tabs():
    plan = ip.map_sheets_to_kinds(["1.Warm Up Challenge", "2.Main Challenge Submission"])
    assert plan["1.Warm Up Challenge"] == "warmup"
    assert plan["2.Main Challenge Submission"] == "main"


def test_map_sheets_duplicate_main_raises():
    with pytest.raises(ValueError, match="Duplicate"):
        ip.map_sheets_to_kinds(["Main Challenge Submission", "2.Main Challenge"])


def test_map_sheets_unknown_tab_raises():
    with pytest.raises(ValueError, match="expected tab name"):
        ip.map_sheets_to_kinds(["Warm Up Challenge", "Extra"])


def test_parse_workbook_minimal():
    df_w = pd.DataFrame(
        [
            {
                "Team Name": "Team A",
                "Leader Email": "lead@example.com",
                "Leader Name": "Lead",
                "Team Size": 3,
                "Total Score (Latest Attempt)": 88.5,
            }
        ]
    )
    df_m = pd.DataFrame(
        [
            {
                "Team Name": "Team B",
                "Leader Email": "b@example.com",
                "Total Score (Latest Attempt)": "100",
            }
        ]
    )
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df_w.to_excel(writer, sheet_name="Warm Up Challenge", index=False)
        df_m.to_excel(writer, sheet_name="Main Challenge Submission", index=False)
    buf.seek(0)

    rows, stats = ip.parse_in_person_action_center_workbook(buf, "book.xlsx")
    assert len(rows) == 2
    by_team = {r["team_name"]: r for r in rows}
    assert by_team["Team A"]["sheet_kind"] == "warmup"
    assert by_team["Team A"]["total_score"] == 88.5
    assert by_team["Team A"]["team_size"] == 3
    assert by_team["Team B"]["sheet_kind"] == "main"
    assert by_team["Team B"]["total_score"] == 100.0
    assert stats["rows_valid"] == 2


def test_parse_dedupe_last_row_wins_same_sheet_kind_and_team():
    df_m = pd.DataFrame(
        [
            {"Team Name": "Same", "Leader Email": "x@example.com", "Total Score (Latest Attempt)": 1},
            {"Team Name": "Same", "Leader Email": "x@example.com", "Total Score (Latest Attempt)": 99},
        ]
    )
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        pd.DataFrame(
            [{"Team Name": "W", "Leader Email": "w@example.com"}]
        ).to_excel(writer, sheet_name="Warm Up Challenge", index=False)
        df_m.to_excel(writer, sheet_name="Main Challenge Submission", index=False)
    buf.seek(0)

    rows, _ = ip.parse_in_person_action_center_workbook(buf, "b.xlsx")
    main_rows = [r for r in rows if r["sheet_kind"] == "main"]
    assert len(main_rows) == 1
    assert main_rows[0]["total_score"] == 99.0


def test_parse_skips_empty_team_or_email():
    df_m = pd.DataFrame(
        [
            {"Team Name": "", "Leader Email": "a@example.com"},
            {"Team Name": "OK", "Leader Email": "ok@example.com"},
        ]
    )
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        pd.DataFrame([{"Team Name": "W", "Leader Email": "w@e.com"}]).to_excel(
            writer, sheet_name="Warm Up Challenge", index=False
        )
        df_m.to_excel(writer, sheet_name="Main Challenge Submission", index=False)
    buf.seek(0)

    rows, stats = ip.parse_in_person_action_center_workbook(buf, "b.xlsx")
    assert len(rows) == 2  # W warmup + OK main
    assert stats["rows_skipped"] >= 1
