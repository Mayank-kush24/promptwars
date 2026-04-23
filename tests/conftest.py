"""Shared pytest fixtures for the Prompt Wars Flask app."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Disable audit sink in unit tests by default. Tests that exercise audit
# behavior opt back in with the `audit_enabled` fixture.
os.environ.setdefault("AUDIT_ENABLED", "0")

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Force a deterministic config before importing the app module.
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:5432/promptwars_test")
os.environ.setdefault("SESSION_SECRET", "test-secret")
os.environ.setdefault("FLASK_DEBUG", "0")
os.environ.setdefault("FLASK_USE_RELOADER", "0")

import app as app_module  # noqa: E402

# Shared stub for Main Data Center dashboard blocks (in-person + virtual pages).
MDC_PAGE_STUB: dict = {
    "error": None,
    "total_registrations": 42,
    "with_attendance_city": 30,
    "skip_attendance_city": False,
    "distinct_countries": 2,
    "distinct_states": 5,
    "attendance_cities": [{"city": "Mumbai", "count": 18}, {"city": "Delhi", "count": 12}],
    "utm_sources": [{"source": "google", "count": 22}, {"source": "(none)", "count": 20}],
    "last_updated": "22-04-2026 12:00:00",
    "pill_top_city": "Mumbai",
    "pill_top_city_count": 18,
    "pill_top_state": "Maharashtra",
    "pill_top_state_count": 20,
    "average_age": 24.5,
    "with_dob_count": 30,
    "registrations_last_7_days": 8,
    "timeline_labels": ["20-04-2026", "21-04-2026", "22-04-2026"],
    "timeline_counts": [5, 12, 8],
    "hourly_counts": [0, 0, 0, 0, 0, 0, 1, 2, 4, 8, 6, 3, 2, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    "state_distribution": [{"name": "Maharashtra", "value": 20}, {"name": "Karnataka", "value": 12}],
    "city_pivot": [{"city": "Mumbai", "count": 18}, {"city": "Delhi", "count": 12}],
    "gender_breakdown": [{"gender": "Male", "count": 25}, {"gender": "Female", "count": 17}],
    "top_occupations": [{"occupation": "Student", "count": 10}, {"occupation": "Professional", "count": 8}],
}


@pytest.fixture
def flask_app():
    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    yield app_module.app


@pytest.fixture
def client(flask_app):
    return flask_app.test_client()


@pytest.fixture
def app_mod():
    """Convenience handle to the imported `app` module for monkeypatching."""
    return app_module


@pytest.fixture
def no_admin_pw(monkeypatch, app_mod):
    """Disable admin password so admin routes don't redirect."""
    monkeypatch.setattr(app_mod, "ADMIN_PASSWORD", "")
    return app_mod


@pytest.fixture
def overview_stub(monkeypatch, app_mod):
    """Replace the overview loader with a deterministic stub."""
    _mdc_brief = {
        "total_fmt": "0",
        "last7_fmt": "0",
        "top_city": "—",
        "top_city_count_fmt": "0",
        "top_state": "—",
        "top_state_count_fmt": "0",
        "average_age": None,
        "error": None,
    }
    payload = {
        "total_registrations_fmt": "120",
        "submissions_fmt": "45",
        "credits_fmt": "1.2k",
        "in_person_rsvps_fmt": "100",
        "in_person_submissions_fmt": "45",
        "in_person_conversion_fmt": "45.0%",
        "virtual_registrations_fmt": "20",
        "live_challenges_fmt": "1",
        "mdc_total_fmt": "120",
        "mdc_last7_fmt": "12",
        "mdc_in_person": dict(_mdc_brief, total_fmt="100", last7_fmt="10", top_city="Pune", top_state="Maharashtra"),
        "mdc_virtual": dict(_mdc_brief, total_fmt="20", last7_fmt="2", top_city="Bengaluru", top_state="Karnataka"),
    }
    monkeypatch.setattr(app_mod, "_fetch_overview_stats", lambda *_a, **_k: dict(payload))
    return payload


@pytest.fixture
def funnel_stub(monkeypatch, app_mod):
    """Stubs Main Data Center stats for /in-person page tests."""
    monkeypatch.setattr(app_mod, "_load_mdc_stats", lambda _eid, *, mode="in_person": dict(MDC_PAGE_STUB))


@pytest.fixture
def flush_audit(app_mod):
    """
    Synchronously drain the audit sink so tests can assert on what was enqueued.
    Returns the active sink (or None if audit is disabled).
    """
    import audit

    sink = audit.get_sink()
    if sink is None:
        yield None
        return
    yield sink
    try:
        sink.flush_blocking(timeout=2.0)
    except Exception:
        pass


@pytest.fixture
def virtual_stub(monkeypatch, app_mod):
    leaderboard = {
        "scope": {"challenge_id": 1, "virtual_event_id": 2},
        "rows": [
            {"rank": 1, "participant_id": 11, "display_name": "Alice", "score": 100.0},
            {"rank": 2, "participant_id": 12, "display_name": "Bob", "score": 80.0},
        ],
        "error": None,
    }
    distribution = {"scope": {"challenge_id": 1}, "bins": [{"low": 0, "high": 50, "count": 1}, {"low": 50, "high": 100, "count": 1}], "min": 0, "max": 100, "error": None}
    bins = list(distribution["bins"])
    monkeypatch.setattr(
        app_mod,
        "_load_virtual_bundle",
        lambda *_a, **_k: (dict(leaderboard), dict(distribution), list(bins)),
    )
    def _mdc_for_mode(_eid, *, mode="in_person"):
        d = dict(MDC_PAGE_STUB)
        if mode == "virtual":
            d["with_attendance_city"] = 0
            d["attendance_cities"] = []
            d["skip_attendance_city"] = True
        else:
            d["skip_attendance_city"] = False
        return d

    monkeypatch.setattr(app_mod, "_load_mdc_stats", _mdc_for_mode)

    challenges_brief = [
        {
            "id": 101,
            "title": "Sprint Alpha",
            "opens_at": None,
            "closes_at": None,
            "status": "live",
        },
        {
            "id": 102,
            "title": "Sprint Beta",
            "opens_at": None,
            "closes_at": None,
            "status": "draft",
        },
    ]
    monkeypatch.setattr(
        app_mod, "_load_virtual_challenges_brief", lambda *_a, **_k: list(challenges_brief)
    )
    return leaderboard, distribution, bins


@pytest.fixture
def challenges_stub(monkeypatch, app_mod):
    """Stubs the challenge management helpers for /virtual/challenges tests."""
    challenges = [
        {
            "id": 101,
            "title": "Sprint Alpha",
            "description": "First weekly sprint",
            "slug": "alpha",
            "opens_at": None,
            "closes_at": None,
            "status": "live",
            "created_at": None,
            "updated_at": None,
            "eligible_count": 12,
            "total_registrations": 50,
        },
        {
            "id": 102,
            "title": "Sprint Beta",
            "description": None,
            "slug": None,
            "opens_at": None,
            "closes_at": None,
            "status": "draft",
            "created_at": None,
            "updated_at": None,
            "eligible_count": 0,
            "total_registrations": 50,
        },
    ]
    monkeypatch.setattr(app_mod, "_load_virtual_challenges", lambda *_a, **_k: list(challenges))
    monkeypatch.setattr(
        app_mod,
        "_get_virtual_challenge",
        lambda cid, **_k: next((dict(c) for c in challenges if c["id"] == int(cid)), None),
    )
    return challenges
