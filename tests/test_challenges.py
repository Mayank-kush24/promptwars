"""Tests for the Virtual challenges manager and eligibility enforcement."""

from __future__ import annotations

import json
from datetime import date
from urllib.parse import urlencode


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
        preserve = {"challengeId": "5", "per_page": str(per_page)}
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
            "preserve_query_str": urlencode(preserve),
            "challenge_id": kwargs.get("challenge_id"),
            "advanced": None,
            "advanced_active": False,
            "advanced_form_fields": app_mod.MDC_USERS_ADVANCED_FORM_FIELDS,
            "advanced_text": {},
            "advanced_raw": {},
            "preserve_items": [("challengeId", "5")],
            "sort_key": None,
            "sort_dir": "desc",
            "sort_hrefs": {},
            "roster_has_score_column": False,
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


def test_mdc_users_build_filter_advanced_text_and_dates(app_mod):
    adv = app_mod._parse_mdc_users_advanced_from_request(
        {
            "af_city": "Mumbai",
            "af_country": "IN",
            "form_ts_from": "2024-01-15T10:00",
            "form_ts_to": "2024-01-20",
            "dob_from": "1990-01-01",
            "dob_to": "2000-12-31",
        }
    )
    assert adv is not None
    where, params = app_mod._mdc_users_build_filter(
        1, "", None, mode="in_person", advanced=adv
    )
    assert "lower(btrim(COALESCE(city" in where
    assert "lower(btrim(COALESCE(country" in where
    assert "email ILIKE" not in where
    assert "country ILIKE" not in where
    assert "form_timestamp >=" in where
    assert "form_timestamp <=" in where
    assert "dob >=" in where
    assert "dob <=" in where
    assert params.get("adv_city") == "Mumbai"
    assert params.get("adv_country") == "IN"
    assert "adv_fts_from" in params and "adv_fts_to" in params
    assert "adv_dob_from" in params and "adv_dob_to" in params


def test_mdc_users_build_filter_designation_uses_exact_match(app_mod):
    adv = app_mod._parse_mdc_users_advanced_from_request({"af_designation": "  Senior Dev  "})
    assert adv is not None
    where, params = app_mod._mdc_users_build_filter(1, "", None, mode="in_person", advanced=adv)
    assert "lower(btrim(COALESCE(designation" in where
    assert "ILIKE" not in where
    assert params.get("adv_designation") == "Senior Dev"


def test_parse_mdc_users_advanced_empty(app_mod):
    assert app_mod._parse_mdc_users_advanced_from_request({}) is None
    assert app_mod._parse_mdc_users_advanced_from_request({"af_country": "  "}) is None


def test_parse_mdc_users_advanced_years_only(app_mod):
    adv = app_mod._parse_mdc_users_advanced_from_request({"designation_years_min": "1"})
    assert adv is not None
    assert adv["designation_years_min"] == 1
    assert adv["designation_years_max"] is None


def test_parse_mdc_users_advanced_swaps_inverted_year_range(app_mod):
    adv = app_mod._parse_mdc_users_advanced_from_request(
        {"designation_years_min": "9", "designation_years_max": "2"}
    )
    assert adv is not None
    assert adv["designation_years_min"] == 2
    assert adv["designation_years_max"] == 9


def test_mdc_users_build_filter_designation_years_range(app_mod):
    adv = app_mod._parse_mdc_users_advanced_from_request(
        {"designation_years_min": "2", "designation_years_max": "5"}
    )
    where, params = app_mod._mdc_users_build_filter(1, "", None, mode="in_person", advanced=adv)
    assert "designation_years_experience >=" in where
    assert "designation_years_experience <=" in where
    assert params["adv_dyoe_min"] == 2
    assert params["adv_dyoe_max"] == 5


def test_mdc_users_active_chips_emits_one_per_filter_with_remove_qs(app_mod):
    adv = app_mod._parse_mdc_users_advanced_from_request(
        {
            "af_city": "Pune",
            "af_country": "IN",
            "form_ts_from": "2024-01-15T10:00",
            "dob_to": "2000-12-31",
        }
    )
    assert adv is not None
    preserve = app_mod._mdc_users_preserve_query_dict("alice", "Mumbai", None, adv)
    chips = app_mod._mdc_users_active_chips(adv, preserve=preserve, per_page=25)

    by_key = {c["key"]: c for c in chips}
    assert set(by_key) == {"af_city", "af_country", "form_ts_from", "dob_to"}
    assert by_key["af_city"]["value"] == "Pune"
    assert by_key["af_country"]["label"] == "Country"
    assert by_key["form_ts_from"]["label"] == "Registered from"
    assert by_key["dob_to"]["label"] == "DOB to"

    city_chip = by_key["af_city"]
    assert "af_city" not in city_chip["remove_qs"]
    assert "af_country=IN" in city_chip["remove_qs"]
    assert "q=alice" in city_chip["remove_qs"]
    assert "attendance_city=Mumbai" in city_chip["remove_qs"]
    assert "per_page=25" in city_chip["remove_qs"]


def test_mdc_users_active_chips_returns_empty_when_no_advanced(app_mod):
    assert app_mod._mdc_users_active_chips(None, preserve={}, per_page=25) == []
    assert (
        app_mod._mdc_users_active_chips(
            {"text": {}, "raw": {}}, preserve={"q": "x"}, per_page=25
        )
        == []
    )


