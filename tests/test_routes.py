"""End-to-end tests for the Flask routes using the test client.

These tests do not require a live PostgreSQL — DB-touching helpers are
monkeypatched via fixtures in ``conftest.py``.
"""

from __future__ import annotations

from urllib.parse import urlencode

# ---------- API: validation paths (no DB needed) -------------------------


def test_leaderboard_requires_one_scope(client):
    resp = client.get("/api/leaderboard")
    assert resp.status_code == 400
    body = resp.get_json()
    assert "error" in body


def test_leaderboard_rejects_both_scopes(client):
    resp = client.get("/api/leaderboard?event_id=1&challenge_id=1")
    assert resp.status_code == 400


def test_submission_leaderboard_requires_challenge_id(client):
    resp = client.get("/api/virtual/submission-leaderboard")
    assert resp.status_code == 400
    assert "challenge_id" in resp.get_json().get("error", "").lower()


def test_virtual_leaderboard_page_renders(client, no_admin_pw, monkeypatch, app_mod):
    monkeypatch.setattr(
        app_mod,
        "_submission_leaderboard_payload",
        lambda **kw: {
            "rows": [],
            "total": 0,
            "error": None,
            "challenge": {"id": 1, "title": "Demo", "event_id": 2},
        },
    )
    monkeypatch.setattr(
        app_mod,
        "_virtual_global_submission_leaderboard",
        lambda **kw: {
            "rows": [{"rank": 1, "team_name": "T", "leader_name": "L", "leader_email": "e@x.com", "average_score": 9.0, "submitted_at": None, "arena_count": 1}],
            "total": 1,
            "error": None,
            "challenge": None,
            "scope": {},
        },
    )
    monkeypatch.setattr(
        app_mod,
        "_load_virtual_challenges_brief",
        lambda _eid: [{"id": 1, "title": "Demo", "status": "live", "opens_at": None, "closes_at": None}],
    )
    resp = client.get("/virtual/leaderboard")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Submission leaderboard" in body
    assert "virtual_challenge_submission_rows" in body
    assert "Global (all arenas)" not in body

    resp_g = client.get("/virtual/leaderboard?global=1")
    assert resp_g.status_code == 302
    assert "arenaChallengeId=1" in (resp_g.headers.get("Location") or "")


def test_api_virtual_global_submission_leaderboard_ok(client, no_admin_pw, monkeypatch, app_mod):
    monkeypatch.setattr(
        app_mod,
        "_virtual_global_submission_leaderboard",
        lambda **kw: {
            "rows": [],
            "total": 0,
            "error": None,
            "challenge": None,
            "scope": {"virtual_event_id": 1, "global": True},
        },
    )
    resp = client.get("/api/virtual/global-submission-leaderboard?virtualEventId=1")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["scope"]["global"] is True
    assert data["total"] == 0


def test_distribution_requires_one_scope(client):
    resp = client.get("/api/distribution")
    assert resp.status_code == 400


def test_credits_grant_requires_fields(client, no_admin_pw):
    resp = client.post("/api/credits/grant", json={})
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_credits_grant_invalid_delta(client, no_admin_pw):
    resp = client.post(
        "/api/credits/grant",
        json={"participant_id": "abc", "delta": "nope", "reason": "test"},
    )
    assert resp.status_code == 400


def test_import_in_person_requires_event_id(client, no_admin_pw):
    resp = client.post("/api/import/in-person", data={})
    assert resp.status_code == 400


def test_import_main_data_center_requires_file(client, no_admin_pw):
    resp = client.post("/api/import/in-person/main-data-center", data={})
    assert resp.status_code == 400
    assert "file" in (resp.get_json() or {}).get("error", "").lower()


# ---------- API: health (DB may or may not be up) ------------------------


def test_health_endpoint_shape(client):
    resp = client.get("/api/health")
    assert resp.status_code in (200, 503)
    data = resp.get_json()
    assert isinstance(data, dict)
    assert "ok" in data and "database" in data


# ---------- HTML pages (DB helpers stubbed) ------------------------------


def test_main_dashboard_renders(client, overview_stub):
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    # Hero copy (sr-only + visible spans)
    assert "Build with AI" in body
    assert "Build" in body and "with AI" in body
    assert "Live ·" in body and "IST" in body
    assert "Virtual</a>" in body
    assert "In-person</a>" in body
    # Stats section heading + a known card title
    assert "System overview" in body
    assert "At a glance" in body
    assert "Total PW registrations" in body
    assert "In-person PW" in body
    assert "Virtual PW" in body
    assert "In-person · Top 10" in body
    assert "Virtual · Top 10" in body
    assert body.count("Leaderboard overview") >= 1
    assert "Min score" in body
    assert overview_stub["mdc_total_fmt"] in body
    assert overview_stub["mdc_in_person"]["top_city"] in body
    assert overview_stub["mdc_virtual"]["top_city"] in body


