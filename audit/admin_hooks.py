"""
Auth event helpers - call these from admin login/logout endpoints.

These are the only two app-level write touch points (the rest is captured
automatically by SQL listeners and Flask hooks).
"""

from __future__ import annotations

import audit as _audit
from audit.context import current_audit_context


def _enqueue(action: str, **extra) -> None:
    sink = _audit.get_sink()
    if sink is None:
        return
    ctx = current_audit_context()
    sink.enqueue(
        {
            "action": action,
            "module": "admin",
            "entity": "session",
            "principal": ctx.principal,
            "principal_email": ctx.principal_email,
            "session_id": ctx.session_id,
            "client_ip": ctx.client_ip,
            "user_agent": ctx.user_agent,
            "source": ctx.source,
            "request_id": ctx.request_id,
            "extra": extra or None,
        }
    )


def log_login_success(*, principal: str = "admin", **extra) -> None:
    """Update the active context too so the rest of the request is attributed correctly."""
    ctx = current_audit_context()
    ctx.principal = principal
    _enqueue("LOGIN", **extra)


def log_login_failed(*, attempted: str | None = None, **extra) -> None:
    payload = dict(extra)
    if attempted:
        payload["attempted_principal"] = attempted
    _enqueue("LOGIN_FAILED", **payload)


def log_logout(**extra) -> None:
    _enqueue("LOGOUT", **extra)
