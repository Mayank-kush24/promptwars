"""Virtual challenge submission XLSX import: sheet naming + ETL (no DB required)."""

from __future__ import annotations

from io import BytesIO

import pandas as pd
import pytest

from scripts import etl_virtual_challenge_submissions as vc


def test_strip_leading_enumerator_ignored_not_used_as_id():
    assert vc.strip_leading_sheet_enumerator("1.Submission Challenge 1") == "Submission Challenge 1"
    assert vc.strip_leading_sheet_enumerator("12.Submission Challenge 1") == "Submission Challenge 1"
    assert vc.strip_leading_sheet_enumerator("2)Submission Foo") == "Submission Foo"
    assert vc.strip_leading_sheet_enumerator("Submission Bar") == "Submission Bar"


def test_submission_sheet_suffix_same_for_numbered_and_plain():
    a = vc.submission_sheet_suffix("1.Submission Challenge 1")
    b = vc.submission_sheet_suffix("Submission Challenge 1")
    assert a == b == "challenge 1"
    assert a is not None


def test_submission_sheet_suffix_non_submission_returns_none():
    assert vc.submission_sheet_suffix("Summary") is None
    assert vc.submission_sheet_suffix("Data") is None


def test_map_sheets_to_challenges_exact_title():
    challenges = [
        {"id": 10, "title": "Challenge 1", "import_sheet_suffix": None},
        {"id": 20, "title": "Challenge Two", "import_sheet_suffix": None},
    ]
    plan = vc.map_sheets_to_challenges(
        ["Submission Challenge 1", "2.Submission Challenge Two"],
        challenges,
    )
    assert plan["Submission Challenge 1"] == 10
    assert plan["2.Submission Challenge Two"] == 20


def test_map_sheets_import_sheet_suffix_overrides_title():
    challenges = [
        {"id": 1, "title": "Long marketing title here", "import_sheet_suffix": "Challenge 1"},
    ]
    plan = vc.map_sheets_to_challenges(["Submission Challenge 1"], challenges)
    assert plan["Submission Challenge 1"] == 1


def test_map_sheets_unknown_suffix_raises():
    challenges = [{"id": 1, "title": "Alpha", "import_sheet_suffix": None}]
    with pytest.raises(ValueError, match="does not match"):
        vc.map_sheets_to_challenges(["Submission Beta"], challenges)


def test_build_challenge_match_map_duplicate_suffix_raises():
    challenges = [
        {"id": 1, "title": "A", "import_sheet_suffix": "same"},
        {"id": 2, "title": "B", "import_sheet_suffix": "same"},
    ]
    with pytest.raises(ValueError, match="Two challenges share import_sheet_suffix"):
        vc.build_challenge_match_map(challenges)


def test_parse_workbook_minimal():
    challenges = [{"id": 5, "title": "Sprint", "import_sheet_suffix": None}]
    df = pd.DataFrame(
        [
            {
                "Team Name": "Team A",
                "Leader Email": "lead@example.com",
                "Leader Name": "Lead",
            }
        ]
    )
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="Submission Sprint", index=False)
    buf.seek(0)

    rows, stats = vc.parse_virtual_challenge_submissions_workbook(buf, "book.xlsx", challenges)
    assert len(rows) == 1
    assert rows[0]["challenge_id"] == 5
    assert rows[0]["team_name"] == "Team A"
    assert rows[0]["leader_email"] == "lead@example.com"
    assert stats["rows_valid"] == 1
    assert stats["rows_parsed_with_team_email"] == 1
    assert stats["rows_collapsed_duplicate_leader_email"] == 0
    assert "Submission Sprint" in stats["sheets"]
    assert stats["sheets"]["Submission Sprint"]["rows_parsed"] == 1


def test_parse_workbook_duplicate_leader_email_same_challenge_collapses():
    challenges = [{"id": 5, "title": "Sprint", "import_sheet_suffix": None}]
    df = pd.DataFrame(
        [
            {"Team Name": "Team A", "Leader Email": "same@example.com"},
            {"Team Name": "Team B", "Leader Email": "same@example.com"},
        ]
    )
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="Submission Sprint", index=False)
    buf.seek(0)

    rows, stats = vc.parse_virtual_challenge_submissions_workbook(buf, "book.xlsx", challenges)
    assert len(rows) == 1
    assert rows[0]["team_name"] == "Team B"
    assert rows[0]["leader_email"] == "same@example.com"
    assert stats["rows_parsed_with_team_email"] == 2
    assert stats["rows_collapsed_duplicate_leader_email"] == 1
    assert stats["rows_valid"] == 1
    assert stats["sheets"]["Submission Sprint"]["rows_parsed"] == 2


def test_parse_workbook_same_team_name_different_emails_keeps_both():
    challenges = [{"id": 5, "title": "Sprint", "import_sheet_suffix": None}]
    df = pd.DataFrame(
        [
            {"Team Name": "Team A", "Leader Email": "a@example.com"},
            {"Team Name": "Team A", "Leader Email": "b@example.com"},
        ]
    )
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="Submission Sprint", index=False)
    buf.seek(0)

    rows, stats = vc.parse_virtual_challenge_submissions_workbook(buf, "book.xlsx", challenges)
    assert len(rows) == 2
    assert stats["rows_collapsed_duplicate_leader_email"] == 0
    assert stats["rows_valid"] == 2
