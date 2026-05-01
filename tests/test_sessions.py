"""PW sessions API + Hawkeye mapping by ``pw_session_id`` (PostgreSQL when table exists)."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import text

from services import hawkeye as hk
from tests.test_hawkeye import HawkeyeMemoryEngine


@pytest.fixture
def _skip_without_pw_sessions_table(app_mod):
    try:
        with app_mod.engine.connect() as conn:
            conn.execute(text("SELECT 1 FROM in_person_pw_sessions LIMIT 1"))
    except Exception:
        pytest.skip("in_person_pw_sessions missing — apply database/migrate_sessions.sql")


@pytest.fixture
def in_person_event_id(app_mod, _skip_without_pw_sessions_table):
    with app_mod.engine.connect() as conn:
        row = conn.execute(
            text("SELECT id FROM events WHERE kind = 'in_person' ORDER BY id LIMIT 1")
        ).fetchone()
    if not row:
        pytest.skip("no in_person event in database")
    return int(row[0])


def test_create_session(client, in_person_event_id):
    resp = client.post(
        "/api/in-person/sessions",
        json={
            "event_id": in_person_event_id,
            "city": "pytestcity",
            "prompt_war_on": "2026-06-15",
            "session_label": "",
        },
        content_type="application/json",
    )
    assert resp.status_code == 201, resp.get_data(as_text=True)
    data = resp.get_json()
    assert data["scope_key"] == "pytestcity|2026-06-15|"
    assert data["id"]


def test_create_duplicate_session(client, in_person_event_id):
    body = {
        "event_id": in_person_event_id,
        "city": "dupcity",
        "prompt_war_on": "2026-07-20",
        "session_label": "am",
    }
    r1 = client.post("/api/in-person/sessions", json=body, content_type="application/json")
    assert r1.status_code == 201
    r2 = client.post("/api/in-person/sessions", json=body, content_type="application/json")
    assert r2.status_code == 409
    err = r2.get_json()
    assert err.get("error") == "Session already exists"
    assert err.get("existing", {}).get("id")


def test_reject_legacy_date(client, in_person_event_id):
    resp = client.post(
        "/api/in-person/sessions",
        json={
            "event_id": in_person_event_id,
            "city": "x",
            "prompt_war_on": "1970-01-01",
            "session_label": "",
        },
        content_type="application/json",
    )
    assert resp.status_code == 400
    assert "1970-01-01" in (resp.get_json() or {}).get("error", "")


def test_list_sessions(client, in_person_event_id):
    client.post(
        "/api/in-person/sessions",
        json={
            "event_id": in_person_event_id,
            "city": "list_a",
            "prompt_war_on": "2026-08-01",
            "session_label": "",
        },
        content_type="application/json",
    )
    client.post(
        "/api/in-person/sessions",
        json={
            "event_id": in_person_event_id,
            "city": "list_b",
            "prompt_war_on": "2026-09-01",
            "session_label": "",
        },
        content_type="application/json",
    )
    rv = client.get(f"/api/in-person/sessions?event_id={in_person_event_id}")
    assert rv.status_code == 200
    sessions = rv.get_json().get("sessions") or []
    idx_a = next((i for i, s in enumerate(sessions) if s.get("city") == "list_a"), None)
    idx_b = next((i for i, s in enumerate(sessions) if s.get("city") == "list_b"), None)
    assert idx_a is not None and idx_b is not None
    assert idx_b < idx_a, "expected September session before August (ORDER BY prompt_war_on DESC)"


def test_delete_session_no_data(client, in_person_event_id):
    cr = client.post(
        "/api/in-person/sessions",
        json={
            "event_id": in_person_event_id,
            "city": "delclean",
            "prompt_war_on": "2026-10-10",
            "session_label": "",
        },
        content_type="application/json",
    )
    sid = cr.get_json()["id"]
    d = client.delete(f"/api/in-person/sessions/{sid}?event_id={in_person_event_id}")
    assert d.status_code == 200
    assert d.get_json().get("ok") is True


def test_delete_session_requires_event_id(client, in_person_event_id):
    cr = client.post(
        "/api/in-person/sessions",
        json={
            "event_id": in_person_event_id,
            "city": "noqevent",
            "prompt_war_on": "2026-10-11",
            "session_label": "",
        },
        content_type="application/json",
    )
    sid = cr.get_json()["id"]
    d = client.delete(f"/api/in-person/sessions/{sid}")
    assert d.status_code == 400


def test_delete_session_with_data(client, app_mod, in_person_event_id):
    cr = client.post(
        "/api/in-person/sessions",
        json={
            "event_id": in_person_event_id,
            "city": "delbusy",
            "prompt_war_on": "2026-11-11",
            "session_label": "",
        },
        content_type="application/json",
    )
    assert cr.status_code == 201
    sid = cr.get_json()["id"]
    tbl = app_mod.TABLE_IN_PERSON_MDC
    with app_mod.engine.begin() as conn:
        rid = conn.execute(
            text(f"SELECT id FROM {tbl} WHERE event_id = :eid AND pw_session_id IS NULL LIMIT 1"),
            {"eid": in_person_event_id},
        ).scalar_one_or_none()
        if rid is None:
            pytest.skip("no MDC registration row available to attach pw_session_id")
        conn.execute(
            text(f"UPDATE {tbl} SET pw_session_id = :sid WHERE id = :rid"),
            {"sid": sid, "rid": int(rid)},
        )
    d = client.delete(f"/api/in-person/sessions/{sid}?event_id={in_person_event_id}")
    assert d.status_code == 200, d.get_data(as_text=True)
    body = d.get_json() or {}
    assert body.get("ok") is True
    assert int((body.get("unlinked") or {}).get("mdc_registrations") or 0) >= 1
    with app_mod.engine.connect() as conn:
        still = conn.execute(
            text(f"SELECT pw_session_id FROM {tbl} WHERE id = :rid"),
            {"rid": int(rid)},
        ).scalar_one_or_none()
    assert still is None


def test_hawkeye_mapping_with_session(monkeypatch):
    eng = HawkeyeMemoryEngine()

    def _fake_fetch(_engine, event_id: int, pw_session_id: int):
        assert int(event_id) == 42
        assert int(pw_session_id) == 7
        return {
            "id": 7,
            "event_id": 42,
            "city": "pune",
            "prompt_war_on": date(2026, 4, 24),
            "session_label": "",
            "scope_key": "pune|2026-04-24|",
            "display_name": "Pune · 24 Apr 2026",
        }

    monkeypatch.setattr(hk, "_fetch_pw_session_row", _fake_fetch)
    row = hk.save_mapping(eng, 42, "hawk-tag-z", pw_session_id=7)
    assert row["scope_key"] == "pune|2026-04-24|"
    assert row.get("pw_session_id") == 7
    m = hk.get_mapping(eng, 42, pw_session_id=7)
    assert m is not None
    assert m["external_key"] == "hawk-tag-z"
    assert m["pw_session_id"] == 7
