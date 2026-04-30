"""WSGI entrypoint for production servers (e.g. waitress-serve wsgi:app)."""

from app import app

__all__ = ["app"]
