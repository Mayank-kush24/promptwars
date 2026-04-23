"""
Per-request audit context (principal, request_id, source, IP, etc.).

Stored on Flask `g` when inside a request context, with a thread-local fallback
for background scripts (ETL, CLI). Helpers here are read-only from the rest of
the package; mutation is done in flask_hooks (HTTP) and admin_hooks (auth) and
by callers in scripts via `set_principal()`.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any


@dataclass
class AuditContext:
    request_id: str | None = None
    principal: str | None = None
    principal_email: str | None = None
    session_id: str | None = None
    client_ip: str | None = None
    user_agent: str | None = None
    source: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def as_guc_pairs(self) -> list[tuple[str, str]]:
        return [
            ("audit.request_id", self.request_id or ""),
            ("audit.principal", self.principal or ""),
            ("audit.principal_email", self.principal_email or ""),
            ("audit.session_id", self.session_id or ""),
            ("audit.client_ip", self.client_ip or ""),
            ("audit.source", self.source or ""),
        ]


_THREAD = threading.local()


def _thread_ctx() -> AuditContext:
    ctx = getattr(_THREAD, "ctx", None)
    if ctx is None:
        ctx = AuditContext()
        _THREAD.ctx = ctx
    return ctx


def current_audit_context() -> AuditContext:
    """
    Return the active AuditContext.

    Inside a Flask request: returns flask.g.audit_ctx (creating it lazily).
    Outside Flask (scripts, worker threads): returns a thread-local context.
    """
    try:
        from flask import g, has_request_context  # local import to avoid hard dep at import time

        if has_request_context():
            ctx = getattr(g, "audit_ctx", None)
            if ctx is None:
                ctx = AuditContext()
                g.audit_ctx = ctx
            return ctx
    except Exception:  # noqa: BLE001
        pass
    return _thread_ctx()


def set_principal(
    principal: str,
    *,
    email: str | None = None,
    source: str | None = None,
    session_id: str | None = None,
) -> None:
    """
    Set principal/source/etc. on the active context.

    Use this in CLI / ETL / background scripts to set
    `principal='system:<script-name>'` and `source='etl'` (or 'cli', 'background').
    """
    ctx = current_audit_context()
    ctx.principal = principal
    if email is not None:
        ctx.principal_email = email
    if source is not None:
        ctx.source = source
    if session_id is not None:
        ctx.session_id = session_id


def set_source(source: str) -> None:
    ctx = current_audit_context()
    ctx.source = source


def reset_thread_context() -> None:
    """Clear the thread-local context (helpful between unit tests)."""
    if hasattr(_THREAD, "ctx"):
        delattr(_THREAD, "ctx")
