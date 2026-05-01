"""Virtual challenge attempt sheet parser and import API surface."""

from __future__ import annotations

import io
from unittest.mock import MagicMock

from services.virtual_challenge_attempts_sheet import (
    parse_challenge_attempts_sheet,
    preview_attempts_sheet,
    suggest_column_mapping,
)


def test_parse_attempts_sheet_csv_headers():
    csv = (
        b"Team Name,Leader Name,Leader Email,Attempts Completed\n"
        b"T1,L1,lead1@example.com,1\n"
        b"T2,L2,lead2@example.com,3\n"
    )
    rows, err = parse_challenge_attempts_sheet(csv, "a.csv")
    assert err is None
    assert len(rows) == 2
    assert rows[0]["leader_email"] == "lead1@example.com"
    assert rows[0]["attempts_completed"] == 1
    assert rows[1]["attempts_completed"] == 3


def test_parse_attempts_sheet_missing_column():
    rows, err = parse_challenge_attempts_sheet(b"a,b\n1,2", "x.csv")
    assert err
    assert "Leader Email" in err or "Attempts" in err


def test_parse_attempts_sheet_explicit_column_mapping():
    csv = b"Work Email,Qta\nx@y.com,2\n"
    rows, err = parse_challenge_attempts_sheet(
        csv, "a.csv", {"email": "Work Email", "attempts": "Qta"}
    )
    assert err is None
    assert len(rows) == 1
    assert rows[0]["leader_email"] == "x@y.com"
    assert rows[0]["attempts_completed"] == 2


def test_parse_attempts_sheet_mapping_requires_both_columns():
    csv = b"A,B\n1,2"
    rows, err = parse_challenge_attempts_sheet(csv, "a.csv", {"email": "A", "attempts": ""})
    assert err
    assert "both" in err.lower()


def test_parse_attempts_sheet_mapping_unknown_column():
    csv = b"A,B\n1,2"
    rows, err = parse_challenge_attempts_sheet(
        csv, "a.csv", {"email": "NoSuch", "attempts": "B"}
    )
    assert err
    assert "not found" in err.lower()


def test_suggest_and_preview_attempts_sheet():
    csv = b"Leader Email,Attempts Completed\na@b.com,5\n"
    sug = suggest_column_mapping(["Leader Email", "Attempts Completed"])
    assert sug.get("email") == "Leader Email"
    assert sug.get("attempts") == "Attempts Completed"
    prev = preview_attempts_sheet(csv, "t.csv")
    assert prev["headers"] == ["Leader Email", "Attempts Completed"]
    assert prev["target_fields"] == ["email", "attempts"]


def test_api_virtual_challenge_attempts_preview(client, no_admin_pw):
    csv = b"Leader Email,Attempts Completed\na@b.com,2\n"
    resp = client.post(
        "/api/import/virtual/challenge-attempts/preview",
        data={"virtual_challenge_attempts": (io.BytesIO(csv), "t.csv")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body.get("headers")
    assert body.get("suggested_mapping")


def test_api_import_challenge_attempts_parse_error(client, no_admin_pw, monkeypatch, app_mod):
    arch = MagicMock()
    arch.id = 9
    arch.stored_path = "/tmp/fake.csv"
    arch.fresh_stream = lambda: io.BytesIO(b"x")

    monkeypatch.setattr(app_mod, "archive_upload", lambda *a, **k: arch)
    monkeypatch.setattr(app_mod, "mark_archive_status", lambda *a, **k: None)

    resp = client.post(
        "/api/import/virtual/challenge-attempts",
        data={
            "virtualEventId": "1",
            "challenge_id": "1",
            "virtual_challenge_attempts": (io.BytesIO(b"not,csv"), "bad.csv"),
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    body = resp.get_json()
    assert body.get("error")
