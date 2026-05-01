"""Cross-track submission analytics (services + API + page smoke)."""

from __future__ import annotations

from werkzeug.datastructures import MultiDict

from services.submission_analytics import (
    SubmissionCrossoverParams,
    parse_submission_crossover_params,
    submission_crossover_cache_key,
)


def test_parse_defaults_and_challenge_list():
    md = MultiDict(
        [
            ("inPersonEventId", "3"),
            ("virtualEventId", "4"),
            ("virtualChallengeId", "1"),
            ("virtualChallengeId", "2"),
            ("ipSheetKind", "MAIN"),
            ("ipAttendanceCity", " Pune "),
        ]
    )
    p = parse_submission_crossover_params(
        md,
        default_ip_event_id=99,
        default_v_event_id=88,
    )
    assert p is not None
    assert p.in_person_event_id == 3
    assert p.virtual_event_id == 4
    assert p.virtual_challenge_ids == (1, 2)
    assert p.ip_sheet_kind == "main"
    assert p.ip_attendance_city == "Pune"


def test_parse_invalid_events_returns_none():
    assert (
        parse_submission_crossover_params(
            {"inPersonEventId": "0", "virtualEventId": "1"},
            default_ip_event_id=1,
            default_v_event_id=2,
        )
        is None
    )


def test_cache_key_stable():
    a = SubmissionCrossoverParams(
        in_person_event_id=1,
        virtual_event_id=2,
        virtual_challenge_ids=(3, 4),
        ip_imported_from=None,
    )
    b = SubmissionCrossoverParams(
        in_person_event_id=1,
        virtual_event_id=2,
        virtual_challenge_ids=(3, 4),
        ip_imported_from=None,
    )
    assert submission_crossover_cache_key(a) == submission_crossover_cache_key(b)
    c = SubmissionCrossoverParams(
        in_person_event_id=1,
        virtual_event_id=2,
        virtual_challenge_ids=(4, 3),
        ip_imported_from=None,
    )
    assert submission_crossover_cache_key(a) != submission_crossover_cache_key(c)


def test_api_overview_submission_crossover_ok(client, no_admin_pw, monkeypatch, app_mod):
    def _stub(_engine, p: SubmissionCrossoverParams):
        return {
            "error": None,
            "scope": {
                "in_person_event_id": p.in_person_event_id,
                "virtual_event_id": p.virtual_event_id,
                "virtual_challenge_ids": list(p.virtual_challenge_ids) if p.virtual_challenge_ids else None,
                "match_on": "leader_email_normalized",
            },
            "counts": {
                "distinct_ip_leaders": 10,
                "distinct_v_leaders": 8,
                "both_tracks": 5,
                "ip_only": 5,
                "v_only": 3,
            },
            "filters_applied": {},
            "by_ip_attendance_city": [
                {"attendance_city": "Pune", "n_leaders_both_tracks": 3},
            ],
        }

    monkeypatch.setattr(
        app_mod.submission_analytics_svc,
        "load_submission_crossover_uncached",
        _stub,
    )
    resp = client.get(
        "/api/overview/submission-crossover?inPersonEventId=1&virtualEventId=2"
        "&virtualChallengeId=7&ipAttendanceCity=Pune"
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["error"] is None
    assert data["counts"]["both_tracks"] == 5
    assert data["scope"]["virtual_challenge_ids"] == [7]
    assert data["by_ip_attendance_city"][0]["attendance_city"] == "Pune"


def test_api_overview_submission_crossover_bad_event(client, no_admin_pw):
    resp = client.get("/api/overview/submission-crossover?inPersonEventId=0&virtualEventId=2")
    assert resp.status_code == 400


def test_overview_submission_analytics_page_renders(client, no_admin_pw, monkeypatch, app_mod):
    monkeypatch.setattr(app_mod, "_load_virtual_challenges_brief", lambda _eid: [{"id": 1, "title": "A", "status": "live", "opens_at": None, "closes_at": None}])
    resp = client.get("/overview/submission-analytics")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Submission crossover" in body
    assert "query_stats" in body or "Run" in body