def test_mdc_users_reset_advanced_qs_keeps_basic_filters(app_mod):
    qs = app_mod._mdc_users_reset_advanced_qs(
        search_s="alice", attendance_city="Mumbai", challenge_id=None, per_page=50
    )
    assert "per_page=50" in qs
    assert "q=alice" in qs
    assert "attendance_city=Mumbai" in qs
    assert "af_" not in qs and "form_ts_" not in qs and "dob_" not in qs
    assert "designation_years" not in qs


def test_mdc_users_reset_advanced_qs_includes_challenge_id(app_mod):
    qs = app_mod._mdc_users_reset_advanced_qs(
        search_s="", attendance_city=None, challenge_id=7, per_page=25
    )
    assert "challengeId=7" in qs
    assert "q=" not in qs
    assert "attendance_city=" not in qs


def test_mdc_users_advanced_field_groups_cover_every_text_column(app_mod):
    text_cols_in_groups: set[str] = set()
    for group in app_mod.MDC_USERS_ADVANCED_FIELD_GROUPS:
        for item in group["fields"]:
            if item.get("kind") == "select_distinct":
                text_cols_in_groups.add(item["col"])
    assert text_cols_in_groups == set(app_mod.MDC_USERS_ADVANCED_TEXT_COLUMNS)


def test_virtual_dashboard_propagates_challenge_id_without_eligibility_panel(
    client, virtual_stub, monkeypatch, app_mod
):
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
    assert "Challenge eligibility" not in body
    assert 'name="challengeId"' in body
    assert 'value="101"' in body


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


# ---------- Participation filters & session tokens ------------------------


def test_parse_mdc_users_advanced_participated_challenge_only(app_mod):
    adv = app_mod._parse_mdc_users_advanced_from_request({"participated_challenge_id": "12"})
    assert adv is not None
    assert adv["participated_challenge_id"] == 12


def test_ip_submission_session_token_roundtrip(app_mod):
    tok = app_mod._encode_ip_submission_session_token("Mumbai", date(2026, 3, 28), "Morning")
    dec = app_mod._decode_ip_submission_session_token(tok)
    assert dec == ("Mumbai", date(2026, 3, 28), "Morning")


def test_parse_mdc_users_advanced_submission_session_only(app_mod):
    tok = app_mod._encode_ip_submission_session_token("Delhi", date(2026, 1, 15), "")
    adv = app_mod._parse_mdc_users_advanced_from_request({"submission_session": tok})
    assert adv is not None
    assert adv["ip_submission_session"] == ("Delhi", date(2026, 1, 15), "")


def test_mdc_users_build_filter_virtual_participated_challenge(monkeypatch, app_mod):
    def fake_get(cid):
        if int(cid) == 12:
            return {"id": 12, "event_id": 2, "title": "Arena A"}
        return None

    monkeypatch.setattr(app_mod, "_get_virtual_challenge", fake_get)
    adv = app_mod._parse_mdc_users_advanced_from_request({"participated_challenge_id": "12"})
    where, params = app_mod._mdc_users_build_filter(2, "", None, mode="virtual", advanced=adv)
    assert "virtual_challenge_submission_rows" in where
    assert params.get("part_ch_id") == 12


def test_mdc_users_build_filter_in_person_submission_session(app_mod):
    tok = app_mod._encode_ip_submission_session_token("Pune", date(2026, 2, 1), "A")
    adv = app_mod._parse_mdc_users_advanced_from_request({"submission_session": tok})
    where, params = app_mod._mdc_users_build_filter(1, "", None, mode="in_person", advanced=adv)
    assert "in_person_challenge_submission_rows" in where
    assert params.get("ss_city") == "Pune"
    assert params.get("ss_pwo") == date(2026, 2, 1)
    assert params.get("ss_sl") == "A"


def test_virtual_users_page_shows_submitted_in_challenge_filter(client, no_admin_pw, monkeypatch, app_mod):
    def fake_load(eid, page, per_page, search, attendance_city=None, **kwargs):
        preserve = {"per_page": str(per_page)}
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
            "export_query": urlencode(preserve),
            "preserve_query_str": urlencode(preserve),
            "challenge_id": None,
            "advanced": None,
            "advanced_active": False,
            "advanced_count": 0,
            "advanced_form_fields": app_mod.MDC_USERS_ADVANCED_FORM_FIELDS,
            "advanced_field_groups": app_mod.MDC_USERS_ADVANCED_FIELD_GROUPS,
            "advanced_text": {},
            "advanced_raw": {},
            "advanced_chips": [],
            "advanced_select_options": {c: [] for c in app_mod.MDC_USERS_ADVANCED_TEXT_COLUMNS},
            "advanced_select_limit": app_mod.MDC_USERS_ADVANCED_SELECT_LIMIT,
            "reset_advanced_qs": urlencode(preserve),
            "preserve_items": list(preserve.items()),
            "mdc_pw_on": "",
            "mdc_session_label": "",
            "participation_challenge_options": [{"id": 1, "title": "Round 1"}],
            "participation_submission_session_options": [],
            "selected_participated_challenge_id": None,
            "selected_submission_session": "",
            "sort_key": None,
            "sort_dir": "desc",
            "sort_hrefs": {},
            "roster_has_score_column": False,
        }

    monkeypatch.setattr(app_mod, "_load_mdc_users_page", fake_load)
    monkeypatch.setattr(app_mod, "_load_virtual_challenges_brief", lambda *_a, **_k: [])
    resp = client.get("/virtual/users")
    assert resp.status_code == 200
    text = resp.get_data(as_text=True)
    assert "Submitted in challenge (workbook)" in text
    assert "Eligible for challenge" in text
