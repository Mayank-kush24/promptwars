"""
Optional record-level VIEW / EXPORT decorator.

`SQL_EXEC` and `HTTP_REQUEST` events already cover everything automatically.
This decorator adds a high-signal entry such as VIEW or EXPORT that names
the entity being inspected and the record id (for direct record-level reads
like /api/in-person/main-data-center/registrations/<id> and CSV exports).
"""

from __future__ import annotations

from functools import wraps
from typing import Any, Callable

import audit as _audit
from audit.context import current_audit_context


def audit_view(
    *,
    entity: str,
    action: str = "VIEW",
    module: str | None = None,
    record_pk_fn: Callable[..., dict | None] | None = None,
    extra_fn: Callable[..., dict | None] | None = None,
):
    """
    Decorator factory. Logs an `action` event before invoking the wrapped view.

    record_pk_fn(*args, **kwargs) -> dict | None  - optional, builds record_pk
    extra_fn(*args, **kwargs)     -> dict | None  - optional, populates extra
    """

    def _wrap(fn: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(fn)
        def _inner(*args, **kwargs):
            try:
                pk = record_pk_fn(*args, **kwargs) if record_pk_fn else None
            except Exception:  # noqa: BLE001
                pk = None
            try:
                ex = extra_fn(*args, **kwargs) if extra_fn else None
            except Exception:  # noqa: BLE001
                ex = None
            sink = _audit.get_sink()
            if sink is not None:
                ctx = current_audit_context()
                sink.enqueue(
                    {
                        "action": action,
                        "module": module,
                        "entity": entity,
                        "record_pk": pk,
                        "request_id": ctx.request_id,
                        "principal": ctx.principal,
                        "principal_email": ctx.principal_email,
                        "session_id": ctx.session_id,
                        "client_ip": ctx.client_ip,
                        "user_agent": ctx.user_agent,
                        "source": ctx.source,
                        "extra": ex,
                    }
                )
            return fn(*args, **kwargs)

        return _inner

    return _wrap