def test_main_dashboard_accepts_event_overrides(client, overview_stub):
    resp = client.get("/?inPersonEventId=7&virtualEventId=8&challengeId=9")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    # Deep links should carry query overrides for in-person / virtual scope
    assert "inPersonEventId=7" in body
    assert "virtualEventId=8" in body


def test_in_person_dashboard_redirect_uses_latest_pw_on_or_before_today_ist(
    client, monkeypatch, app_mod
):
    """Default session is latest ``prompt_war_on`` on/before IST today, not a future placeholder row."""
    from datetime import date

    from tests.conftest import MDC_PAGE_STUB

    monkeypatch.setattr(app_mod, "PW_GLOBAL_LEADERBOARDS_ENABLED", False)
    monkeypatch.setattr(app_mod, "_in_person_pw_default_reference_date", lambda: date(2026, 5, 1))
    pws = [
        {
            "pw_session_id": 1,
            "city": "gurugram",
            "prompt_war_on_iso": "2026-05-16",
            "session_label": "",
            "display": "Gurugram · 16 May 2026",
            "team_count": 0,
        },
        {
            "pw_session_id": 2,
            "city": "ahmedabad",
            "prompt_war_on_iso": "2026-05-04",
            "session_label": "",
            "display": "Ahmedabad · 04 May 2026",
            "team_count": 77,
        },
        {
            "pw_session_id": 3,
            "city": "pune",
            "prompt_war_on_iso": "2026-04-25",
            "session_label": "",
            "display": "Pune · 25 Apr 2026",
            "team_count": 73,
        },
        {
            "pw_session_id": 4,
            "city": "mumbai",
            "prompt_war_on_iso": "2026-04-12",
            "session_label": "",
            "display": "Mumbai · 12 Apr 2026",
            "team_count": 106,
        },
    ]
    monkeypatch.setattr(app_mod, "_in_person_pw_options", lambda *_a, **_k: list(pws))
    monkeypatch.setattr(
        app_mod,
        "_load_mdc_stats",
        lambda _eid, *args, mode="in_person", **kwargs: dict(MDC_PAGE_STUB),
    )
    monkeypatch.setattr(
        app_mod,
        "_in_person_submission_leaderboard",
        lambda *_a, **_k: {
            "rows": [],
            "total": 0,
            "error": None,
            "scope": {},
            "page": 1,
            "per_page": 50,
            "total_pages": 1,
        },
    )
    resp = client.get("/in-person", follow_redirects=False)
    assert resp.status_code == 302
    loc = resp.headers.get("Location") or ""
    assert "ipActionCenterCity=pune" in loc
    assert "ipPromptWarDate=2026-04-25" in loc


def test_in_person_dashboard_redirect_all_future_picks_earliest_upcoming(
    client, monkeypatch, app_mod
):
    from datetime import date

    from tests.conftest import MDC_PAGE_STUB

    monkeypatch.setattr(app_mod, "PW_GLOBAL_LEADERBOARDS_ENABLED", False)
    monkeypatch.setattr(app_mod, "_in_person_pw_default_reference_date", lambda: date(2026, 3, 1))
    pws = [
        {
            "pw_session_id": 1,
            "city": "hyderabad",
            "prompt_war_on_iso": "2026-03-20",
            "session_label": "",
            "display": "Hyderabad · 20 Mar 2026",
            "team_count": 115,
        },
        {
            "pw_session_id": 2,
            "city": "bengaluru",
            "prompt_war_on_iso": "2026-03-28",
            "session_label": "",
            "display": "Bengaluru · 28 Mar 2026",
            "team_count": 150,
        },
    ]
    monkeypatch.setattr(app_mod, "_in_person_pw_options", lambda *_a, **_k: list(pws))
    monkeypatch.setattr(
        app_mod,
        "_load_mdc_stats",
        lambda _eid, *args, mode="in_person", **kwargs: dict(MDC_PAGE_STUB),
    )
    monkeypatch.setattr(
        app_mod,
        "_in_person_submission_leaderboard",
        lambda *_a, **_k: {
            "rows": [],
            "total": 0,
            "error": None,
            "scope": {},
            "page": 1,
            "per_page": 50,
            "total_pages": 1,
        },
    )
    resp = client.get("/in-person", follow_redirects=False)
    assert resp.status_code == 302
    loc = resp.headers.get("Location") or ""
    assert "ipActionCenterCity=hyderabad" in loc
    assert "ipPromptWarDate=2026-03-20" in loc


