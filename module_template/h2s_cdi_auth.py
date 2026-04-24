"""
h2s_cdi_auth.py — Reusable Hack2skill Central Data Intelligence Level 2 RBAC middleware.

SETUP (copy this file into any new module app, then):

  1. Install dependencies:
       pip install PyJWT python-dotenv requests

  2. Set these env vars in your .env (legacy JARVIS_* names still work):
       H2S_CDI_JWT_SECRET          = <same as H2S_CDI_JWT_SECRET in portal .env>
       H2S_CDI_MODULE_ID           = myapp          (slug registered in the portal)
       H2S_CDI_URL                 = http://h2s.tech (portal URL; may be localhost if H2S_CDI_INTERNAL_URL is set)
       H2S_CDI_PUBLIC_URL         = optional — force browser links if not behind CDI nginx proxy headers
       H2S_CDI_REGISTRATION_SECRET = <same as MODULE_REGISTRATION_SECRET in portal .env>

  3. In your Flask app.py:
       from werkzeug.middleware.proxy_fix import ProxyFix
       app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

  4. Register global auth once (recommended):
       register_h2s_cdi_auth(
           app,
           public_paths=("/static", "/favicon.ico", "/health", "/login", "/logout"),
           path_page_rules=[
               ("/dashboard", "dashboard"),
               ("/reports", "reports"),
           ],
           default_page=None,  # JWT only for paths not matched by any rule (e.g. "/")
       )
       Longer prefixes win over shorter ones. Flask's static route is skipped when
       skip_static=True (default).

  5. Optional: keep @h2s_cdi_auth_required(page="x") on specific routes for a
     different page_id than path_page_rules would assign (advanced / migration).

  6. Call register_with_portal() at startup.

HOW IT WORKS:
  - The portal issues ONE unified JWT (h2s_cdi_session cookie) at login time.
  - When a user opens a module from the portal, the cookie is refreshed with
    the latest page permissions and the browser is redirected to the app.
  - Global before_request reads the cookie, verifies the signature, sets g.user,
    and checks moduleAccess[MODULE_ID] for the resolved page id.
  - When the token expires, the user is sent to /auth/refresh on the portal.
"""
from __future__ import annotations

import os
from functools import wraps

import jwt
import requests
from dotenv import load_dotenv
from flask import Flask, g, has_request_context, make_response, redirect, request

load_dotenv()


def _env_first(*names: str, default: str = "") -> str:
    for n in names:
        v = os.environ.get(n)
        if v is not None and str(v).strip() != "":
            return v
    return default


_JWT_SECRET = _env_first("H2S_CDI_JWT_SECRET", "JARVIS_JWT_SECRET")
_MODULE_ID = _env_first("H2S_CDI_MODULE_ID", "JARVIS_MODULE_ID")
_PORTAL_URL = _env_first("H2S_CDI_URL", "JARVIS_URL", default="http://localhost:5050").rstrip("/")
_internal = _env_first("H2S_CDI_INTERNAL_URL", "JARVIS_INTERNAL_URL")
_INTERNAL_URL = (_internal or _PORTAL_URL).rstrip("/")
_REGISTRATION_SECRET = _env_first(
    "H2S_CDI_REGISTRATION_SECRET",
    "JARVIS_REGISTRATION_SECRET",
)

_COOKIE_NAME = "h2s_cdi_session"

_PUBLIC_BROWSER_URL = _env_first("H2S_CDI_PUBLIC_URL", "JARVIS_PUBLIC_URL").rstrip("/")


def _browser_portal_base() -> str:
    """
    Portal origin for browser navigation (links and redirects).

    Behind the CDI nginx JWT proxy, ``X-Forwarded-Prefix`` is set and the client
    host is in ``X-Forwarded-Host`` / ``Host``. Then we use that origin so links
    work even when ``H2S_CDI_URL`` is ``localhost`` for registration only.

    Set ``H2S_CDI_PUBLIC_URL`` (or ``JARVIS_PUBLIC_URL``) to force this value.
    """
    if _PUBLIC_BROWSER_URL:
        return _PUBLIC_BROWSER_URL
    try:
        if has_request_context() and (request.headers.get("X-Forwarded-Prefix") or "").strip():
            proto = (request.headers.get("X-Forwarded-Proto") or request.scheme or "https")
            proto = proto.split(",")[0].strip().lower()
            if proto not in ("http", "https"):
                proto = "https"
            host = (request.headers.get("X-Forwarded-Host") or request.host or "").split(",")[0].strip()
            if host:
                return f"{proto}://{host}".rstrip("/")
    except Exception:
        pass
    return _PORTAL_URL


