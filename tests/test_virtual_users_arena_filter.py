"""Arena chart → Virtual · Users roster filter (query parse + SQL + route wiring)."""


def test_parse_mdc_users_advanced_arena_only(app_mod):
    adv = app_mod._parse_mdc_users_advanced_from_request(
        {
            "arenaChallengeId": "42",
            "arenaTeamSegment": "student",
            "arenaAttemptsCompleted": "2",
        }
    )
    assert adv is not None
    assert adv["arena_challenge_id"] == 42
    assert adv["arena_team_segment"] == "student"
    assert adv["arena_attempts_completed"] == 2


def test_parse_mdc_users_advanced_arena_attempts_zero(app_mod):
    adv = app_mod._parse_mdc_users_advanced_from_request(
        {
            "arenaChallengeId": "42",
            "arenaTeamSegment": "student",
            "arenaAttemptsCompleted": "0",
        }
    )
    assert adv is not None
    assert adv["arena_attempts_completed"] == 0


def test_parse_mdc_users_advanced_arena_ignores_attempts_for_other(app_mod):
    adv = app_mod._parse_mdc_users_advanced_from_request(
        {
            "arenaChallengeId": "1",
            "arenaTeamSegment": "other",
            "arenaAttemptsCompleted": "3",
        }
    )
    assert adv is not None
    assert adv["arena_team_segment"] == "other"
    assert adv["arena_attempts_completed"] is None


def test_parse_mdc_users_advanced_returns_none_without_segment(app_mod):
    assert (
        app_mod._parse_mdc_users_advanced_from_request(
            {
                "arenaChallengeId": "99",
            }
        )
        is None
    )


def test_arena_roster_filter_applicable_respects_event(app_mod, monkeypatch):
    def fake_get(cid):
        if int(cid) == 7:
            return {"id": 7, "event_id": 2, "title": "T"}
        return None

    monkeypatch.setattr(app_mod, "_get_virtual_challenge", fake_get)
    adv = {
        "text": {},
        "raw": {},
        "arena_challenge_id": 7,
        "arena_team_segment": "student",
        "arena_attempts_completed": None,
    }
    assert app_mod._arena_roster_filter_applicable(adv, event_id=2, mode="virtual") is True
    assert app_mod._arena_roster_filter_applicable(adv, event_id=99, mode="virtual") is False
    assert app_mod._arena_roster_filter_applicable(adv, event_id=2, mode="in_person") is False


def test_mdc_users_build_filter_includes_arena_exists(app_mod, monkeypatch):
    monkeypatch.setattr(
        app_mod,
        "_get_virtual_challenge",
        lambda cid: {"id": int(cid), "event_id": 5, "title": "X"},
    )
    adv = {
        "text": {},
        "raw": {},
        "arena_challenge_id": 3,
        "arena_team_segment": "student",
        "arena_attempts_completed": 2,
    }
    sql, params = app_mod._mdc_users_build_filter(
        5, "", None, mode="virtual", challenge_id=None, advanced=adv
    )
    assert "virtual_challenge_submission_rows" in sql
    assert "arena_ch_id" in params
    assert params["arena_ch_id"] == 3
    assert "arena_ac_eq" in params
    assert params["arena_ac_eq"] == 2
    assert "college_student" in sql


def test_mdc_users_build_filter_arena_attempts_not_reported(app_mod, monkeypatch):
    monkeypatch.setattr(
        app_mod,
        "_get_virtual_challenge",
        lambda cid: {"id": int(cid), "event_id": 5, "title": "X"},
    )
    adv = {
        "text": {},
        "raw": {},
        "arena_challenge_id": 3,
        "arena_team_segment": "student",
        "arena_attempts_completed": 0,
    }
    sql, params = app_mod._mdc_users_build_filter(
        5, "", None, mode="virtual", challenge_id=None, advanced=adv
    )
    assert "virtual_challenge_submission_rows" in sql
    assert "attempts_completed IS NULL OR s.attempts_completed < 1" in sql.replace("\n", " ")
    assert "arena_ac_eq" not in params


def test_parse_mdc_users_roster_sort_virtual(app_mod):
    assert app_mod._parse_mdc_users_roster_sort({"sort": "name", "sort_dir": "asc"}, mode="virtual") == (
        "name",
        "asc",
    )
    assert app_mod._parse_mdc_users_roster_sort({"sort": "bogus"}, mode="virtual")[0] is None


