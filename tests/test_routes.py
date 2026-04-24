"""End-to-end tests for the Flask routes using the test client.

These tests do not require a live PostgreSQL — DB-touching helpers are
monkeypatched via fixtures in ``conftest.py``.
"""

from __future__ import annotations


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
    monkeypatch.setattr(app_mod, "_load_virtual_challenges_brief", lambda _eid: [])
    resp = client.get("/virtual/leaderboard")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Submission leaderboard" in body
    assert "virtual_challenge_submission_rows" in body


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
    # Hero copy
    assert "Build" in body and "with AI" in body
    # Stats section heading + a known card title
    assert "System overview" in body
    assert "Total PW registrations" in body
    assert "In-person PW" in body
    assert "Virtual PW" in body
    assert overview_stub["mdc_total_fmt"] in body
    assert overview_stub["credits_fmt"] in body
    assert overview_stub["mdc_in_person"]["top_city"] in body
    assert overview_stub["mdc_virtual"]["top_city"] in body


def test_main_dashboard_accepts_event_overrides(client, overview_stub):
    resp = client.get("/?inPersonEventId=7&virtualEventId=8&challengeId=9")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    # Deep links should carry query overrides for in-person / virtual scope
    assert "inPersonEventId=7" in body
    assert "virtualEventId=8" in body


def test_in_person_page_renders(client, funnel_stub):
    resp = client.get("/in-person")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Mumbai" in body
    assert "Delhi" in body
    assert "Main Data Center" in body
    assert "42" in body
    assert "UTM source breakdown" in body
    assert "Attendance city" in body


def test_virtual_page_renders(client, virtual_stub):
    resp = client.get("/virtual")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Virtual arena" in body
    assert "Virtual Main Data Center" in body
    assert "Alice" in body
    assert "Bob" in body
    assert "Attendance city" not in body
    assert "Top 400 teams" in body
    assert "Registrations at close" in body
    assert "Unique MDC-linked" in body
    assert "42" in body


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
        }

    monkeypatch.setattr(app_mod, "_load_mdc_users_page", _empty_mdc_users)
    cases = (
        ("/overview/users", "Overview · Users"),
        ("/overview/settings", "Overview · Settings"),
        ("/in-person/users", "In-person · Users"),
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


def test_in_person_users_shows_download_and_city_filter(client, no_admin_pw, monkeypatch, app_mod):
    def fake_load(_eid, _page, _per_page, _search, attendance_city=None, **_kwargs):
        ac = (attendance_city or "").strip() if attendance_city else ""
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
            "export_query": "attendance_city=Mumbai" if ac == "Mumbai" else "",
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
    assert "Run import" in body_ip
    assert "Main Data Center" in body_ip
    assert "Import Main Data Center" in body_ip
    resp_v = client.get("/virtual/import")
    assert resp_v.status_code == 200
    body_v = resp_v.get_data(as_text=True)
    assert "Virtual · Import" in body_v
    assert "Virtual Main Data Center" in body_v
    assert "virtual_main_data_center" in body_v
    assert "/api/import/virtual/main-data-center" in body_v
    assert "/api/credits/grant" in body_v


def test_context_processor_injects_defaults(client, overview_stub):
    resp = client.get("/")
    body = resp.get_data(as_text=True)
    # Defaults from .env.example: in-person=1, virtual=2 (propagated into nav links)
    assert "inPersonEventId=1" in body
    assert "virtualEventId=2" in body
