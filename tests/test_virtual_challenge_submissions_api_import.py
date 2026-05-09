"""Virtual challenge submissions import API: missing-email confirmation (409)."""

from __future__ import annotations

import io
from unittest.mock import MagicMock

import pytest


def test_api_virtual_submissions_missing_leader_emails_needs_confirmation(client, no_admin_pw, monkeypatch, app_mod):
    arch = MagicMock()
    arch.id = 77
    arch.stored_path = "/tmp/fake_virtual_sub.xlsx"

    def _stream():
        return io.BytesIO(b"")

    arch.fresh_stream = _stream

    rows = [
        {
            "challenge_id": 1,
            "source_sheet_name": "Submission Sprint",
            "team_name": "Team Z",
            "leader_name": "Zed",
            "leader_email": "ghost@example.com",
            "leader_phone": None,
            "team_size": None,
            "problem_statements": None,
            "total_score": 1.0,
            "deployed_link": None,
            "linkedin_post": None,
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
        "sheets": {"Submission Sprint": {"challenge_id": 1, "rows_parsed": 1}},
        "rows_read": 1,
        "rows_valid": 1,
        "rows_parsed_with_team_email": 1,
        "rows_collapsed_duplicate_leader_email": 0,
        "rows_skipped": 0,
    }

    monkeypatch.setattr(app_mod, "archive_upload", lambda *a, **kw: arch)
    monkeypatch.setattr(
        app_mod.etl_virtual_challenge_submissions,
        "parse_virtual_challenge_submissions_workbook",
        lambda *a, **k: (list(rows), dict(parse_stats)),
    )
    monkeypatch.setattr(app_mod, "mark_archive_status", lambda *a, **k: None)

    connect_calls = [0]
    vid = int(app_mod.DEFAULT_VIRTUAL_EVENT_ID)

    def _connect():
        mock_cm = MagicMock()
        mock_conn = MagicMock()
        idx = connect_calls[0]
        connect_calls[0] += 1

        def _exec(stmt, params=None):
            s = str(stmt)
            if idx == 0:
                m = MagicMock()
                m.mappings.return_value.all.return_value = [
                    {"id": 1, "title": "Sprint", "import_sheet_suffix": None},
                ]
                return m
            if "FROM events" in s and "WHERE id" in s:
                r = MagicMock()
                r.fetchone.return_value = (vid, "virtual")
                return r
            if "virtual_main_data_center_registrations" in s:
                r = MagicMock()
                r.fetchall.return_value = []
                return r
            return MagicMock()

        mock_conn.execute.side_effect = _exec
        mock_cm.__enter__.return_value = mock_conn
        mock_cm.__exit__.return_value = None
        return mock_cm

    monkeypatch.setattr(app_mod.engine, "connect", _connect)

    resp = client.post(
        "/api/import/virtual/challenge-submissions",
        data={"virtual_challenge_submissions": (io.BytesIO(b"x"), "book.xlsx")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 409
    body = resp.get_json()
    assert body.get("needs_confirmation") is True
    assert body.get("nothing_written") is True
    assert "ghost@example.com" in (body.get("missing_emails") or [])


def test_api_virtual_submissions_skip_missing_all_unknown_emails_returns_400(
    client, no_admin_pw, monkeypatch, app_mod
):
    """When every row uses an email missing from MDC, skip_missing must not write and returns 400."""
    arch = MagicMock()
    arch.id = 78
    arch.stored_path = "/tmp/fake_virtual_sub_skip.xlsx"

    def _stream():
        return io.BytesIO(b"")

    arch.fresh_stream = _stream

    rows = [
        {
            "challenge_id": 1,
            "source_sheet_name": "Submission Sprint",
            "team_name": "Team Z",
            "leader_name": "Zed",
            "leader_email": "ghost@example.com",
            "leader_phone": None,
            "team_size": None,
            "problem_statements": None,
            "total_score": 1.0,
            "deployed_link": None,
            "linkedin_post": None,
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
        "sheets": {"Submission Sprint": {"challenge_id": 1, "rows_parsed": 1}},
        "rows_read": 1,
        "rows_valid": 1,
        "rows_parsed_with_team_email": 1,
        "rows_collapsed_duplicate_leader_email": 0,
        "rows_skipped": 0,
    }

    monkeypatch.setattr(app_mod, "archive_upload", lambda *a, **kw: arch)
    monkeypatch.setattr(
        app_mod.etl_virtual_challenge_submissions,
        "parse_virtual_challenge_submissions_workbook",
        lambda *a, **k: (list(rows), dict(parse_stats)),
    )
    monkeypatch.setattr(app_mod, "mark_archive_status", lambda *a, **k: None)

    connect_calls = [0]
    vid = int(app_mod.DEFAULT_VIRTUAL_EVENT_ID)

    def _connect():
        mock_cm = MagicMock()
        mock_conn = MagicMock()
        idx = connect_calls[0]
        connect_calls[0] += 1

        def _exec(stmt, params=None):
            s = str(stmt)
            if idx == 0:
                m = MagicMock()
                m.mappings.return_value.all.return_value = [
                    {"id": 1, "title": "Sprint", "import_sheet_suffix": None},
                ]
                return m
            if "FROM events" in s and "WHERE id" in s:
                r = MagicMock()
                r.fetchone.return_value = (vid, "virtual")
                return r
            if "virtual_main_data_center_registrations" in s:
                r = MagicMock()
                r.fetchall.return_value = []
                return r
            return MagicMock()

        mock_conn.execute.side_effect = _exec
        mock_cm.__enter__.return_value = mock_conn
        mock_cm.__exit__.return_value = None
        return mock_cm

    monkeypatch.setattr(app_mod.engine, "connect", _connect)

    resp = client.post(
        "/api/import/virtual/challenge-submissions",
        data={
            "virtual_challenge_submissions": (io.BytesIO(b"x"), "book.xlsx"),
            "skip_missing_mdc_emails": "1",
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    body = resp.get_json()
    assert body.get("nothing_written") is True
    assert "ghost@example.com" in (body.get("missing_emails") or [])
    assert body.get("needs_confirmation") is not True
