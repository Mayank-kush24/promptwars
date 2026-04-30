"""
SQLAlchemy engine event listeners.

We hook three engine-level events on the application's main `engine` so every
SQL execution is captured (regardless of which route or ETL or background job
issued it - no per-module opt-in is possible to bypass):

  * engine_connect:        push per-context GUCs into the Postgres session so
                           the row trigger sees `audit.request_id`,
                           `audit.principal`, `audit.client_ip`, etc.
  * before_cursor_execute: stash a perf-counter start time on the exec context.
  * after_cursor_execute:  enqueue a SQL_EXEC event with kind, latency, rowcount,
                           and a truncated statement.
  * handle_error:          enqueue a SQL_ERROR event.

The audit sink uses its own engine (no listeners attached) so writes to the
audit schema do not loop back through us. We additionally guard against any
GUC-sync statement that we ourselves issued.
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import Engine

from audit.context import current_audit_context
from audit.sink import AsyncAuditSink

log = logging.getLogger("audit.sql")

_INSTRUMENTED_FLAG = "_pw_audit_instrumented"

_GUC_SYNC_MARKER = "/* pw_audit_set_guc */"

_KIND_RE = re.compile(r"^\s*(--[^\n]*\n|/\*.*?\*/\s*)*\s*([A-Za-z]+)", re.DOTALL)


def _audit_sql_selects_enabled() -> bool:
    """When False (default), skip app-layer audit for SELECT/TXN — huge read-path savings."""
    raw = os.environ.get("AUDIT_SQL_SELECTS", "")
    return raw.strip().lower() in {"1", "true", "yes", "on", "y"}


def _detect_sql_kind(stmt: str) -> str:
    if not stmt:
        return "OTHER"
    m = _KIND_RE.match(stmt)
    if not m:
        return "OTHER"
    word = m.group(2).upper()
    if word in {"SELECT", "INSERT", "UPDATE", "DELETE", "MERGE", "TRUNCATE", "WITH"}:
        return "SELECT" if word == "WITH" else word
    if word in {"CREATE", "ALTER", "DROP", "GRANT", "REVOKE", "COMMENT"}:
        return "DDL"
    if word in {"BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT", "RELEASE"}:
        return "TXN"
    return word or "OTHER"


def is_engine_instrumented(engine: Engine) -> bool:
    return bool(getattr(engine, _INSTRUMENTED_FLAG, False))


def _push_gucs_dbapi(dbapi_conn) -> None:
    """
    Push the active AuditContext into the Postgres session via set_config(),
    using a RAW DBAPI cursor so SQLAlchemy's transaction tracker is bypassed.

    Why raw DBAPI: if we used `connection.exec_driver_sql(...)` here, the
    SQLAlchemy Core Connection would autobegin a transaction. The next time
    user code calls `engine.begin()` on the same checked-out connection,
    SQLAlchemy raises:
        "This connection has already initialized a SQLAlchemy Transaction()
         object via begin() or autobegin; can't call begin() here..."
    By going through the DBAPI cursor we sidestep that bookkeeping entirely.
    set_config(name, value, is_local=false) sets the value at session scope,
    which is what we want: it persists across statements within this
    checkout, and the next checkout will overwrite it.
    """
    ctx = current_audit_context()
    pairs = ctx.as_guc_pairs()
    select_parts: list[str] = []
    params: list[Any] = []
    for k, v in pairs:
        select_parts.append("set_config(%s, %s, false)")
        params.append(k)
        params.append(v or "")
    sql = _GUC_SYNC_MARKER + " SELECT " + ", ".join(select_parts)
    try:
        cur = dbapi_conn.cursor()
        try:
            cur.execute(sql, params)
            # Drain the result to keep the cursor in a clean state.
            try:
                cur.fetchall()
            except Exception:  # noqa: BLE001
                pass
        finally:
            cur.close()
    except Exception as exc:  # noqa: BLE001
        # never break a request because we couldn't push GUCs
        log.debug("audit: push_gucs failed: %s", exc)


def install_engine_listeners(engine: Engine, sink: AsyncAuditSink) -> None:
    """Idempotently attach SQL execution listeners to `engine`."""
    if is_engine_instrumented(engine):
        return

    @event.listens_for(engine, "checkout")
    def _on_checkout(dbapi_conn, connection_record, connection_proxy):  # noqa: ANN001
        # Raw DBAPI hook - does NOT trigger SQLAlchemy autobegin, so the
        # caller's subsequent `engine.begin()` / `engine.connect()` works
        # normally. Fires every time a connection is checked out from the
        # pool, so each request/transaction gets the current context.
        _push_gucs_dbapi(dbapi_conn)

    @event.listens_for(engine, "before_cursor_execute")
    def _on_before(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001, PLR0913
        if context is not None:
            try:
                context._pw_audit_t0 = time.perf_counter()
            except Exception:  # noqa: BLE001
                pass

    @event.listens_for(engine, "after_cursor_execute")
    def _on_after(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001, PLR0913
        try:
            if statement and _GUC_SYNC_MARKER in statement:
                return  # do not record our own GUC sync statements
            if statement and ("audit.audit_events" in statement
                              or "audit.audit_data_changes" in statement):
                return  # never audit writes to the audit schema itself
            t0 = getattr(context, "_pw_audit_t0", None) if context is not None else None
            latency_ms = (time.perf_counter() - t0) * 1000.0 if t0 else None
            kind = _detect_sql_kind(statement or "")
            if not _audit_sql_selects_enabled() and kind in ("SELECT", "TXN"):
                return
            ctx = current_audit_context()
            try:
                rc = cursor.rowcount
            except Exception:  # noqa: BLE001
                rc = None
            sink.enqueue(
                {
                    "action": "SQL_EXEC",
                    "sql_kind": kind,
                    "sql_statement": (statement or "")[:2048],
                    "rowcount": rc if rc is not None and rc >= 0 else None,
                    "latency_ms": latency_ms,
                    "request_id": ctx.request_id,
                    "principal": ctx.principal,
                    "principal_email": ctx.principal_email,
                    "session_id": ctx.session_id,
                    "client_ip": ctx.client_ip,
                    "user_agent": ctx.user_agent,
                    "source": ctx.source,
                }
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("audit: after_cursor_execute hook failed: %s", exc)

    @event.listens_for(engine, "handle_error")
    def _on_error(exc_context):  # noqa: ANN001
        try:
            stmt = str(getattr(exc_context, "statement", "") or "")
            if _GUC_SYNC_MARKER in stmt:
                return
            ctx = current_audit_context()
            sink.enqueue(
                {
                    "action": "SQL_ERROR",
                    "sql_kind": _detect_sql_kind(stmt),
                    "sql_statement": stmt[:2048],
                    "status": 500,
                    "request_id": ctx.request_id,
                    "principal": ctx.principal,
                    "session_id": ctx.session_id,
                    "client_ip": ctx.client_ip,
                    "user_agent": ctx.user_agent,
                    "source": ctx.source,
                    "extra": {"error": str(exc_context.original_exception)[:500]},
                }
            )
        except Exception:  # noqa: BLE001
            pass

    setattr(engine, _INSTRUMENTED_FLAG, True)
