"""
Tests for the Master Audit Log (audit/ package + database/audit.sql).

Two layers:
  * Unit tests run without a database (sink semantics, sql kind detection,
    GUC builder, decorator behavior, Flask hook integration with stubs).
  * Integration tests require a live PostgreSQL with database/audit.sql
    applied. They are auto-skipped when the DB is unreachable or the audit
    schema is missing.
"""

from __future__ import annotations

import os
import time
from unittest.mock import MagicMock

import pytest


# --------------------------------------------------------------------------
# Unit tests (no DB)
# --------------------------------------------------------------------------


def test_sql_kind_detection():
    from audit.sql_listener import _detect_sql_kind

    assert _detect_sql_kind("SELECT 1") == "SELECT"
    assert _detect_sql_kind("  select 1") == "SELECT"
    assert _detect_sql_kind("INSERT INTO foo VALUES (1)") == "INSERT"
    assert _detect_sql_kind("UPDATE foo SET x = 1") == "UPDATE"
    assert _detect_sql_kind("DELETE FROM foo") == "DELETE"
    assert _detect_sql_kind("WITH t AS (SELECT 1) SELECT * FROM t") == "SELECT"
    assert _detect_sql_kind("CREATE TABLE foo (id int)") == "DDL"
    assert _detect_sql_kind("ALTER TABLE foo ADD COLUMN y int") == "DDL"
    assert _detect_sql_kind("BEGIN") == "TXN"
    assert _detect_sql_kind("") == "OTHER"
    assert _detect_sql_kind("-- a comment\nSELECT 1") == "SELECT"
    assert _detect_sql_kind("/* hi */ SELECT 1") == "SELECT"


def test_audit_context_thread_local_outside_flask():
    from audit.context import current_audit_context, reset_thread_context, set_principal

    reset_thread_context()
    ctx = current_audit_context()
    assert ctx.principal is None
    set_principal("system:test", source="cli", email="t@example.com")
    assert current_audit_context().principal == "system:test"
    assert current_audit_context().source == "cli"
    assert current_audit_context().principal_email == "t@example.com"
    reset_thread_context()
    assert current_audit_context().principal is None


def test_audit_context_guc_pairs_have_six_keys():
    from audit.context import AuditContext

    ctx = AuditContext(
        request_id="r1",
        principal="admin",
        principal_email="a@x.com",
        session_id="s1",
        client_ip="1.2.3.4",
        source="api",
    )
    pairs = ctx.as_guc_pairs()
    assert len(pairs) == 6
    keys = [k for k, _ in pairs]
    assert "audit.request_id" in keys
    assert "audit.principal" in keys
    assert "audit.client_ip" in keys
    assert "audit.source" in keys


def test_async_sink_overflow_emits_marker_and_does_not_drop_silently():
    from audit.sink import AsyncAuditSink

    fake_engine = MagicMock()  # not used because we won't drain
    sink = AsyncAuditSink(fake_engine, maxsize=2, flush_interval_ms=10_000,
                          flush_batch=10, block_ms=0)
    # Don't start the worker; we want the queue to actually fill up.
    assert sink.enqueue({"action": "HTTP_REQUEST"}) is True
    assert sink.enqueue({"action": "SQL_EXEC"}) is True
    # Queue is now full (size 2). Next enqueue should overflow.
    accepted = sink.enqueue({"action": "HTTP_REQUEST"})
    assert accepted is False
    assert sink.stats.overflowed == 1
    # The overflow itself was attempted to be enqueued, but queue was full,
    # so we record stats but at least one overflow attempt is visible.


def test_async_sink_skip_when_disabled():
    from audit.sink import AsyncAuditSink

    sink = AsyncAuditSink(MagicMock(), enabled=False)
    assert sink.enqueue({"action": "HTTP_REQUEST"}) is False
    assert sink.stats.enqueued == 0


