"""RSVP list import: parsing + API (PostgreSQL when tables exist)."""

from __future__ import annotations

import io
import json

import pytest
from sqlalchemy import text

from services import in_person_rsvp_list_import as ip_rsvp_list_svc


def test_suggest_mapping_registered_email():
    m = ip_rsvp_list_svc.suggest_column_mapping(["Registered email", "Full Name"])
    assert m["email"] == "Registered email"
    assert m.get("display_name") == "Full Name"


def test_parse_emails_dedupe_and_stats():
    csv = "Mail,Note\na@example.com,1\na@example.com,2\nb@example.com,\n,\nnot-an-email,x\n"
    emails, stats = ip_rsvp_list_svc.parse_emails_with_mapping(
        csv.encode(), "f.csv", {"email": "Mail", "display_name": None}
    )
    assert set(emails) == {"a@example.com", "b@example.com"}
    assert stats["rows_read"] == 5
    assert stats["rows_after_dedupe"] == 2
    assert stats["rows_blank_email"] >= 1
    assert stats["rows_invalid_email"] >= 1


def test_preview_api(client, no_admin_pw):
    csv = "Registered email\nalpha@example.com\n"
    resp = client.post(
        "/api/import/in-person/rsvp-lists/preview",
        data={"rsvp_list_file": (io.BytesIO(csv.encode()), "t.csv")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    j = resp.get_json()
    assert j["suggested_mapping"]["email"] == "Registered email"
    assert j["headers"] == ["Registered email"]
    assert j["target_fields"]


@pytest.fixture
def _skip_without_pw_sessions_table(app_mod):
    try:
        with app_mod.engine.connect() as conn:
            conn.execute(text("SELECT 1 FROM in_person_pw_sessions LIMIT 1"))
    except Exception:
        pytest.skip("in_person_pw_sessions missing — apply database/migrate_sessions.sql")


@pytest.fixture
def _skip_without_rsvp_list_table(app_mod):
    try:
        with app_mod.engine.connect() as conn:
            conn.execute(text("SELECT 1 FROM in_person_pw_session_rsvp_list_emails LIMIT 1"))
    except Exception:
        pytest.skip("in_person_pw_session_rsvp_list_emails missing — apply database/migrate_in_person_rsvp_list_imports.sql")


@pytest.fixture
def in_person_event_id(app_mod, _skip_without_pw_sessions_table):
    with app_mod.engine.connect() as conn:
        row = conn.execute(
            text("SELECT id FROM events WHERE kind = 'in_person' ORDER BY id LIMIT 1")
        ).fetchone()
    if not row:
        pytest.skip("no in_person event in database")
    return int(row[0])


def test_import_replaces_list(client, app_mod, monkeypatch, in_person_event_id, _skip_without_rsvp_list_table):
    monkeypatch.setattr(app_mod, "DEFAULT_IN_PERSON_EVENT_ID", in_person_event_id, raising=False)
    r = client.post(
        "/api/in-person/sessions",
        json={
            "event_id": in_person_event_id,
            "city": "rsvplistcity",
            "prompt_war_on": "2026-11-10",
            "session_label": "",
        },
        content_type="application/json",
    )
    assert r.status_code == 201, r.get_data(as_text=True)
    sid = int(r.get_json()["id"])

    csv1 = "Email\none@example.com\ntwo@example.com\n"
    m1 = {"email": "Email", "display_name": None, "source_timestamp": None}
    resp1 = client.post(
        "/api/import/in-person/rsvp-lists",
        data={
            "pw_session_id": str(sid),
            "list_kind": "invite_sent",
            "column_mapping": json.dumps(m1),
            "rsvp_list_file": (io.BytesIO(csv1.encode()), "a.csv"),
        },
        content_type="multipart/form-data",
    )
    assert resp1.status_code == 200, resp1.get_data(as_text=True)
    with app_mod.engine.connect() as conn:
        n = conn.execute(
            text(
                "SELECT COUNT(*) FROM in_person_pw_session_rsvp_list_emails "
                "WHERE pw_session_id = :s AND list_kind = 'invite_sent'"
            ),
            {"s": sid},
        ).scalar()
    assert int(n or 0) == 2

    csv2 = "Email\nthree@example.com\n"
    resp2 = client.post(
        "/api/import/in-person/rsvp-lists",
        data={
            "pw_session_id": str(sid),
            "list_kind": "invite_sent",
            "column_mapping": json.dumps(m1),
            "rsvp_list_file": (io.BytesIO(csv2.encode()), "b.csv"),
        },
        content_type="multipart/form-data",
    )
    assert resp2.status_code == 200
    with app_mod.engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT email_normalized FROM in_person_pw_session_rsvp_list_emails "
                "WHERE pw_session_id = :s AND list_kind = 'invite_sent' ORDER BY email_normalized"
            ),
            {"s": sid},
        ).scalars().all()
    assert [str(x) for x in rows] == ["three@example.com"]


def test_import_requires_mapping_email(
    client, app_mod, monkeypatch, in_person_event_id, _skip_without_pw_sessions_table, _skip_without_rsvp_list_table
):
    monkeypatch.setattr(app_mod, "DEFAULT_IN_PERSON_EVENT_ID", in_person_event_id, raising=False)
    r = client.post(
        "/api/in-person/sessions",
        json={
            "event_id": in_person_event_id,
            "city": "rsvplistcity2",
            "prompt_war_on": "2026-11-11",
            "session_label": "",
        },
        content_type="application/json",
    )
    assert r.status_code == 201
    sid = int(r.get_json()["id"])
    resp = client.post(
        "/api/import/in-person/rsvp-lists",
        data={
            "pw_session_id": str(sid),
            "list_kind": "accepted",
            "column_mapping": json.dumps({"email": "", "display_name": None}),
            "rsvp_list_file": (io.BytesIO(b"Email\nx@y.com\n"), "a.csv"),
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
