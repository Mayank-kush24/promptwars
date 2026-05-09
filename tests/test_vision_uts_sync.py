"""Tests for Vision UTS → virtual MDC mapping and sync helpers."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from services.vision_uts_sync import (
    extract_registration_records,
    map_registration_row,
    run_virtual_mdc_vision_uts_sync,
)


def test_extract_registration_records_top_level_list():
    rows = [{"email": "a@b.com"}, {"email": "c@d.com"}]
    assert extract_registration_records(rows) == rows


def test_extract_registration_records_data_key():
    payload = {"data": [{"email": "x@y.com"}]}
    assert extract_registration_records(payload) == [{"email": "x@y.com"}]


def test_extract_registration_records_event_nested():
    payload = {"event": {"registrations": [{"email": "n@o.com"}]}}
    assert extract_registration_records(payload) == [{"email": "n@o.com"}]


def test_extract_registration_records_tabular_first_row_headers():
    payload = {
        "success": True,
        "message": "ok",
        "data": [
            ["email", "full_name"],
            ["a@b.com", "Alice"],
            ["c@d.com", "Bob"],
        ],
    }
    out = extract_registration_records(payload)
    assert out == [
        {"email": "a@b.com", "full_name": "Alice"},
        {"email": "c@d.com", "full_name": "Bob"},
    ]


def test_extract_registration_records_tabular_explicit_headers():
    payload = {
        "columns": ["email", "name"],
        "data": [
            ["x@y.com", "X"],
            ["p@q.com", "P"],
        ],
    }
    out = extract_registration_records(payload)
    assert out == [
        {"email": "x@y.com", "name": "X"},
        {"email": "p@q.com", "name": "P"},
    ]


def test_map_registration_row_minimal():
    row, err = map_registration_row({"email": "  User@Example.com  "})
    assert err is None
    assert row is not None
    assert row["email"] == "User@Example.com"


def test_map_registration_row_missing_email():
    row, err = map_registration_row({"full_name": "Nobody"})
    assert row is None
    assert err == "missing email"


def test_run_virtual_mdc_vision_uts_sync_skipped_when_lock_held(app_mod, monkeypatch):
    """Second connection cannot acquire the same advisory lock while first holds it."""
    engine = app_mod.engine
    try:
        with engine.connect() as probe:
            probe.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001
        pytest.skip("PostgreSQL not available")

    def fetcher():
        return {"data": [{"email": "lock-test@example.com", "full_name": "Lock"}]}

    with engine.connect() as c1:
        got = c1.execute(
            text("SELECT pg_try_advisory_lock(:k1, :k2)"),
            {"k1": 1_853_272_190, "k2": 90_010_001},
        ).scalar()
        assert bool(got) is True
        try:
            out = run_virtual_mdc_vision_uts_sync(
                engine,
                2,
                triggered_by="test",
                fetch_json=fetcher,
                invalidate_caches=None,
            )
            assert out["skipped_due_to_lock"] is True
            assert out["status"] == "skipped"
        finally:
            c1.execute(text("SELECT pg_advisory_unlock(:k1, :k2)"), {"k1": 1_853_272_190, "k2": 90_010_001})
            c1.commit()