def get_portal_url() -> str:
    """Public portal base URL for redirects and template links (browser-visible when proxied)."""
    return _browser_portal_base()


def get_portal_dashboard_url() -> str:
    """CDI portal dashboard URL (matches hardcoded “Back to CDI dashboard” links in module base templates)."""
    return "https://h2s.tech/dashboard"


def _decode_token(token: str) -> dict | None:
    """Decode and verify the unified portal JWT. Returns payload or None."""
    if not _JWT_SECRET:
        return None
    try:
        return jwt.decode(
            token,
            _JWT_SECRET,
            algorithms=["HS256"],
            options={"verify_exp": True},
        )
    except Exception:
        return None


def _module_access_value(payload: dict) -> list[str] | None:
    """
    moduleAccess entry for this module (keys matched case-insensitively).
    None  → JWT null, all pages allowed for this module.
    []    → no pages.
    list  → explicit page ids.
    """
    if payload.get("isAdmin"):
        return None
    module_access = payload.get("moduleAccess") or {}
    if not isinstance(module_access, dict):
        return []
    mid = (_MODULE_ID or "").strip().lower()
    if not mid:
        return []
    for k, v in module_access.items():
        if (k or "").strip().lower() == mid:
            if v is None:
                return None
            if isinstance(v, list):
                return v
            return []
    return []


def _allowed_pages(payload: dict) -> list[str] | None:
    return _module_access_value(payload)


def _refresh_url() -> str:
    return f"{get_portal_url()}/auth/refresh?module={_MODULE_ID}"


def _first_allowed_path(pages: list[str] | None) -> str:
    _root = (request.environ.get("SCRIPT_NAME") or "").rstrip("/")
    if pages:
        return f"{_root}/{pages[0]}"
    return f"{_root}/dashboard"


def _normalize_path(path: str | None) -> str:
    p = (path or "").strip() or "/"
    if not p.startswith("/"):
        p = "/" + p
    return p


def _routing_path() -> str:
    """
    PATH_INFO normalized for auth rules. When WSGI PATH_INFO includes the mount
    prefix (e.g. /my-module/api/...), strip SCRIPT_NAME so public_paths still match.
    """
    path = _normalize_path(request.path)
    sn = (request.environ.get("SCRIPT_NAME") or "").strip().rstrip("/")
    if sn:
        if path in (sn, sn + "/"):
            return "/"
        if path.startswith(sn + "/"):
            path = path[len(sn) :] or "/"
    return _normalize_path(path)


def _prefix_matches(path: str, prefix: str) -> bool:
    """True if path is exactly prefix or a sub-path under prefix."""
    path = _normalize_path(path)
    pre = (prefix or "").strip()
    if not pre.startswith("/"):
        pre = "/" + pre
    pre = pre.rstrip("/")
    if pre == "" or pre == "/":
        return path == "/"
    return path == pre or path.startswith(pre + "/")


def _is_public_path(path: str, public_paths: tuple[str, ...]) -> bool:
    for p in public_paths:
        if _prefix_matches(path, p):
            return True
    return False


def _resolve_page_for_path(
    path: str,
    path_page_rules: list[tuple[str, str]],
    default_page: str | None,
) -> str | None:
    """
    Longest matching path prefix wins. If no rule matches, return default_page
    (may be None → JWT valid only, no L2 page check).
    """
    if not path_page_rules:
        return default_page
    sorted_rules = sorted(
        path_page_rules,
        key=lambda x: len(_normalize_path(x[0]).rstrip("/")),
        reverse=True,
    )
    for prefix, page_id in sorted_rules:
        if _prefix_matches(path, prefix):
            return page_id
    return default_page


def _enforce_request_auth(page: str | None):
    """
    Validate cookie JWT and optional L2 page. Returns None if OK, else a Response.
    Sets g.user on success.
    """
    cookie_token = request.cookies.get(_COOKIE_NAME)
    if not cookie_token:
        return redirect(_refresh_url())

    payload = _decode_token(cookie_token)
    if not payload:
        resp = make_response(redirect(_refresh_url()))
        resp.delete_cookie(_COOKIE_NAME, path="/")
        return resp

    g.user = payload

    if page and not payload.get("isAdmin"):
        pages = _allowed_pages(payload)
        if pages is not None and page not in pages:
            if pages:
                return redirect(_first_allowed_path(pages))
            return redirect(f"{get_portal_url()}/dashboard")

    return None