def test_mdc_users_roster_order_score_virtual_without_challenge(app_mod):
    sql = app_mod._mdc_users_roster_order_clause(
        "score", "desc", mode="virtual", challenge_id=None
    )
    assert "mdc_submission_score" in sql
    sql_ip = app_mod._mdc_users_roster_order_clause(
        "score", "asc", mode="in_person", challenge_id=None
    )
    assert "mdc_submission_score" not in sql_ip


def test_virtual_score_sql_all_challenges_when_no_challenge_id(app_mod):
    sql = app_mod._mdc_users_virtual_submission_score_select_sql(
        app_mod.TABLE_VIRTUAL_MDC, None
    )
    assert "s.challenge_id = :cid" not in sql
    assert "ORDER BY" in sql
    sql_c = app_mod._mdc_users_virtual_submission_score_select_sql(
        app_mod.TABLE_VIRTUAL_MDC, 99
    )
    assert "s.challenge_id = :cid" in sql_c


def test_parse_mdc_users_advanced_ip_arena_from_submission_session(app_mod):
    tok = app_mod._encode_ip_submission_session_token(
        "Mumbai", __import__("datetime").date(2026, 1, 15), "Track A"
    )
    adv = app_mod._parse_mdc_users_advanced_from_request(
        {
            "submission_session": tok,
            "arenaTeamSegment": "professional",
            "arenaAttemptsCompleted": "0",
        }
    )
    assert adv is not None
    assert adv["arena_challenge_id"] is None
    assert adv["arena_team_segment"] == "professional"
    assert adv["arena_attempts_completed"] == 0
    assert adv["ip_submission_session"] is not None


def test_ip_ac_arena_roster_filter_sql(app_mod):
    city = "Mumbai"
    pwo = __import__("datetime").date(2026, 1, 15)
    slab = "A"
    tok = app_mod._encode_ip_submission_session_token(city, pwo, slab)
    adv = app_mod._parse_mdc_users_advanced_from_request(
        {
            "submission_session": tok,
            "arenaTeamSegment": "student",
            "arenaAttemptsCompleted": "3",
        }
    )
    sql, params = app_mod._mdc_users_build_filter(
        5, "", None, mode="in_person", challenge_id=None, advanced=adv
    )
    assert app_mod.TABLE_IN_PERSON_CHALLENGE_SUBMISSIONS in sql
    assert "ip_ac_eq" in params
    assert params["ip_ac_eq"] == 3
    assert "college_student" in sql


def test_ip_ac_arena_attempts_not_reported(app_mod):
    city = "Pune"
    pwo = __import__("datetime").date(2026, 2, 1)
    tok = app_mod._encode_ip_submission_session_token(city, pwo, "")
    adv = app_mod._parse_mdc_users_advanced_from_request(
        {
            "submission_session": tok,
            "arenaTeamSegment": "student",
            "arenaAttemptsCompleted": "0",
        }
    )
    sql, params = app_mod._mdc_users_build_filter(
        1, "", None, mode="in_person", challenge_id=None, advanced=adv
    )
    assert "attempts_completed IS NULL OR s.attempts_completed < 1" in sql.replace("\n", " ")
    assert "ip_ac_eq" not in params


def test_virtual_users_passes_virtual_event_id_to_loader(client, monkeypatch, app_mod, no_admin_pw):
    seen = {}

    def fake_load(eid, *a, **kw):
        seen["event_id"] = eid
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
            "preserve_query_str": "per_page=25",
            "challenge_id": None,
            "advanced": None,
            "advanced_active": False,
            "advanced_count": 0,
            "advanced_form_fields": app_mod.MDC_USERS_ADVANCED_FORM_FIELDS,
            "advanced_field_groups": app_mod.MDC_USERS_ADVANCED_FIELD_GROUPS,
            "advanced_text": {},
            "advanced_raw": {},
            "advanced_chips": [],
            "reset_advanced_qs": "per_page=25",
            "preserve_items": [],
            "mdc_pw_on": "",
            "mdc_session_label": "",
            "participation_challenge_options": [],
            "participation_submission_session_options": [],
            "selected_participated_challenge_id": None,
            "selected_submission_session": "",
            "arena_from_charts_active": False,
            "sort_key": None,
            "sort_dir": "desc",
            "sort_hrefs": {},
            "roster_has_score_column": False,
        }

    monkeypatch.setattr(app_mod, "_load_mdc_users_page", fake_load)
    r = client.get("/virtual/users?virtualEventId=77")
    assert r.status_code == 200
    assert seen.get("event_id") == 77
