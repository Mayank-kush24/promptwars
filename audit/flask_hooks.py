"""
Flask before/after/teardown_request hooks.

These are mandatory at the framework level - registered once via
audit.install(app, engine), they fire for EVERY route (no per-route opt-in,
no way for a developer to bypass).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from uuid import uuid4

from flask import Flask, g, request, session

from audit.context import AuditContext
from audit.sink import AsyncAuditSink

log = logging.getLogger("audit.flask")

_REGISTERED_FLAG = "_pw_audit_flask_registered"


def _resolve_principal() -> tuple[str, str | None]:
    """
    CDI portal JWT (g.user) when present; else legacy session admin; else anonymous.
    Returns (principal, session_id).
    """
    try:
        user = getattr(g, "user", None)
        if isinstance(user, dict) and user.get("email"):
            email = str(user.get("email") or "").strip() or "unknown"
            if user.get("isAdmin"):
                return (f"admin:{email}", _sid_from_cookie("h2s_cdi_session"))
            return (email, _sid_from_cookie("h2s_cdi_session"))
    except Exception:  # noqa: BLE001
        pass
    try:
        is_admin = bool(session.get("admin"))
    except Exception:  # noqa: BLE001
        is_admin = False
    sid = _sid_from_cookie(getattr(request, "session_cookie_name", "session"))
    return ("admin" if is_admin else "anonymous", sid)


def _sid_from_cookie(name: str) -> str | None:
    try:
        cookie = request.cookies.get(name) or ""
        if cookie:
            from hashlib import blake2s
            return blake2s(cookie.encode("utf-8"), digest_size=8).hexdigest()
    except Exception:  # noqa: BLE001
        pass
    return None


def _resolve_client_ip() -> str | None:
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        # take the left-most (original client)
        return xff.split(",")[0].strip()
    real = request.headers.get("X-Real-IP")
    if real:
        return real.strip()
    return request.remote_addr


def _resolve_source() -> str:
    path = request.path or ""
    if path.startswith("/api/"):
        return "api"
    return "ui"


def _resolve_module() -> str | None:
    path = request.path or ""
    if path.startswith("/in-person") or "/in-person/" in path:
        return "in_person"
    if path.startswith("/virtual"):
        return "virtual"
    if path.startswith("/admin"):
        return "admin"
    if path.startswith("/api/"):
        return "api"
    if path.startswith("/static/"):
        return "static"
    return "overview"


def register_flask_hooks(app: Flask, sink: AsyncAuditSink) -> None:
    if getattr(app, _REGISTERED_FLAG, False):
        return

    @app.before_request
    def _audit_before():  # noqa: ANN202
        ctx = AuditContext()
        ctx.request_id = uuid4().hex
        principal, sid = _resolve_principal()
        ctx.principal = principal
        ctx.session_id = sid
        ctx.client_ip = _resolve_client_ip()
        ctx.user_agent = (request.headers.get("User-Agent") or "")[:512]
        ctx.source = _resolve_source()
        g.audit_ctx = ctx
        g.audit_t0 = time.perf_counter()
        g.audit_started_at = datetime.now(timezone.utc)

    @app.after_request
    def _audit_after(response):  # noqa: ANN202
        try:
            ctx = getattr(g, "audit_ctx", None) or AuditContext()
            t0 = getattr(g, "audit_t0", None)
            latency_ms = (time.perf_counter() - t0) * 1000.0 if t0 else None
            sink.enqueue(
                {
                    "occurred_at": getattr(g, "audit_started_at", None) or datetime.now(timezone.utc),
                    "action": "HTTP_REQUEST",
                    "module": _resolve_module(),
                    "entity": None,
                    "http_method": request.method,
                    "url": request.full_path if request.query_string else request.path,
                    "endpoint": request.endpoint,
                    "status": int(response.status_code) if response is not None else None,
                    "latency_ms": latency_ms,
                    "request_id": ctx.request_id,
                    "principal": ctx.principal,
                    "principal_email": ctx.principal_email,
                    "session_id": ctx.session_id,
                    "client_ip": ctx.client_ip,
                    "user_agent": ctx.user_agent,
                    "source": ctx.source,
                    "extra": {"response_size": int(response.calculate_content_length() or 0)
                              if response is not None else None},
                }
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("audit: after_request hook failed: %s", exc)
        return response

    @app.teardown_request
    def _audit_teardown(exc):  # noqa: ANN001, ANN202
        if exc is None:
            return
        try:
            ctx = getattr(g, "audit_ctx", None) or AuditContext()
            t0 = getattr(g, "audit_t0", None)
            latency_ms = (time.perf_counter() - t0) * 1000.0 if t0 else None
            sink.enqueue(
                {
                    "action": "HTTP_ERROR",
                    "module": _resolve_module() if request else None,
                    "http_method": getattr(request, "method", None),
                    "url": getattr(request, "path", None),
                    "endpoint": getattr(request, "endpoint", None),
                    "status": 500,
                    "latency_ms": latency_ms,
                    "request_id": ctx.request_id,
                    "principal": ctx.principal,
                    "session_id": ctx.session_id,
                    "client_ip": ctx.client_ip,
                    "user_agent": ctx.user_agent,
                    "source": ctx.source,
                    "extra": {"error": str(exc)[:500]},
                }
            )
        except Exception:  # noqa: BLE001
            pass

    setattr(app, _REGISTERED_FLAG, True)