def test_in_person_page_renders(client, funnel_stub):
    resp = client.get("/in-person")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "pw_submission_analytics_charts.js" in body
    assert "Mumbai" in body
    assert "Delhi" in body
    assert "Registrations" in body
    assert "42" in body
    assert "UTM source breakdown" in body
    assert "Attendance city" in body
    assert "Full leaderboard" in body
    assert "/in-person/leaderboard" in body
    assert "mdcDateRangePanel" in body
    assert "Registration date" in body
    assert 'id="mdcDateRangePicker"' in body
    assert "data-pw-date-range" in body
    assert "api/in-person/main-data-center/stats" in body
    assert "Hawkeye" in body
    assert "City pivot" in body
    assert "Hawkeye · RSVP" in body
    assert "RSVP sent" in body
    assert "api/in-person/hawkeye/events" in body
    assert "Also in virtual PW" in body
    assert "crossoverVirtualEventId" in body


def test_in_person_page_submission_analytics_with_session(client, funnel_stub, monkeypatch, app_mod):
    def _stub_stats(**_kw):
        return {
            "error": None,
            "challenge_id": None,
            "kpi_profile": "in_person_session",
            "opens_at": None,
            "closes_at": None,
            "opens_at_display": None,
            "closes_at_display": None,
            "opens_at_set": True,
            "registrations_at_open": 3,
            "registrations_at_close": 2,
            "total_submissions": 2,
            "unique_mdc_submissions": 2,
            "submission_distinct_teams": 2,
            "submission_fresh_vs_prior_challenge": 2,
            "submission_returning_from_prior_challenge": 0,
            "submission_prior_challenge_id": None,
            "submission_prior_challenge_title": None,
            "team_segment_student": 1,
            "team_segment_professional": 1,
            "team_segment_other": 0,
            "team_segment_unknown": 0,
            "attempt_buckets_student": [{"label": "0", "count": 1}],
            "attempt_buckets_professional": [{"label": "1", "count": 1}],
            "submission_score_student_n": 0,
            "submission_score_student_min": None,
            "submission_score_student_max": None,
            "submission_score_student_avg": None,
            "submission_score_student_median": None,
            "submission_score_student_stddev": None,
            "submission_score_professional_n": 0,
            "submission_score_professional_min": None,
            "submission_score_professional_max": None,
            "submission_score_professional_avg": None,
            "submission_score_professional_median": None,
            "submission_score_professional_stddev": None,
            "submission_score_agg_n": 0,
            "submission_score_min": None,
            "submission_score_max": None,
            "submission_score_avg": None,
            "submission_score_median": None,
            "submission_score_p25": None,
            "submission_score_p75": None,
            "submission_score_stddev": None,
            "submission_score_range": None,
            "submission_crossover": {
                "error": None,
                "distinct_ip_leaders": 5,
                "distinct_v_leaders": 4,
                "both_tracks": 2,
                "ip_only": 3,
                "v_only": 2,
            },
        }

    monkeypatch.setattr(app_mod, "_in_person_action_center_stats", _stub_stats)
    resp = client.get(
        "/in-person?ipActionCenterCity=Mumbai&ipPromptWarDate=2026-04-21&ipPromptWarLabel="
    )
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Submission Analytics" in body
    assert "Submission from Virtual PW" in body
    assert "Teams by registration occupation" in body


def test_api_in_person_mdc_stats_json(client, funnel_stub):
    resp = client.get("/api/in-person/main-data-center/stats?inPersonEventId=1")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload.get("error") is None
    assert payload["total_registrations"] == 42
    assert payload.get("chart_date_min") == "2026-04-20"
    pr = payload.get("pw_session_rsvp")
    assert isinstance(pr, list)
    assert len(pr) == 2
    assert pr[0]["city"] == "Mumbai"
    assert "session_display" in pr[0]
    assert pr[0]["rsvp_sent"] == pr[0]["rsvp_accepted"] == pr[0]["attended"] == 0


def test_in_person_leaderboard_page_renders(client, funnel_stub):
    resp = client.get("/in-person/leaderboard")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Full leaderboard" in body
    assert "Per page" in body
    assert "All PWs (global)" not in body