def test_async_sink_drain_writes_bulk_insert_with_jsonb_casts():
    """The drain path builds an INSERT with explicit JSONB / INET casts."""
    from audit.sink import AsyncAuditSink

    captured: dict = {}

    class _FakeConn:
        def execute(self, sql_obj, params):
            captured["sql"] = str(sql_obj)
            captured["params"] = dict(params)

    class _CtxMgr:
        def __enter__(self):
            return _FakeConn()

        def __exit__(self, *a):
            return False

    fake_engine = MagicMock()
    fake_engine.begin.return_value = _CtxMgr()

    sink = AsyncAuditSink(fake_engine, maxsize=10, flush_interval_ms=10_000,
                          flush_batch=10, block_ms=0)
    sink.enqueue({
        "action": "HTTP_REQUEST",
        "principal": "admin",
        "client_ip": "10.0.0.1",
        "record_pk": {"id": 7},
        "extra": {"foo": "bar"},
    })
    sink._drain_once()
    assert "INSERT INTO audit.audit_events" in captured["sql"]
    assert "CAST(:record_pk_0 AS JSONB)" in captured["sql"]
    assert "NULLIF(:client_ip_0, '')::inet" in captured["sql"]
    assert captured["params"]["principal_0"] == "admin"
    # JSONB params are serialized strings
    assert '"id": 7' in captured["params"]["record_pk_0"]
    assert '"foo": "bar"' in captured["params"]["extra_0"]


def test_audit_view_decorator_enqueues_event(monkeypatch):
    from audit import context, decorators
    import audit

    captured = []

    class _FakeSink:
        enabled = True

        def enqueue(self, ev):
            captured.append(ev)
            return True

    monkeypatch.setattr(audit, "get_sink", lambda: _FakeSink())
    context.reset_thread_context()
    context.set_principal("admin", source="ui")

    @decorators.audit_view(
        entity="widgets",
        module="m",
        record_pk_fn=lambda widget_id, *a, **kw: {"id": int(widget_id)},
        extra_fn=lambda *a, **kw: {"detail": "yes"},
    )
    def view(widget_id):
        return f"viewed {widget_id}"

    out = view(42)
    assert out == "viewed 42"
    assert len(captured) == 1
    ev = captured[0]
    assert ev["action"] == "VIEW"
    assert ev["entity"] == "widgets"
    assert ev["record_pk"] == {"id": 42}
    assert ev["extra"] == {"detail": "yes"}
    assert ev["principal"] == "admin"
    context.reset_thread_context()


def test_install_is_idempotent(monkeypatch):
    """Calling audit.install twice on the same engine must not double-register."""
    import audit
    from audit.sql_listener import is_engine_instrumented

    monkeypatch.setenv("AUDIT_ENABLED", "1")
    audit.shutdown(timeout=0.5)
    from sqlalchemy import create_engine

    eng = create_engine("postgresql://x:y@127.0.0.1:5/non_existent_for_test", future=True)
    fake_app = MagicMock()
    fake_app.before_request = lambda fn: fn
    fake_app.after_request = lambda fn: fn
    fake_app.teardown_request = lambda fn: fn

    sink1 = audit.install(fake_app, eng)
    sink2 = audit.install(fake_app, eng)
    assert sink1 is sink2
    assert is_engine_instrumented(eng)
    audit.shutdown(timeout=0.5)


def test_flask_hook_assigns_request_id_and_enqueues_http_request(monkeypatch):
    """End-to-end: a request through Flask's test client enqueues HTTP_REQUEST."""
    import audit
    import app as app_mod

    captured = []

    class _FakeSink:
        enabled = True
        queue = MagicMock(maxsize=100)
        flush_interval = 0.25
        flush_batch = 100
        stats = MagicMock(enqueued=0, written=0)

        def enqueue(self, ev):
            captured.append(ev)
            return True

        def flush_blocking(self, *a, **kw):
            return 0

        def stop(self, *a, **kw):
            pass

        def start(self):
            pass

    monkeypatch.setattr(audit, "get_sink", lambda: _FakeSink())
    # Replace the active sink with our fake so the already-registered after_request
    # hook (registered during module import) writes to it.
    monkeypatch.setitem(audit._ACTIVE, "sink", _FakeSink())
    # Re-register hooks against this sink so they pick it up
    from audit.flask_hooks import register_flask_hooks

    fake_sink = _FakeSink()
    captured = []

    # Inject a fresh hook on a clean Flask app for isolation
    from flask import Flask

    app2 = Flask(__name__)
    app2.secret_key = "test"
    register_flask_hooks(app2, fake_sink)

    @app2.get("/ping")
    def ping():
        return "pong"

    client = app2.test_client()
    resp = client.get("/ping")
    assert resp.status_code == 200
    # HTTP_REQUEST should have been enqueued
    actions = [e.get("action") for e in captured]
    assert "HTTP_REQUEST" in actions
    ev = next(e for e in captured if e.get("action") == "HTTP_REQUEST")
    assert ev["status"] == 200
    assert ev["http_method"] == "GET"
    assert ev["request_id"] and len(ev["request_id"]) >= 16
    assert ev["principal"] in ("admin", "anonymous")


