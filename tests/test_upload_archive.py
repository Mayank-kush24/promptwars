"""Tests for the upload archive helper and its wiring into the import endpoints.

These tests do not require a live PostgreSQL: the SQLAlchemy ``engine``
on the helper / Flask app is replaced with an in-memory recorder so we
can assert on the rows that ``upload_archive`` would receive.
"""

from __future__ import annotations

import hashlib
import io
import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from werkzeug.datastructures import FileStorage

from services import upload_archive as ua


class _RecorderConn:
    def __init__(self, store: dict) -> None:
        self._store = store

    def execute(self, stmt, params=None):
        sql = str(stmt)
        params = params or {}
        self._store["calls"].append((sql, dict(params)))
        if "INSERT INTO upload_archive" in sql and "RETURNING id" in sql:
            self._store["rows"].append(dict(params))
            new_id = self._store["next_id"]
            self._store["next_id"] += 1
            return _OneResult(new_id)
        if sql.startswith("UPDATE upload_archive"):
            target_id = params.get("id")
            updates = self._store.setdefault("updates", {}).setdefault(target_id, [])
            updates.append({k: v for k, v in params.items() if k != "id"})
            return _OneResult(None)
        return _OneResult(None)


class _OneResult:
    def __init__(self, value):
        self._value = value

    def one(self):
        return (self._value,)

    def scalar_one(self):
        return self._value


class FakeEngine:
    """Minimal stand-in for SQLAlchemy Engine used by upload_archive helpers."""

    def __init__(self) -> None:
        self.store: dict[str, Any] = {
            "calls": [],
            "rows": [],
            "updates": {},
            "next_id": 1,
        }

    @contextmanager
    def begin(self):
        yield _RecorderConn(self.store)

    @contextmanager
    def connect(self):
        yield _RecorderConn(self.store)


@pytest.fixture
def archive_root(tmp_path, monkeypatch):
    target = tmp_path / "uploads_archive"
    monkeypatch.setenv("UPLOAD_ARCHIVE_DIR", str(target))
    return target


@pytest.fixture
def fake_engine():
    return FakeEngine()


def _make_filestorage(content: bytes, filename: str, mimetype: str = "text/csv") -> FileStorage:
    return FileStorage(stream=io.BytesIO(content), filename=filename, content_type=mimetype)


# ---------- helper: archive_upload --------------------------------------


def test_archive_upload_writes_file_with_dated_path_and_correct_sha(
    archive_root, fake_engine, flask_app
):
    payload = b"email,name\nalice@example.com,Alice\n"
    fs = _make_filestorage(payload, "rsvps.csv")

    with flask_app.test_request_context("/admin/import"):
        archived = ua.archive_upload(
            fs,
            engine=fake_engine,
            module="in_person_rsvps",
            source_route="/admin/import",
            event_id=1,
        )

    assert archived.size_bytes == len(payload)
    assert archived.sha256 == hashlib.sha256(payload).hexdigest()
    assert archived.id == 1
    assert archived.original_name == "rsvps.csv"

    abs_path = Path(archived.absolute_path)
    assert abs_path.is_file()
    assert abs_path.read_bytes() == payload

    rel = Path(archived.stored_path)
    assert rel.parts[0] == "in_person_rsvps"
    assert rel.name.endswith("__rsvps.csv")
    assert "Z__" in rel.name

    sidecar = Path(str(abs_path) + ".meta.json")
    assert sidecar.is_file()
    meta = json.loads(sidecar.read_text())
    assert meta["sha256"] == archived.sha256
    assert meta["original_name"] == "rsvps.csv"
    assert meta["status"] == "received"
    assert meta["event_id"] == 1


def test_archive_upload_module_is_sanitized(archive_root, fake_engine, flask_app):
    fs = _make_filestorage(b"hello", "x.csv")
    with flask_app.test_request_context("/x"):
        archived = ua.archive_upload(
            fs, engine=fake_engine, module="In Person/MDC!", source_route="/x"
        )
    assert archived.module == "in_person_mdc"
    assert Path(archived.stored_path).parts[0] == "in_person_mdc"


def test_archive_upload_records_received_row_with_metadata(
    archive_root, fake_engine, flask_app
):
    fs = _make_filestorage(b"a,b\n1,2\n", "demo.csv")
    with flask_app.test_request_context("/admin/import/in-person/main-data-center"):
        ua.archive_upload(
            fs,
            engine=fake_engine,
            module="in_person_mdc",
            source_route="/admin/import/in-person/main-data-center",
            event_id=7,
        )

    rows = fake_engine.store["rows"]
    assert len(rows) == 1
    row = rows[0]
    assert row["module"] == "in_person_mdc"
    assert row["route"] == "/admin/import/in-person/main-data-center"
    assert row["orig"] == "demo.csv"
    assert row["size"] == len(b"a,b\n1,2\n")
    assert row["sha"] == hashlib.sha256(b"a,b\n1,2\n").hexdigest()
    assert row["event_id"] == 7
    assert row["mime"] == "text/csv"