def register_h2s_cdi_auth(
    app: Flask,
    *,
    public_paths: tuple[str, ...] | list[str] | None = None,
    path_page_rules: list[tuple[str, str]] | None = None,
    default_page: str | None = None,
    skip_static: bool = True,
) -> None:
    """
    Protect the whole app with one before_request handler.

    public_paths: URL prefixes that skip auth (health, login, static, ...).
    path_page_rules: (path_prefix, pageId) — must match portal pageIds. Longest prefix wins.
    default_page: pageId for paths not matched by any rule; None = only require valid JWT
                  (use for "/" redirect routes that do not map to a registered page).
    """
    pub = tuple(public_paths) if public_paths is not None else (
        "/static",
        "/favicon.ico",
    )
    rules = list(path_page_rules or [])

    @app.before_request
    def _h2s_cdi_global_auth():
        if request.method == "OPTIONS":
            return None
        if skip_static and request.endpoint == "static":
            return None
        path = _routing_path()
        if _is_public_path(path, pub):
            return None
        page = _resolve_page_for_path(path, rules, default_page)
        return _enforce_request_auth(page)


def h2s_cdi_auth_required(f=None, *, page: str | None = None):
    """
    Enforce portal JWT authentication on a single Flask route.

    Prefer register_h2s_cdi_auth() for new apps. Use this for one-off overrides
    or while migrating.
    """
    def decorator(func):
        @wraps(func)
        def wrapped(*args, **kwargs):
            err = _enforce_request_auth(page)
            if err is not None:
                return err
            return func(*args, **kwargs)
        return wrapped

    if f is not None:
        return decorator(f)
    return decorator


def get_user() -> dict | None:
    return getattr(g, "user", None)


def get_module_pages(user: dict | None = None) -> list[str] | None:
    if user is None:
        user = get_user()
    if user is None:
        return []
    return _module_access_value(user)


def get_module_event_allowlist(user: dict | None = None) -> list[str] | None:
    """
    Initiative/event names the user may see in modules that filter by event (JWT: moduleEventAccess).

    None  → unrestricted (all events).
    []    → no events.
    [...] → explicit allow-list (names must match the source data).
    """
    if user is None:
        user = get_user()
    if user is None:
        return []
    if user.get("isAdmin"):
        return None
    raw = user.get("moduleEventAccess") or {}
    if not isinstance(raw, dict):
        return None
    mid = (_MODULE_ID or "").strip().lower()
    if not mid:
        return None
    for k, v in raw.items():
        if (k or "").strip().lower() == mid:
            if v is None:
                return None
            if isinstance(v, list):
                return v
            return []
    return None


def register_with_portal(
    pages: list[dict],
    module_name: str = "",
    base_url: str = "",
) -> bool:
    """
    Register this module and its pages with the portal on startup.

    pages: list of {"pageId", "label", "path"} dicts.
    """
    if not _REGISTRATION_SECRET:
        print(
            f"[h2s_cdi_auth] WARNING: H2S_CDI_REGISTRATION_SECRET (or JARVIS_REGISTRATION_SECRET) not set. "
            f"Module '{_MODULE_ID}' will not register."
        )
        return False

    if not _MODULE_ID:
        print("[h2s_cdi_auth] WARNING: H2S_CDI_MODULE_ID (or JARVIS_MODULE_ID) not set. Skipping registration.")
        return False

    try:
        resp = requests.post(
            f"{_INTERNAL_URL}/api/modules/register",
            json={
                "moduleId": _MODULE_ID,
                "moduleName": module_name or _MODULE_ID.upper(),
                "baseUrl": base_url,
                "pages": pages,
            },
            headers={"x-module-secret": _REGISTRATION_SECRET},
            timeout=5,
        )
        if resp.status_code == 200:
            data = resp.json()
            new = data.get("pagesNew", 0)
            archived = data.get("pagesArchived", 0)
            print(
                f"[h2s_cdi_auth] Registered with portal. "
                f"Pages: {data.get('pagesIncoming', 0)} total, "
                f"{new} new, {archived} archived."
            )
            return True
        print(f"[h2s_cdi_auth] Registration failed: HTTP {resp.status_code} — {resp.text[:200]}")
        return False
    except requests.exceptions.ConnectionError:
        print(
            f"[h2s_cdi_auth] WARNING: Could not reach portal at {_PORTAL_URL}. "
            f"Registration skipped — app will still start."
        )
        return False
    except Exception as exc:
        print(f"[h2s_cdi_auth] Registration error: {exc}")
        return False
