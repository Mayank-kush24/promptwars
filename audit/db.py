"""
Thin wrapper around `sqlalchemy.create_engine` that auto-instruments the
returned engine for audit (when called after `audit.install` has been wired,
listeners are added at install time; for engines created later, callers can
also use `install_engine_listeners`).

Currently the wrapper is intentionally minimal - the actual listener
attachment happens in `audit.install(app, engine)` so the install/sink/engine
relationship is explicit. We still export this module so the codebase has a
single import path that we can swap in if we ever auto-attach at create time
(without changing call sites).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import create_engine as _sa_create_engine
from sqlalchemy.engine import Engine


def create_engine(url: str, **kwargs: Any) -> Engine:
    """
    Drop-in replacement for `sqlalchemy.create_engine`.

    For now this returns a plain SQLAlchemy engine; `audit.install(app, engine)`
    must be called once at app startup to attach the SQL listeners. ETL/CLI
    scripts that create their own engine should call
    `audit.sql_listener.install_engine_listeners(engine, audit.get_sink())`
    after `audit.install(app, engine)` was called in the same process.
    """
    return _sa_create_engine(url, **kwargs)
