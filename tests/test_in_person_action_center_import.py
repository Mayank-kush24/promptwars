"""In-person Action Center import API: validation and missing-email gate (no real DB writes)."""

from __future__ import annotations

import io
from unittest.mock import MagicMock

import pytest


def test_api_action_center_missing_attendance_city(client, no_admin_pw):
    resp = client.post(
        "/api/import/in-person/action-center",
        data={"in_person_action_center": (io.BytesIO(b"x"), "book.xlsx")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    body = resp.get_json()
    assert body and "attendance_city" in (body.get("error") or "").lower()


def test_api_action_center_missing_file(client, no_admin_pw):
    resp = client.post(
        "/api/import/in-person/action-center",
        data={"attendance_city": "Pune", "prompt_war_on": "2025-06-01"},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    body = resp.get_json()
    assert body and "in_person_action_center" in (body.get("error") or "").lower()


def test_api_action_center_missing_prompt_war_date(client, no_admin_pw):
    resp = client.post(
        "/api/import/in-person/action-center",
        data={
            "attendance_city": "Pune",
            "in_person_action_center": (io.BytesIO(b"x"), "book.xlsx"),
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    body = resp.get_json()
    assert body and "prompt_war_on" in (body.get("error") or "").lower()


def test_api_action_center_rejects_legacy_sentinel_date(client, no_admin_pw, monkeypatch, app_mod):
    mock_cm = MagicMock()
    mock_conn = MagicMock()
    r = MagicMock()
    r.scalars.return_value.all.return_value = ["Pune"]
    mock_conn.execute.return_value = r
    mock_cm.__enter__.return_value = mock_conn
    mock_cm.__exit__.return_value = None
    monkeypatch.setattr(app_mod.engine, "connect", lambda: mock_cm)
    resp = client.post(
        "/api/import/in-person/action-center",
        data={
            "attendance_city": "Pune",
            "prompt_war_on": "1970-01-01",
            "in_person_action_center": (io.BytesIO(b"x"), "book.xlsx"),
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    body = resp.get_json()
    assert body and "legacy" in (body.get("error") or "").lower()


def test_api_action_center_city_not_in_mdc_options(client, no_admin_pw, monkeypatch, app_mod):
    mock_cm = MagicMock()
    mock_conn = MagicMock()
    r = MagicMock()
    r.scalars.return_value.all.return_value = ["Delhi"]
    mock_conn.execute.return_value = r
    mock_cm.__enter__.return_value = mock_conn
    mock_cm.__exit__.return_value = None
    monkeypatch.setattr(app_mod.engine, "connect", lambda: mock_cm)
    resp = client.post(
        "/api/import/in-person/action-center",
        data={
            "attendance_city": "Pune",
            "prompt_war_on": "2025-06-15",
            "in_person_action_center": (io.BytesIO(b"x"), "book.xlsx"),
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    body = resp.get_json()
    assert body.get("nothing_written") is True


def test_api_action_center_missing_leader_emails_nothing_written(client, no_admin_pw, monkeypatch, app_mod):
    arch = MagicMock()
    arch.id = 99
    arch.stored_path = "/tmp/fake_action_center.xlsx"

    def _stream():
        return io.BytesIO(b"")

    arch.fresh_stream = _stream

    rows = [
        {
            "sheet_kind": "main",
            "source_sheet_name": "Main Challenge Submission",
            "team_name": "Team Z",
            "leader_name": "Zed",
            "leader_email": "missing@example.com",
            "leader_phone": None,
            "team_size": None,
            "problem_statements": None,
            "total_score": 10.0,
            "deployed_link": None,
            "deployed_changes_notes": None,
            "github_repository_link": None,
            "export_created_at": None,
            "export_created_by_name": None,
            "export_created_by_email": None,
            "export_updated_at": None,
            "export_updated_by_name": None,
            "export_updated_by_email": None,
        }
    ]
    parse_stats = {
        "sheets": {"Main Challenge Submission": {"sheet_kind": "main", "rows_written": 1, "rows_skipped": 0}},
        "rows_read": 1,
        "rows_valid": 1,
        "rows_skipped": 0,
    }

    monkeypatch.setattr(app_mod, "archive_upload", lambda *a, **kw: arch)
    monkeypatch.setattr(
        app_mod.etl_in_person_challenge_submissions,
        "parse_in_person_action_center_workbook",
        lambda *a, **k: (list(rows), dict(parse_stats)),
    )
    monkeypatch.setattr(app_mod, "mark_archive_status", lambda *a, **k: None)

    mock_cm = MagicMock()
    mock_conn = MagicMock()
    _n = [0]

    def _exec(stmt, params=None):
        _n[0] += 1
        s = str(stmt)
        if _n[0] == 1:
            r = MagicMock()
            r.scalars.return_value.all.return_value = ["Pune"]
            return r
        if "events" in s and "WHERE id" in s:
            r = MagicMock()
            r.fetchone.return_value = (1, "in_person")
            return r
        if "in_person_main_data_center_registrations" in s:
            m = MagicMock()
            m.mappings.return_value.all.return_value = []
            return m
        return MagicMock()

    mock_conn.execute.side_effect = _exec
    mock_cm.__enter__.return_value = mock_conn
    mock_cm.__exit__.return_value = None
    monkeypatch.setattr(app_mod.engine, "connect", lambda: mock_cm)

    resp = client.post(
        "/api/import/in-person/action-center",
        data={
            "attendance_city": "Pune",
            "prompt_war_on": "2025-06-20",
            "in_person_action_center": (io.BytesIO(b"x"), "book.xlsx"),
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    body = resp.get_json()
    assert body.get("nothing_written") is True
    assert "missing_emails" in body
    assert "missing@example.com" in body["missing_emails"]


def test_import_in_person_page_renders_action_center(client, no_admin_pw, monkeypatch, app_mod):
    mock_cm = MagicMock()
    mock_conn = MagicMock()
    r = MagicMock()
    r.scalars.return_value.all.return_value = ["Mumbai", "Delhi"]
    mock_conn.execute.return_value = r
    mock_cm.__enter__.return_value = mock_conn
    mock_cm.__exit__.return_value = None
    monkeypatch.setattr(app_mod.engine, "connect", lambda: mock_cm)
    rv = client.get("/in-person/import")
    assert rv.status_code == 200
    assert b"Action Center" in rv.data
    assert b"Mumbai" in rv.data
    assert b"prompt_war_on" in rv.data or b"Prompt War date" in rv.data


def test_api_attendance_cities_requires_in_person_event(client, no_admin_pw, monkeypatch, app_mod):
    mock_cm = MagicMock()
    mock_conn = MagicMock()
    r = MagicMock()
    r.fetchone.return_value = (2, "virtual")
    mock_conn.execute.return_value = r
    mock_cm.__enter__.return_value = mock_conn
    mock_cm.__exit__.return_value = None
    monkeypatch.setattr(app_mod.engine, "connect", lambda: mock_cm)
    resp = client.get("/api/in-person/attendance-cities?inPersonEventId=2")
    assert resp.status_code == 400


def test_api_attendance_cities_ok(client, no_admin_pw, monkeypatch, app_mod):
    mock_cm = MagicMock()
    mock_conn = MagicMock()
    _calls: list[str] = []

    def _exec(stmt, params=None):
        _calls.append(str(stmt))
        r = MagicMock()
        if len(_calls) == 1:
            r.fetchone.return_value = (1, "in_person")
            return r
        r.scalars.return_value.all.return_value = ["Pune", "Delhi"]
        return r

    mock_conn.execute.side_effect = _exec
    mock_cm.__enter__.return_value = mock_conn
    mock_cm.__exit__.return_value = None
    monkeypatch.setattr(app_mod.engine, "connect", lambda: mock_cm)
    resp = client.get("/api/in-person/attendance-cities")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["attendance_cities"] == ["Pune", "Delhi"]
