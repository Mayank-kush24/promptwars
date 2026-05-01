"""In-person challenge attempt counts import API (validation + parse path; no real DB transaction)."""

from __future__ import annotations

import io
from datetime import date
from unittest.mock import MagicMock


def test_api_in_person_challenge_attempts_missing_session(client, no_admin_pw):
    resp = client.post(
        "/api/import/in-person/challenge-attempts",
        data={
            "sheet_kind": "main",
            "in_person_challenge_attempts": (io.BytesIO(b"x"), "a.csv"),
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    assert "pw_session_id" in (resp.get_json() or {}).get("error", "").lower()


def test_api_in_person_challenge_attempts_missing_file(client, no_admin_pw):
    resp = client.post(
        "/api/import/in-person/challenge-attempts",
        data={"pw_session_id": "1", "sheet_kind": "main"},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    body = resp.get_json()
    assert body and "in_person_challenge_attempts" in (body.get("error") or "").lower()


def test_api_in_person_challenge_attempts_bad_sheet_kind(client, no_admin_pw):
    resp = client.post(
        "/api/import/in-person/challenge-attempts",
        data={
            "pw_session_id": "1",
            "sheet_kind": "arena",
            "in_person_challenge_attempts": (io.BytesIO(b"x"), "a.csv"),
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    assert "sheet_kind" in (resp.get_json() or {}).get("error", "").lower()


def test_api_in_person_challenge_attempts_preview(client, no_admin_pw):
    csv = b"Leader Email,Attempts Completed\na@b.com,2\n"
    resp = client.post(
        "/api/import/in-person/challenge-attempts/preview",
        data={"in_person_challenge_attempts": (io.BytesIO(csv), "t.csv")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body.get("headers")
    assert body.get("suggested_mapping")


def test_api_in_person_challenge_attempts_invalid_column_mapping_json(
    client, no_admin_pw, monkeypatch, app_mod
):
    row1 = MagicMock()
    row1.fetchone.return_value = (1, "in_person")
    row2 = MagicMock()
    row2.mappings.return_value.first.return_value = {
        "id": 1,
        "city": "Austin",
        "prompt_war_on": date(2026, 3, 28),
        "session_label": "",
    }
    mock_conn = MagicMock()
    mock_conn.execute.side_effect = [row1, row2]
    mock_cm = MagicMock()
    mock_cm.__enter__.return_value = mock_conn
    mock_cm.__exit__.return_value = None
    monkeypatch.setattr(app_mod.engine, "connect", lambda: mock_cm)

    arch = MagicMock()
    arch.id = 9
    arch.stored_path = "/tmp/fake.csv"
    arch.fresh_stream = lambda: io.BytesIO(
        b"Leader Email,Attempts Completed\na@b.com,1\n"
    )
    monkeypatch.setattr(app_mod, "archive_upload", lambda *a, **k: arch)
    monkeypatch.setattr(app_mod, "mark_archive_status", lambda *a, **k: None)

    resp = client.post(
        "/api/import/in-person/challenge-attempts",
        data={
            "inPersonEventId": "1",
            "pw_session_id": "1",
            "sheet_kind": "main",
            "column_mapping": "not-json{",
            "in_person_challenge_attempts": (io.BytesIO(b"x"), "ok.csv"),
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    assert "json" in (resp.get_json() or {}).get("error", "").lower()


def test_api_in_person_challenge_attempts_parse_error(client, no_admin_pw, monkeypatch, app_mod):
    row1 = MagicMock()
    row1.fetchone.return_value = (1, "in_person")
    row2 = MagicMock()
    row2.mappings.return_value.first.return_value = {
        "id": 1,
        "city": "Austin",
        "prompt_war_on": date(2026, 3, 28),
        "session_label": "",
    }
    mock_conn = MagicMock()
    mock_conn.execute.side_effect = [row1, row2]
    mock_cm = MagicMock()
    mock_cm.__enter__.return_value = mock_conn
    mock_cm.__exit__.return_value = None
    monkeypatch.setattr(app_mod.engine, "connect", lambda: mock_cm)

    arch = MagicMock()
    arch.id = 9
    arch.stored_path = "/tmp/fake.csv"
    arch.fresh_stream = lambda: io.BytesIO(b"x")
    monkeypatch.setattr(app_mod, "archive_upload", lambda *a, **k: arch)
    monkeypatch.setattr(app_mod, "mark_archive_status", lambda *a, **k: None)

    resp = client.post(
        "/api/import/in-person/challenge-attempts",
        data={
            "inPersonEventId": "1",
            "pw_session_id": "1",
            "sheet_kind": "main",
            "in_person_challenge_attempts": (io.BytesIO(b"not,csv"), "bad.csv"),
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    body = resp.get_json()
    assert body.get("error")