def test_virtual_page_renders(client, virtual_stub):
    resp = client.get("/virtual")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Virtual · Standings" in body
    assert "Sprint Alpha" in body
    assert "Virtual registrations" in body
    assert "Alice" in body
    assert "Bob" in body
    assert "Attendance city" not in body
    assert "Top 400" in body
    assert "At close" in body
    assert "Unique submissions" in body
    assert "Submission Analytics" in body
    assert "/virtual/import" in body
    assert "42" in body
    assert "mdcDateRangePanel" in body
    assert "api/virtual/main-data-center/stats" in body
    assert "Also in in-person PW" in body
    assert "In-person Action Center" in body
    assert "mdcPillIpActionCenterOverlap" in body
    assert "crossoverInPersonEventId" in body
    assert "Submission from in-person PW" in body


def test_api_virtual_mdc_stats_json(client, virtual_stub):
    resp = client.get("/api/virtual/main-data-center/stats?virtualEventId=2")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload.get("error") is None
    assert payload["total_registrations"] == 42
    assert payload.get("skip_attendance_city") is True
    assert payload.get("pw_session_rsvp") == []
    assert payload.get("mdc_crossover_virtual_reg_ip_action_center") == 7


# ---------- Admin / CDI ---------------------------------------------------


def test_admin_page_renders(client, no_admin_pw, monkeypatch, app_mod):
    # /admin queries DB; stub the engine bits the view uses
    class _ConnStub:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, *_a, **_k):
            class _Result:
                def fetchone(self):
                    return None

                def mappings(self):
                    return self

                def all(self):
                    return []

            return _Result()

    class _EngineStub:
        def connect(self):
            return _ConnStub()

    monkeypatch.setattr(app_mod, "engine", _EngineStub())
    resp = client.get("/admin")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Data Ops" in body
    assert "In-person CSV import" in body


def test_legacy_admin_login_redirects_to_portal(client):
    resp = client.get("/admin/login", follow_redirects=False)
    assert resp.status_code in (302, 303)
    assert "login" in (resp.headers.get("Location") or "").lower()


# ---------- Static / misc ------------------------------------------------


def test_unknown_route_404(client):
    resp = client.get("/this-does-not-exist")
    assert resp.status_code == 404


def test_module_subpages_render(client, no_admin_pw, monkeypatch, app_mod):
    """Users / Settings stubs for each module (in-person Users needs admin open for roster)."""

    def _empty_mdc_users(_eid, _page, _per_page, _search, attendance_city=None, **kwargs):
        return {
            "error": None,
            "rows": [],
            "total": 0,
            "page": 1,
            "per_page": 25,
            "search": "",
            "attendance_city": "",
            "attendance_city_options": [],
            "total_pages": 1,
            "export_query": "",
            "preserve_query_str": "",
            "preserve_items": [],
            "mdc_pw_on": "",
            "mdc_session_label": "",
            "advanced_active": False,
            "sort_key": None,
            "sort_dir": "desc",
            "sort_hrefs": {},
            "roster_has_score_column": False,
        }

    monkeypatch.setattr(app_mod, "_load_mdc_users_page", _empty_mdc_users)
    monkeypatch.setattr(app_mod, "_load_virtual_challenges_brief", lambda _eid: [])
    cases = (
        ("/overview/logs", "Overview · Logs"),
        ("/overview/settings", "Overview · Settings"),
        ("/overview/submission-analytics", "Submission crossover"),
        ("/in-person/users", "In-person · Users"),
        ("/in-person/leaderboard", "Full leaderboard"),
        ("/in-person/settings", "In-person · Settings"),
        ("/virtual/users", "Virtual · Users"),
        ("/virtual/settings", "Virtual · Settings"),
    )
    for path, title in cases:
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert title in resp.get_data(as_text=True), path


def test_in_person_users_roster_table(client, no_admin_pw):
    resp = client.get("/in-person/users")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Promptathon city" in body
    assert "Designation" in body
    assert "Yrs exp." in body
    assert "visibility" in body


def test_in_person_users_export_csv(client, no_admin_pw, monkeypatch, app_mod):
    def fake_fetch(_eid, _q, _ac, **_kwargs):
        return [
            {
                "id": 1,
                "full_name": "Test User",
                "email": "t@example.com",
                "city": "X",
                "state": "Y",
                "country": "IN",
                "attendance_city": "Mumbai",
                "pw_session_display": "Mumbai · 28 Mar 2026",
                "prompt_war_on_iso": "2026-03-28",
                "session_label": "",
                "occupation": "Dev",
                "mobile": "",
                "profile_name": None,
                "form_timestamp": None,
            }
        ], None

    monkeypatch.setattr(app_mod, "_fetch_mdc_users_export_rows", fake_fetch)
    resp = client.get("/in-person/users/export.csv?attendance_city=Mumbai")
    assert resp.status_code == 200
    assert "csv" in (resp.headers.get("Content-Type") or "").lower()
    body = resp.get_data(as_text=True)
    assert "full_name" in body
    assert "pw_session_display" in body
    assert "Test User" in body
    assert "Mumbai" in body


