"""
Master Audit Log (Prompt Wars).

Two coordinated layers, both mandatory at boot:

  * Layer A: PostgreSQL row triggers (in database/audit.sql) capture every
    INSERT / UPDATE / DELETE / TRUNCATE atomically inside the user's
    transaction, with field-level diffs, and an EVENT TRIGGER on CREATE TABLE
    auto-attaches the trigger to every newly created table - so any future
    module/entity is covered automatically without code changes.

    * Layer B: a Flask + SQLAlchemy middleware (this package) captures every
    HTTP request, SQL writes (SELECT/TXN optional via AUDIT_SQL_SELECTS), and
    auth events into
    a bounded in-process queue drained by an async batch worker thread that
    bulk-INSERTs into audit.audit_events.

Public entry point:

    from audit.db import create_engine
    from audit import install as install_audit

    engine = create_engine(DATABASE_URL, future=True)
    app = Flask(__name__, ...)
    install_audit(app, engine)
"""

from __future__ import annotations

import logging
import os
from typing import Any

from sqlalchemy.engine import Engine

from audit.context import (
    AuditContext,
    current_audit_context,
    set_principal,
    set_source,
)
from audit.sink import AsyncAuditSink
from audit.sql_listener import (
    install_engine_listeners,
    is_engine_instrumented,
)

log = logging.getLogger("audit")

_ACTIVE: dict[str, Any] = {
    "sink": None,
    "engine": None,
    "audit_engine": None,
    "app": None,
    "installed": False,
}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "y"}


def install(app, engine: Engine, *, audit_engine: Engine | None = None) -> AsyncAuditSink:
    """
    Idempotently wire the Master Audit Log into a Flask app + SQLAlchemy engine.

    - SQLAlchemy event listeners are attached to `engine` so every cursor
      execution is captured and per-request GUCs are pushed at connect time.
    - Flask before/after/teardown_request hooks are registered so every HTTP
      request is captured and a request_id flows down to DB triggers.
    - An async batched sink is started to drain captured events into
      audit.audit_events using a dedicated audit_engine.

    On first call: registers everything and starts the worker thread.
    On subsequent calls: returns the existing sink (no double registration).
    """
    if _ACTIVE["installed"]:
        return _ACTIVE["sink"]

    enabled = _env_bool("AUDIT_ENABLED", True)
    if not enabled:
        log.warning("audit: AUDIT_ENABLED=0 - audit log is DISABLED at this process")
        # Still register a no-op sink so callers can rely on the API
        sink = AsyncAuditSink(audit_engine or engine, enabled=False)
        _ACTIVE.update({"sink": sink, "engine": engine, "audit_engine": audit_engine or engine,
                        "app": app, "installed": True})
        return sink

    if audit_engine is None:
        audit_engine = engine

    sink = AsyncAuditSink(
        audit_engine,
        maxsize=_env_int("AUDIT_QUEUE_MAXSIZE", 100_000),
        flush_interval_ms=_env_int("AUDIT_FLUSH_INTERVAL_MS", 250),
        flush_batch=_env_int("AUDIT_FLUSH_BATCH", 500),
        block_ms=_env_int("AUDIT_QUEUE_BLOCK_MS", 500),
        drop_oldest=_env_bool("AUDIT_QUEUE_DROP_OLDEST", True),
    )
    sink.start()

    install_engine_listeners(engine, sink)

    from audit.flask_hooks import register_flask_hooks  # local import to avoid cycle

    register_flask_hooks(app, sink)

    # Best-effort DB self-test (warn-only). Operators wanting strict boot can
    # set AUDIT_STRICT_BOOT_CHECK=1 to fail fast instead.
    strict = _env_bool("AUDIT_STRICT_BOOT_CHECK", False)
    db_ok, db_err = _self_test_db(audit_engine)
    if not db_ok:
        msg = f"audit: DB self-test failed: {db_err}"
        if strict:
            sink.stop(timeout=1.0)
            raise RuntimeError(msg)
        log.warning("%s (continuing; events will retry on next flush)", msg)

    if not is_engine_instrumented(engine):
        msg = "audit: SQLAlchemy listeners failed to register"
        sink.stop(timeout=1.0)
        raise RuntimeError(msg)

    _ACTIVE.update({"sink": sink, "engine": engine, "audit_engine": audit_engine,
                    "app": app, "installed": True})
    log.info("audit: installed (queue=%d, flush=%dms/%d rows)",
             sink.queue.maxsize, int(sink.flush_interval * 1000), sink.flush_batch)
    return sink


def _self_test_db(engine: Engine) -> tuple[bool, str | None]:
    """Verify the audit schema and event trigger exist."""
    try:
        from sqlalchemy import text
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT
                      to_regclass('audit.audit_events')        AS events,
                      to_regclass('audit.audit_data_changes')  AS changes,
                      EXISTS (
                        SELECT 1 FROM pg_event_trigger
                        WHERE evtname = 'pw_audit_auto_attach' AND evtenabled <> 'D'
                      ) AS evtrg
                    """
                )
            ).fetchone()
        if row is None:
            return False, "no row returned"
        events_ok = row[0] is not None
        changes_ok = row[1] is not None
        evtrg_ok = bool(row[2])
        if not (events_ok and changes_ok and evtrg_ok):
            return False, (
                f"missing: audit_events={events_ok}, audit_data_changes={changes_ok}, "
                f"event_trigger={evtrg_ok} - apply database/audit.sql"
            )
        return True, None
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def install_for_script(
    engine: Engine,
    *,
    principal: str,
    source: str = "cli",
    email: str | None = None,
) -> AsyncAuditSink:
    """
    Same as install() but for non-Flask processes (CLI, ETL, cron jobs).

    Usage at the top of a script:

        from audit.db import create_engine
        from audit import install_for_script

        engine = create_engine(DATABASE_URL, future=True)
        install_for_script(engine, principal="system:my_etl", source="etl")
        # ... every engine.execute(...) from here on is audited ...
    """
    sink = install(app=_NoFlaskApp(), engine=engine)
    set_principal(principal, email=email, source=source)
    return sink


class _NoFlaskApp:
    """Minimal Flask-app stand-in for CLI use - swallow before/after_request hooks."""

    def before_request(self, fn):  # noqa: ANN001, ANN201
        return fn

    def after_request(self, fn):  # noqa: ANN001, ANN201
        return fn

    def teardown_request(self, fn):  # noqa: ANN001, ANN201
        return fn


def get_sink() -> AsyncAuditSink | None:
    return _ACTIVE.get("sink")


def shutdown(timeout: float = 5.0) -> None:
    """Stop the background worker (e.g. on test teardown or graceful exit)."""
    sink = _ACTIVE.get("sink")
    if sink is not None:
        sink.stop(timeout=timeout)
    _ACTIVE["installed"] = False
    _ACTIVE["sink"] = None


__all__ = [
    "install",
    "install_for_script",
    "shutdown",
    "get_sink",
    "set_principal",
    "set_source",
    "AuditContext",
    "current_audit_context",
]
