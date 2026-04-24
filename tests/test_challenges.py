"""Tests for the Virtual challenges manager and eligibility enforcement."""

from __future__ import annotations

import json


# ---------- /virtual/challenges (manager UI) -----------------------------


def test_virtual_challenges_page_renders(client, no_admin_pw, challenges_stub):
    resp = client.get("/virtual/challenges")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Virtual · Challenges" in body
    assert "Add new challenge" in body
    assert "All challenges" in body
    # Form fields
    assert 'name="title"' in body
    assert 'name="opens_at"' in body
    assert 'name="closes_at"' in body
    assert 'name="status"' in body
    # Both stubbed challenges should be listed
    assert "Sprint Alpha" in body
    assert "Sprint Beta" in body
    # Eligibility cells link through to the filtered users page
    assert "challengeId=101" in body
    # Eligible / Total numbers from stub
    assert "12" in body and "50" in body


def test_virtual_challenges_create_validates_missing_title(client, no_admin_pw, challenges_stub):
    resp = client.post(
        "/virtual/challenges",
        data={"title": "", "closes_at": "2026-05-01T12:00", "status": "draft"},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    location = resp.headers.get("Location", "")
    assert "/virtual/challenges" in location
    assert "error=" in location
    assert "Title" in location or "title" in location


def test_virtual_challenges_create_validates_window(client, no_admin_pw, challenges_stub):
    resp = client.post(
        "/virtual/challenges",
        data={
            "title": "Bad window",
            "opens_at": "2026-05-01T12:00",
            "closes_at": "2026-05-01T11:00",
            "status": "draft",
        },
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    assert "error=" in resp.headers.get("Location", "")


def test_virtual_challenges_create_persists(client, no_admin_pw, challenges_stub, monkeypatch, app_mod):
    captured = {}

    class _Conn:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def execute(self, sql, params=None):
            sql_text = str(sql.text if hasattr(sql, "text") else sql)
            if "FROM events" in sql_text and "kind" in sql_text:
                class _R:
                    def fetchone(self_inner):
                        return ("virtual",)
                return _R()
            captured["sql"] = sql_text
            captured["params"] = params or {}
            class _R:
                def fetchone(self_inner):
                    return None
            return _R()

    class _Engine:
        def begin(self):
            return _Conn()
        def connect(self):
            return _Conn()

    monkeypatch.setattr(app_mod, "engine", _Engine())
    resp = client.post(
        "/virtual/challenges",
        data={
            "title": "Sprint Gamma",
            "description": "Third sprint",
            "opens_at": "2026-05-01T10:00",
            "closes_at": "2026-05-08T18:00",
            "status": "live",
            "slug": "gamma",
        },
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    assert "ok=" in resp.headers.get("Location", "")
    assert "INSERT INTO challenges" in captured.get("sql", "")
    p = captured.get("params") or {}
    assert p.get("title") == "Sprint Gamma"
    assert p.get("status") == "live"
    assert p.get("slug") == "gamma"
    # Datetimes should round-trip as datetime objects
    assert p.get("opens_at") is not None
    assert p.get("closes_at") is not None


def test_virtual_challenges_delete(client, no_admin_pw, challenges_stub, monkeypatch, app_mod):
    captured = {}

    class _Conn:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def execute(self, sql, params=None):
            captured["sql"] = str(sql.text if hasattr(sql, "text") else sql)
            captured["params"] = params or {}
            class _R:
                def fetchone(self_inner):
                    return None
            return _R()

    class _Engine:
        def begin(self):
            return _Conn()
        def connect(self):
            return _Conn()

    monkeypatch.setattr(app_mod, "engine", _Engine())
    resp = client.post("/virtual/challenges/101/delete", follow_redirects=False)
    assert resp.status_code in (302, 303)
    assert "ok=" in resp.headers.get("Location", "")
    assert "DELETE FROM challenges" in captured.get("sql", "")
    assert (captured.get("params") or {}).get("cid") == 101


def test_virtual_challenges_delete_unknown(client, no_admin_pw, monkeypatch, app_mod):
    monkeypatch.setattr(app_mod, "_get_virtual_challenge", lambda *_a, **_k: None)
    resp = client.post("/virtual/challenges/999/delete", follow_redirects=False)
    assert resp.status_code in (302, 303)
    assert "error=" in resp.headers.get("Location", "")


# ---------- API: challenges + eligibility --------------------------------


def test_api_virtual_challenges_lists(client, monkeypatch, app_mod):
    monkeypatch.setattr(
        app_mod,
        "_load_virtual_challenges_brief",
        lambda *_a, **_k: [
            {"id": 7, "title": "X", "opens_at": None, "closes_at": None, "status": "live"},
        ],
    )
    resp = client.get("/api/virtual/challenges?virtualEventId=2")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["event_id"] == 2
    assert body["challenges"][0]["id"] == 7
    assert body["challenges"][0]["status"] == "live"


def test_api_virtual_challenge_eligibility_summary(client, monkeypatch, app_mod):
    monkeypatch.setattr(
        app_mod,
        "_load_virtual_eligibility_summary",
        lambda eid, cid: {
            "challenge_id": int(cid),
            "title": "Sprint",
            "opens_at": None,
            "closes_at": None,
            "status": "live",
            "total": 100,
            "eligible": 60,
            "eligible_last_7_days": 5,
            "error": None,
        },
    )
    resp = client.get("/api/virtual/challenges/3/eligibility?virtualEventId=2")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["challenge_id"] == 3
    assert body["eligible"] == 60
    assert body["total"] == 100


def test_api_virtual_challenge_eligibility_404(client, monkeypatch, app_mod):
    monkeypatch.setattr(
        app_mod,
        "_load_virtual_eligibility_summary",
        lambda eid, cid: {
            "challenge_id": int(cid),
            "title": None,
            "opens_at": None,
            "closes_at": None,
            "status": None,
            "total": 0,
            "eligible": 0,
            "eligible_last_7_days": 0,
            "error": "challenge not found for event",
        },
    )
    resp = client.get("/api/virtual/challenges/999/eligibility")
    assert resp.status_code == 404


# ---------- Eligibility filter on /virtual and /virtual/users ------------


def test_virtual_users_filter_threads_challenge_id(
    client, no_admin_pw, monkeypatch, app_mod
):
    captured = {}

    def fake_load(eid, page, per_page, search, attendance_city=None, **kwargs):
        captured.update(kwargs)
        captured["event_id"] = eid
        return {
            "error": None,
            "rows": [],
            "total": 0,
            "page": 1,
            "per_page": per_page,
            "search": "",
            "attendance_city": "",
            "attendance_city_options": [],
            "total_pages": 1,
            "export_query": "challengeId=5",
            "challenge_id": kwargs.get("challenge_id"),
        }

    monkeypatch.setattr(app_mod, "_load_mdc_users_page", fake_load)
    monkeypatch.setattr(
        app_mod,
        "_load_virtual_challenges_brief",
        lambda *_a, **_k: [{"id": 5, "title": "Sprint", "status": "live", "opens_at": None, "closes_at": None}],
    )
    resp = client.get("/virtual/users?challengeId=5")
    assert resp.status_code == 200
    assert captured.get("mode") == "virtual"
    assert captured.get("challenge_id") == 5
    body = resp.get_data(as_text=True)
    assert "Eligible for challenge" in body
    assert "challengeId=5" in body


def test_virtual_users_export_csv_threads_challenge_id(
    client, no_admin_pw, monkeypatch, app_mod
):
    captured = {}

    def fake_fetch(eid, q, ac, **kwargs):
        captured["event_id"] = eid
        captured.update(kwargs)
        return [], None

    monkeypatch.setattr(app_mod, "_fetch_mdc_users_export_rows", fake_fetch)
    resp = client.get("/virtual/users/export.csv?challengeId=9")
    assert resp.status_code == 200
    assert captured.get("mode") == "virtual"
    assert captured.get("challenge_id") == 9


def test_mdc_users_build_filter_emits_eligibility_clause(app_mod):
    where, params = app_mod._mdc_users_build_filter(
        2, "", None, mode="virtual", challenge_id=42
    )
    assert "form_timestamp" in where
    assert "SELECT closes_at FROM challenges WHERE id = :cid" in where
    assert params.get("cid") == 42
    assert params.get("eid") == 2


def test_mdc_users_build_filter_skips_for_in_person(app_mod):
    where, params = app_mod._mdc_users_build_filter(
        1, "", None, mode="in_person", challenge_id=42
    )
    assert "form_timestamp" not in where
    assert "cid" not in params


def test_virtual_dashboard_shows_eligibility_pill(client, virtual_stub, monkeypatch, app_mod):
    monkeypatch.setattr(
        app_mod,
        "_load_virtual_eligibility_summary",
        lambda eid, cid: {
            "challenge_id": int(cid),
            "title": "Sprint",
            "opens_at": None,
            "closes_at": "01-05-2026 12:00:00",
            "status": "live",
            "total": 200,
            "eligible": 150,
            "eligible_last_7_days": 11,
            "error": None,
        },
    )
    resp = client.get("/virtual?challengeId=101")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Manage challenges" in body
    assert "Challenge eligibility" in body
    assert "150" in body and "200" in body


def test_virtual_redirects_when_challenge_id_not_in_event(client, virtual_stub, monkeypatch, app_mod):
    monkeypatch.setattr(
        app_mod,
        "_submission_leaderboard_payload",
        lambda **kw: {
            "rows": [],
            "total": 0,
            "error": None,
            "challenge": {"id": int(kw["challenge_id"]), "title": "stub", "event_id": 2},
        },
    )
    resp = client.get("/virtual?challengeId=1", follow_redirects=False)
    assert resp.status_code == 302
    loc = resp.headers.get("Location", "")
    assert "challengeId=1" not in loc


def test_virtual_arena_challenge_param_overrides_eligibility(client, virtual_stub, monkeypatch, app_mod):
    captured: dict = {}

    def _capture(**kw):
        captured["challenge_id"] = int(kw["challenge_id"])
        return {
            "rows": [],
            "total": 0,
            "error": None,
            "challenge": {"id": int(kw["challenge_id"]), "title": "stub", "event_id": 2},
        }

    monkeypatch.setattr(app_mod, "_submission_leaderboard_payload", _capture)
    resp = client.get("/virtual?challengeId=101&arenaChallengeId=102")
    assert resp.status_code == 200
    assert captured.get("challenge_id") == 102


def test_virtual_picks_first_challenge_when_default_missing(client, monkeypatch, app_mod):
    from tests.conftest import MDC_PAGE_STUB

    brief = [
        {"id": 55, "title": "Only", "opens_at": None, "closes_at": None, "status": "live"},
    ]
    monkeypatch.setattr(app_mod, "_load_virtual_challenges_brief", lambda *_a, **_k: list(brief))
    monkeypatch.setattr(
        app_mod,
        "_load_virtual_bundle",
        lambda *_a, **_k: ({"rows": [], "error": None}, {"bins": [], "error": None}, []),
    )
    monkeypatch.setattr(
        app_mod,
        "_submission_leaderboard_payload",
        lambda **kw: {
            "rows": [],
            "total": 0,
            "error": None,
            "challenge": {"id": 55, "title": "Only", "event_id": 2},
        },
    )
    monkeypatch.setattr(app_mod, "_load_mdc_stats", lambda *_a, **_k: dict(MDC_PAGE_STUB))
    resp = client.get("/virtual")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Only" in body
    assert 'value="55"' in body


# ---------- /api/credits/grant: enforcement ------------------------------


def _grant_engine_stub(eligible: bool):
    captured: dict = {"executes": []}

    class _Conn:
        def __init__(self, autocommit=False):
            self._autocommit = autocommit

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            sql_text = str(sql.text if hasattr(sql, "text") else sql)
            captured["executes"].append((sql_text, dict(params or {})))

            class _Result:
                def fetchone(self_inner):
                    if "WHERE idempotency_key" in sql_text:
                        return None
                    if "FROM challenges WHERE id" in sql_text and "event_id" in sql_text:
                        # event_id resolution for a challenge
                        return (2,)
                    if "FROM events WHERE id" in sql_text:
                        return (2, "virtual")
                    return None

                def scalar_one(self_inner):
                    return 9999

            return _Result()

    class _Engine:
        def connect(self):
            return _Conn()

        def begin(self):
            return _Conn()

    captured["engine"] = _Engine()
    return captured


def test_credits_grant_blocks_ineligible(client, no_admin_pw, monkeypatch, app_mod):
    cap = _grant_engine_stub(eligible=False)
    monkeypatch.setattr(app_mod, "engine", cap["engine"])
    monkeypatch.setattr(
        app_mod,
        "_is_participant_eligible_for_challenge",
        lambda *_a, **_k: False,
    )
    resp = client.post(
        "/api/credits/grant",
        json={"participant_id": 5, "delta": 10, "reason": "test", "challenge_id": 101},
    )
    assert resp.status_code == 409
    body = resp.get_json()
    assert body["error"] == "participant ineligible for challenge"
    assert body["challenge_id"] == 101
    # No INSERT into credit_ledger should have happened
    assert not any("INSERT INTO credit_ledger" in s for s, _ in cap["executes"])


def test_credits_grant_allows_eligible(client, no_admin_pw, monkeypatch, app_mod):
    cap = _grant_engine_stub(eligible=True)
    monkeypatch.setattr(app_mod, "engine", cap["engine"])
    monkeypatch.setattr(
        app_mod,
        "_is_participant_eligible_for_challenge",
        lambda *_a, **_k: True,
    )
    resp = client.post(
        "/api/credits/grant",
        json={"participant_id": 5, "delta": 10, "reason": "test", "challenge_id": 101},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    inserts = [(s, p) for s, p in cap["executes"] if "INSERT INTO credit_ledger" in s]
    assert inserts
    # Default metadata should NOT contain force_ineligible
    meta = json.loads(inserts[0][1].get("meta") or "{}")
    assert "force_ineligible" not in meta


def test_credits_grant_force_overrides(client, no_admin_pw, monkeypatch, app_mod):
    cap = _grant_engine_stub(eligible=False)
    monkeypatch.setattr(app_mod, "engine", cap["engine"])
    monkeypatch.setattr(
        app_mod,
        "_is_participant_eligible_for_challenge",
        lambda *_a, **_k: False,
    )
    resp = client.post(
        "/api/credits/grant",
        json={
            "participant_id": 5,
            "delta": 10,
            "reason": "test",
            "challenge_id": 101,
            "force": True,
        },
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    inserts = [(s, p) for s, p in cap["executes"] if "INSERT INTO credit_ledger" in s]
    assert inserts
    meta = json.loads(inserts[0][1].get("meta") or "{}")
    assert meta.get("force_ineligible") is True


def test_credits_grant_event_only_skips_eligibility(client, no_admin_pw, monkeypatch, app_mod):
    """event_id-only grants (no challenge) bypass the eligibility check entirely."""
    cap = _grant_engine_stub(eligible=False)
    monkeypatch.setattr(app_mod, "engine", cap["engine"])
    called = {"n": 0}
    def _check(*_a, **_k):
        called["n"] += 1
        return False
    monkeypatch.setattr(app_mod, "_is_participant_eligible_for_challenge", _check)
    resp = client.post(
        "/api/credits/grant",
        json={"participant_id": 5, "delta": 10, "reason": "test", "event_id": 2},
    )
    assert resp.status_code == 200
    assert called["n"] == 0
