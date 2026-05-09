"""CDI-aware navigation: sidebar / module tabs respect JWT page allow-list."""

from __future__ import annotations

import os

import pytest

import h2s_cdi_auth as h2s_cdi_auth_mod


@pytest.fixture
def cdi_user_single_virtual_leaderboard(monkeypatch):
    """Non-admin with only ``virtual_leaderboard`` (module id from test conftest env)."""

    def _enforce(page=None):
        from flask import g as flask_g

        mid = (os.environ.get("H2S_CDI_MODULE_ID") or os.environ.get("JARVIS_MODULE_ID") or "").strip()
        flask_g.user = {
            "email": "limited@example.com",
            "name": "Limited",
            "isAdmin": False,
            "moduleAccess": {mid: ["virtual_leaderboard"]},
        }
        return None

    monkeypatch.setattr(h2s_cdi_auth_mod, "_enforce_request_auth", _enforce)


def test_nav_allowed_pages_no_user_returns_none(app_mod, flask_app):
    with flask_app.app_context():
        assert app_mod._pw_nav_allowed_pages() is None


def test_nav_filter_subnav_respects_allowlist(app_mod):
    rows = app_mod._pw_subnav_rows("virtual")
    allowed = {"virtual_leaderboard"}
    out = app_mod._pw_filter_subnav_rows(rows, allowed)
    assert [r["page_id"] for r in out] == ["virtual_leaderboard"]


def test_virtual_leaderboard_page_hides_other_modules_in_nav(
    client, app_mod, monkeypatch, cdi_user_single_virtual_leaderboard, no_admin_pw
):
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
            "rows": [],
            "total": 0,
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
    html = resp.get_data(as_text=True)
    assert "Bootcamps" not in html
    assert "In-person" not in html
    assert "Overview" not in html
    assert "/virtual/challenges" not in html
    assert "Submission leaderboard" in html