def test_fresh_stream_yields_independent_reads(archive_root, fake_engine, flask_app):
    fs = _make_filestorage(b"col\nvalue\n", "x.csv")
    with flask_app.test_request_context("/x"):
        archived = ua.archive_upload(fs, engine=fake_engine, module="other")

    s1 = archived.fresh_stream()
    s2 = archived.fresh_stream()
    assert s1.read() == b"col\nvalue\n"
    assert s2.read() == b"col\nvalue\n"


# ---------- helper: mark_archive_status ---------------------------------


def test_mark_archive_status_updates_row(archive_root, fake_engine, flask_app):
    fs = _make_filestorage(b"x", "x.csv")
    with flask_app.test_request_context("/x"):
        archived = ua.archive_upload(fs, engine=fake_engine, module="other")

    ua.mark_archive_status(archived.id, "parsed", engine=fake_engine)
    ua.mark_archive_status(
        archived.id,
        "success",
        engine=fake_engine,
        rows_written=42,
        import_job_id=99,
    )
    ua.mark_archive_status(None, "failed", engine=fake_engine, error="ignored")

    updates = fake_engine.store["updates"][archived.id]
    statuses = [u["status"] for u in updates]
    assert statuses == ["parsed", "success"]
    assert updates[1]["rows"] == 42
    assert updates[1]["jid"] == 99


def test_mark_archive_status_rejects_unknown_status(archive_root, fake_engine):
    with pytest.raises(ValueError):
        ua.mark_archive_status(1, "weird", engine=fake_engine)


# ---------- end-to-end via Flask test client ----------------------------


def _patch_engine(monkeypatch, app_mod, fake_engine):
    """Point both the app module and the helper at the same fake engine."""
    monkeypatch.setattr(app_mod, "engine", fake_engine)


def test_in_person_mdc_import_archives_even_when_parse_fails(
    client, no_admin_pw, monkeypatch, app_mod, archive_root, fake_engine
):
    _patch_engine(monkeypatch, app_mod, fake_engine)

    def _boom(_stream, _name):
        raise ValueError("bad columns")

    monkeypatch.setattr(
        app_mod.etl_data_center, "parse_main_data_center_file", _boom
    )

    data = {
        "main_data_center": (io.BytesIO(b"col1,col2\nx,y\n"), "broken.csv"),
    }
    resp = client.post(
        "/api/import/in-person/main-data-center",
        data=data,
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "bad columns"

    rows = fake_engine.store["rows"]
    assert len(rows) == 1
    assert rows[0]["module"] == "in_person_mdc"

    archive_id = 1
    statuses = [u["status"] for u in fake_engine.store["updates"].get(archive_id, [])]
    assert "failed" in statuses


def test_virtual_mdc_import_archives_even_when_parse_fails(
    client, no_admin_pw, monkeypatch, app_mod, archive_root, fake_engine
):
    _patch_engine(monkeypatch, app_mod, fake_engine)
    monkeypatch.setattr(
        app_mod.etl_data_center,
        "parse_main_data_center_file",
        lambda *_a, **_k: (_ for _ in ()).throw(ValueError("nope")),
    )

    data = {
        "virtual_main_data_center": (io.BytesIO(b"x"), "vbad.csv"),
    }
    resp = client.post(
        "/api/import/virtual/main-data-center",
        data=data,
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400

    rows = fake_engine.store["rows"]
    assert len(rows) == 1
    assert rows[0]["module"] == "virtual_mdc"
    statuses = [u["status"] for u in fake_engine.store["updates"].get(1, [])]
    assert "failed" in statuses


def test_in_person_two_file_import_archives_both_uploads(
    client, no_admin_pw, monkeypatch, app_mod, archive_root, fake_engine
):
    _patch_engine(monkeypatch, app_mod, fake_engine)

    def _boom(_stream):
        raise ValueError("schema mismatch")

    monkeypatch.setattr(app_mod.etl_in_person, "parse_rsvps_csv", _boom)
    monkeypatch.setattr(
        app_mod.etl_in_person, "parse_submissions_csv", _boom
    )

    data = {
        "event_id": "1",
        "rsvps": (io.BytesIO(b"r1,r2\n"), "rsvps.csv"),
        "submissions": (io.BytesIO(b"s1,s2\n"), "subs.csv"),
    }
    resp = client.post(
        "/api/import/in-person",
        data=data,
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400

    rows = fake_engine.store["rows"]
    assert len(rows) == 2
    modules = sorted(r["module"] for r in rows)
    assert modules == ["in_person_rsvps", "in_person_submissions"]

    for archive_id in (1, 2):
        statuses = [
            u["status"] for u in fake_engine.store["updates"].get(archive_id, [])
        ]
        assert "failed" in statuses