def test_virtual_users_has_no_attendance_city_filter(client, no_admin_pw):
    resp = client.get("/virtual/users")
    assert resp.status_code == 200
    text = resp.get_data(as_text=True)
    assert "Attendance city" not in text
    assert "virtual/users/export.csv" in text


def test_virtual_users_export_csv_omits_attendance_city_column(client, no_admin_pw, monkeypatch, app_mod):
    def fake_fetch(_eid, _q, _ac, **_kwargs):
        return (
            [
                {
                    "id": 1,
                    "full_name": "A",
                    "email": "a@example.com",
                    "city": "Pune",
                    "state": "MH",
                    "country": "IN",
                    "attendance_city": "ShouldNotAppearInHeader",
                    "occupation": "Dev",
                    "mobile": "",
                    "profile_name": None,
                    "form_timestamp": None,
                }
            ],
            None,
        )

    monkeypatch.setattr(app_mod, "_fetch_mdc_users_export_rows", fake_fetch)
    resp = client.get("/virtual/users/export.csv")
    assert resp.status_code == 200
    raw = resp.get_data(as_text=True).lstrip("\ufeff")
    header = raw.splitlines()[0] if raw else ""
    assert "attendance_city" not in header
    assert "full_name" in header
    assert "city" in header
    assert "imported_total_score" in header


def test_in_person_users_shows_download_and_city_filter(client, no_admin_pw, monkeypatch, app_mod):
    def fake_load(_eid, _page, _per_page, _search, attendance_city=None, **_kwargs):
        ac = (attendance_city or "").strip() if attendance_city else ""
        preserve = {"attendance_city": "Mumbai"} if ac == "Mumbai" else {}
        export_query = urlencode(preserve)
        pagination = {"per_page": "25", **preserve}
        preserve_query_str = urlencode(pagination)
        preserve_items = list(preserve.items())
        return {
            "error": None,
            "rows": [],
            "total": 0,
            "page": 1,
            "per_page": 25,
            "search": "",
            "attendance_city": ac,
            "attendance_city_options": ["Mumbai", "Delhi"],
            "total_pages": 1,
            "export_query": export_query,
            "preserve_query_str": preserve_query_str,
            "challenge_id": None,
            "advanced": None,
            "advanced_active": False,
            "advanced_form_fields": app_mod.MDC_USERS_ADVANCED_FORM_FIELDS,
            "advanced_text": {},
            "advanced_raw": {},
            "preserve_items": preserve_items,
            "sort_key": None,
            "sort_dir": "desc",
            "sort_hrefs": {},
            "roster_has_score_column": False,
        }

    monkeypatch.setattr(app_mod, "_load_mdc_users_page", fake_load)
    resp = client.get("/in-person/users?attendance_city=Mumbai")
    assert resp.status_code == 200
    text = resp.get_data(as_text=True)
    assert "Download CSV" in text
    assert "Attendance city" in text
    assert "in-person/users/export.csv" in text
    assert "attendance_city=Mumbai" in text


def test_module_import_pages_when_admin_open(client, no_admin_pw):
    resp_ip = client.get("/in-person/import")
    assert resp_ip.status_code == 200
    body_ip = resp_ip.get_data(as_text=True)
    assert "In-person · Import" in body_ip
    assert "Registrations · CSV" in body_ip
    assert "Import registrations" in body_ip
    assert "Challenge attempt counts" in body_ip
    assert "/api/import/in-person/challenge-attempts" in body_ip
    assert "/api/import/in-person/challenge-attempts/preview" in body_ip
    resp_v = client.get("/virtual/import")
    assert resp_v.status_code == 200
    body_v = resp_v.get_data(as_text=True)
    assert "Virtual · Import" in body_v
    assert "Virtual registrations" in body_v
    assert "virtual_main_data_center" in body_v
    assert "/api/import/virtual/main-data-center" in body_v
    assert "Challenge attempt counts" in body_v
    assert "/api/import/virtual/challenge-attempts" in body_v
    assert "/api/import/virtual/challenge-attempts/preview" in body_v


def test_context_processor_injects_defaults(client, overview_stub):
    resp = client.get("/")
    body = resp.get_data(as_text=True)
    # Defaults from .env.example: in-person=1, virtual=2 (propagated into nav links)
    assert "inPersonEventId=1" in body
    assert "virtualEventId=2" in body