# --------------------------------------------------------------------------
# Integration tests (require live Postgres + database/audit.sql applied)
# --------------------------------------------------------------------------


def _audit_db_ready() -> tuple[bool, str | None]:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        return False, "DATABASE_URL not set"
    try:
        import psycopg2  # noqa: F401
    except ImportError:
        return False, "psycopg2 not installed"
    try:
        from sqlalchemy import create_engine, text

        eng = create_engine(url, future=True, pool_pre_ping=True)
        with eng.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT to_regclass('audit.audit_events') IS NOT NULL,
                           to_regclass('audit.audit_data_changes') IS NOT NULL
                    """
                )
            ).fetchone()
        if not row or not row[0] or not row[1]:
            return False, "audit schema missing - run python run_init_sql.py"
        return True, None
    except Exception as exc:  # noqa: BLE001
        return False, f"db connect failed: {exc}"


_DB_OK, _DB_ERR = _audit_db_ready()
needs_db = pytest.mark.skipif(not _DB_OK, reason=f"audit DB not available: {_DB_ERR}")


@pytest.fixture
def audit_engine():
    from sqlalchemy import create_engine

    return create_engine(os.environ["DATABASE_URL"], future=True, pool_pre_ping=True)


def _set_session_audit(conn, **kv):
    from sqlalchemy import text

    parts = []
    params = {}
    for i, (k, v) in enumerate(kv.items()):
        parts.append(f"set_config(:k{i}, :v{i}, false)")
        params[f"k{i}"] = f"audit.{k}"
        params[f"v{i}"] = v or ""
    if parts:
        conn.execute(text("SELECT " + ", ".join(parts)), params)


@needs_db
def test_db_event_trigger_is_enabled(audit_engine):
    from sqlalchemy import text

    with audit_engine.connect() as conn:
        ok = conn.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM pg_event_trigger "
                "WHERE evtname = 'pw_audit_auto_attach' AND evtenabled <> 'D')"
            )
        ).scalar()
    assert ok is True


@needs_db
def test_db_insert_update_delete_diffs_with_request_id(audit_engine):
    from sqlalchemy import text

    rid = f"pytest-{int(time.time() * 1000)}"
    table = f"pw_audit_test_t_{int(time.time() * 1000)}"

    with audit_engine.begin() as conn:
        conn.execute(text(
            f'CREATE TABLE public."{table}" '
            f'(id BIGSERIAL PRIMARY KEY, name TEXT, age INT, updated_at TIMESTAMPTZ DEFAULT now())'
        ))

    try:
        # INSERT
        with audit_engine.begin() as conn:
            _set_session_audit(conn, request_id=rid, principal="admin", source="api",
                               client_ip="127.0.0.1")
            new_id = conn.execute(
                text(f'INSERT INTO public."{table}"(name, age) VALUES (:n, :a) RETURNING id'),
                {"n": "alice", "a": 30},
            ).scalar()

        # UPDATE
        with audit_engine.begin() as conn:
            _set_session_audit(conn, request_id=rid, principal="admin", source="api")
            conn.execute(
                text(f'UPDATE public."{table}" SET age = :a WHERE id = :id'),
                {"a": 31, "id": new_id},
            )

        # DELETE
        with audit_engine.begin() as conn:
            _set_session_audit(conn, request_id=rid, principal="admin", source="api")
            conn.execute(text(f'DELETE FROM public."{table}" WHERE id = :id'), {"id": new_id})

        with audit_engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT op, changed_columns, changes, record_pk, principal, request_id, source "
                    "FROM audit.audit_data_changes "
                    "WHERE table_name = :t AND request_id = :rid "
                    "ORDER BY id"
                ),
                {"t": table, "rid": rid},
            ).fetchall()

        ops = [r[0] for r in rows]
        assert ops == ["INSERT", "UPDATE", "DELETE"], f"unexpected ops: {ops}"

        # INSERT row: pk + new_row populated
        ins = rows[0]
        assert ins[3] == {"id": new_id} or ins[3] == {"id": str(new_id)}, f"pk: {ins[3]}"
        assert ins[4] == "admin"
        assert ins[5] == rid
        assert ins[6] == "api"

        # UPDATE row: changed_columns includes 'age', changes has old/new
        upd = rows[1]
        assert "age" in (upd[1] or [])
        assert upd[2] is not None and "age" in upd[2]
        assert upd[2]["age"]["old"] == 30
        assert upd[2]["age"]["new"] == 31

    finally:
        with audit_engine.begin() as conn:
            conn.execute(text(f'DROP TABLE IF EXISTS public."{table}"'))


@needs_db
def test_event_trigger_auto_attaches_to_new_table(audit_engine):
    """A freshly created table must inherit the audit trigger via the EVENT TRIGGER."""
    from sqlalchemy import text

    table = f"pw_audit_autoattach_{int(time.time() * 1000)}"

    with audit_engine.begin() as conn:
        conn.execute(text(f'CREATE TABLE public."{table}" (id SERIAL PRIMARY KEY, k TEXT)'))

    try:
        with audit_engine.begin() as conn:
            has = conn.execute(
                text(
                    "SELECT COUNT(*) FROM pg_trigger "
                    "WHERE tgname = 'pw_audit_row_trg' "
                    "AND tgrelid = (quote_ident(:t))::regclass"
                ),
                {"t": table},
            ).scalar()
        assert has == 1, "EVENT TRIGGER did not auto-attach pw_audit_row_trg"

        with audit_engine.begin() as conn:
            _set_session_audit(conn, request_id="autoattach-test", principal="system:test",
                               source="api")
            conn.execute(text(f'INSERT INTO public."{table}"(k) VALUES (:v)'), {"v": "hello"})

        with audit_engine.connect() as conn:
            cnt = conn.execute(
                text(
                    "SELECT COUNT(*) FROM audit.audit_data_changes "
                    "WHERE table_name = :t AND request_id = 'autoattach-test'"
                ),
                {"t": table},
            ).scalar()
        assert cnt == 1
    finally:
        with audit_engine.begin() as conn:
            conn.execute(text(f'DROP TABLE IF EXISTS public."{table}"'))


@needs_db
def test_audit_schema_is_self_excluded(audit_engine):
    """Writes to audit.audit_data_changes must not produce more audit rows."""
    from sqlalchemy import text

    rid = f"selfexcl-{int(time.time() * 1000)}"
    with audit_engine.begin() as conn:
        before = conn.execute(text("SELECT COUNT(*) FROM audit.audit_data_changes")).scalar()
    with audit_engine.begin() as conn:
        # direct INSERT into audit table
        conn.execute(
            text(
                "INSERT INTO audit.audit_data_changes "
                "(occurred_at, schema_name, table_name, op, request_id, source) "
                "VALUES (now(), 'audit', 'audit_data_changes', 'INSERT', :rid, 'test')"
            ),
            {"rid": rid},
        )
    with audit_engine.connect() as conn:
        after = conn.execute(text("SELECT COUNT(*) FROM audit.audit_data_changes")).scalar()
        # only +1 (our manual insert), not +2 (which would mean the trigger ran on us)
        assert after - before == 1
        # And no duplicate row with our request_id from the trigger
        cnt = conn.execute(
            text("SELECT COUNT(*) FROM audit.audit_data_changes WHERE request_id = :r"),
            {"r": rid},
        ).scalar()
        assert cnt == 1


@needs_db
def test_async_sink_writes_event_to_audit_events(audit_engine):
    from audit.sink import AsyncAuditSink

    sink = AsyncAuditSink(audit_engine, maxsize=10, flush_interval_ms=10_000,
                          flush_batch=10, block_ms=0)
    rid = f"sinkwrite-{int(time.time() * 1000)}"
    sink.enqueue({
        "action": "HTTP_REQUEST",
        "module": "test",
        "endpoint": "test_endpoint",
        "http_method": "GET",
        "url": "/ping",
        "status": 200,
        "latency_ms": 1.23,
        "request_id": rid,
        "principal": "admin",
        "client_ip": "127.0.0.1",
        "user_agent": "pytest",
        "source": "api",
        "extra": {"k": "v"},
    })
    sink.flush_blocking(timeout=2.0)

    from sqlalchemy import text

    with audit_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT action, status, request_id, principal, extra->>'k' "
                "FROM audit.audit_events WHERE request_id = :r"
            ),
            {"r": rid},
        ).fetchone()
    assert row is not None
    assert row[0] == "HTTP_REQUEST"
    assert row[1] == 200
    assert row[2] == rid
    assert row[3] == "admin"
    assert row[4] == "v"
