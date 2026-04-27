"""
Prompt Wars — Flask-only dashboard and data API.

Run: python app.py
"""

from __future__ import annotations

import base64
import csv
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from decimal import Decimal
from io import StringIO
from pathlib import Path
from urllib.parse import urlencode

from dotenv import load_dotenv
from flask import Flask, g, jsonify, make_response, redirect, render_template, request, Response, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix
from sqlalchemy import bindparam, text
from sqlalchemy.engine import Engine

from audit.db import create_engine

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import (  # noqa: E402
    etl_data_center,
    etl_in_person,
    etl_in_person_challenge_submissions,
    etl_virtual_challenge_submissions,
)
from services.upload_archive import archive_upload, mark_archive_status  # noqa: E402

load_dotenv(ROOT / ".env")

from h2s_cdi_auth import get_portal_url, register_h2s_cdi_auth, register_with_portal  # noqa: E402

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://postgres:postgres@127.0.0.1:5432/prompt_wars",
)
APP_HOST = os.environ.get("FLASK_HOST", os.environ.get("HOST", "127.0.0.1"))
APP_PORT = int(os.environ.get("FLASK_PORT", os.environ.get("PORT", "5000")))
SESSION_SECRET = os.environ.get("SESSION_SECRET", "dev-change-me")
MODULE_DISPLAY_NAME = os.environ.get("MODULE_NAME", "Prompt Wars")
MODULE_BASE_URL = os.environ.get("BASE_URL", f"http://{APP_HOST}:{APP_PORT}").rstrip("/")

APPLICATION_ROOT = (os.environ.get("APPLICATION_ROOT") or "").strip()
if APPLICATION_ROOT and not APPLICATION_ROOT.startswith("/"):
    APPLICATION_ROOT = "/" + APPLICATION_ROOT


def _cdi_mount_prefix_for_wsgi() -> str:
    """URL prefix stripped at WSGI level (Flask routes match before ``before_request``)."""
    r = (APPLICATION_ROOT or "").strip().rstrip("/")
    if r:
        return r
    mid = (os.environ.get("H2S_CDI_MODULE_ID") or os.environ.get("JARVIS_MODULE_ID") or "").strip()
    if not mid:
        return ""
    m = mid.lower().replace(" ", "-")
    return "/" + m.lstrip("/")


CDI_MOUNT_PREFIX = _cdi_mount_prefix_for_wsgi()


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "y"}


# Debug mode is ON by default during development; disable in prod with FLASK_DEBUG=0
DEBUG_MODE = _env_bool("FLASK_DEBUG", True)
USE_RELOADER = _env_bool("FLASK_USE_RELOADER", DEBUG_MODE)

DEFAULT_IN_PERSON_EVENT_ID = int(os.environ.get("DEFAULT_IN_PERSON_EVENT_ID", "1"))
DEFAULT_VIRTUAL_EVENT_ID = int(os.environ.get("DEFAULT_VIRTUAL_EVENT_ID", "2"))
DEFAULT_CHALLENGE_ID = int(os.environ.get("DEFAULT_CHALLENGE_ID", "1"))

# Main Data Center registration exports: separate physical tables per track.
TABLE_IN_PERSON_MDC = "in_person_main_data_center_registrations"
TABLE_VIRTUAL_MDC = "virtual_main_data_center_registrations"
TABLE_IN_PERSON_CHALLENGE_SUBMISSIONS = "in_person_challenge_submission_rows"

# Roster advanced filters: `af_<column>` query params (exact match after trim; values from dropdowns).
# Distinct option lists are capped per column (see ``MDC_USERS_ADVANCED_SELECT_LIMIT``).
MDC_USERS_ADVANCED_SELECT_LIMIT = 300

MDC_USERS_ADVANCED_TEXT_COLUMNS: tuple[str, ...] = (
    "org_name",
    "org_state",
    "org_city",
    "class_stream",
    "portfolio",
    "domain",
    "designation",
    "founded_info",
    "degree",
    "country",
    "state",
    "city",
    "gender",
    "occupation",
    "github_url",
    "linkedin_url",
    "attendance_city",
)

# (column_key, label) for admin UI — same fields in-person and virtual.
MDC_USERS_ADVANCED_FORM_FIELDS: tuple[tuple[str, str], ...] = (
    ("country", "Country"),
    ("state", "State / province"),
    ("city", "City (residence)"),
    ("gender", "Gender"),
    ("occupation", "Occupation"),
    ("degree", "Degree"),
    ("designation", "Designation"),
    ("org_name", "Organization name"),
    ("org_state", "Organization state"),
    ("org_city", "Organization city"),
    ("class_stream", "Class / stream"),
    ("portfolio", "Portfolio"),
    ("domain", "Domain"),
    ("founded_info", "Founded info"),
    ("github_url", "GitHub URL"),
    ("linkedin_url", "LinkedIn URL"),
    ("attendance_city", "Attendance / promptathon city"),
)

# Grouping for the advanced-filter UI. ``kind`` includes select_distinct, years_range_select,
# date_range, datetime_range.
# NOTE: top-level dict uses ``fields`` (not ``items``) because Jinja resolves ``.items`` as the dict method.
MDC_USERS_ADVANCED_FIELD_GROUPS: tuple[dict, ...] = (
    {
        "id": "personal",
        "title": "Personal",
        "icon": "person",
        "fields": (
            {"kind": "select_distinct", "col": "gender", "label": "Gender"},
            {"kind": "select_distinct", "col": "occupation", "label": "Occupation"},
            {"kind": "select_distinct", "col": "degree", "label": "Degree"},
            {"kind": "select_distinct", "col": "designation", "label": "Designation"},
            {
                "kind": "years_range_select",
                "label": "Years of experience",
                "min_key": "designation_years_min",
                "max_key": "designation_years_max",
            },
            {"kind": "select_distinct", "col": "class_stream", "label": "Class / stream"},
            {
                "kind": "date_range",
                "label": "Date of birth",
                "from_key": "dob_from",
                "to_key": "dob_to",
            },
        ),
    },
    {
        "id": "location",
        "title": "Location",
        "icon": "pin_drop",
        "fields": (
            {"kind": "select_distinct", "col": "country", "label": "Country"},
            {"kind": "select_distinct", "col": "state", "label": "State / province"},
            {"kind": "select_distinct", "col": "city", "label": "City (residence)"},
            {"kind": "select_distinct", "col": "attendance_city", "label": "Attendance / promptathon city"},
        ),
    },
    {
        "id": "organization",
        "title": "Organization",
        "icon": "apartment",
        "fields": (
            {"kind": "select_distinct", "col": "org_name", "label": "Organization name"},
            {"kind": "select_distinct", "col": "org_state", "label": "Organization state"},
            {"kind": "select_distinct", "col": "org_city", "label": "Organization city"},
            {"kind": "select_distinct", "col": "domain", "label": "Domain"},
            {"kind": "select_distinct", "col": "founded_info", "label": "Founded info"},
        ),
    },
    {
        "id": "online",
        "title": "Online presence",
        "icon": "link",
        "fields": (
            {"kind": "select_distinct", "col": "portfolio", "label": "Portfolio"},
            {"kind": "select_distinct", "col": "github_url", "label": "GitHub URL"},
            {"kind": "select_distinct", "col": "linkedin_url", "label": "LinkedIn URL"},
        ),
    },
    {
        "id": "registration",
        "title": "Registration",
        "icon": "event",
        "fields": (
            {
                "kind": "datetime_range",
                "label": "Registered",
                "from_key": "form_ts_from",
                "to_key": "form_ts_to",
            },
        ),
    },
)

# Labels for advanced-filter chips (dates, numeric ranges, etc.).
MDC_USERS_ADVANCED_CHIP_LABELS: dict[str, str] = {
    "form_ts_from": "Registered from",
    "form_ts_to": "Registered to",
    "dob_from": "DOB from",
    "dob_to": "DOB to",
    "designation_years_min": "Years exp. (min)",
    "designation_years_max": "Years exp. (max)",
}

# In-person Action Center: legacy rows (pre–Prompt War session) use this date + empty session_label.
IPCSR_LEGACY_PROMPT_WAR_DATE = date(1970, 1, 1)
IPCSR_SESSION_LABEL_MAX_LEN = 64


def _parse_ipcsr_prompt_war_date_from_form(raw: str | None) -> date | None:
    s = (raw or "").strip()
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def _normalize_ipcsr_session_label(raw: str | None) -> str:
    return (raw or "").strip()[:IPCSR_SESSION_LABEL_MAX_LEN]


def _ipcsr_pw_session_display(*, city: str, prompt_war_on: date, session_label: str) -> str:
    if prompt_war_on == IPCSR_LEGACY_PROMPT_WAR_DATE and not (session_label or "").strip():
        return f"{city} · (legacy)"
    d = prompt_war_on.strftime("%d %b %Y")
    if (session_label or "").strip():
        return f"{city} · {d} · {session_label.strip()}"
    return f"{city} · {d}"


def _parse_main_dashboard_pw_session(raw: str | None) -> tuple[date | None, str]:
    """Parse ``inPersonTopPwSession`` value ``YYYY-MM-DD`` or ``YYYY-MM-DD|label``."""
    v = (raw or "").strip()
    if not v:
        return None, ""
    if "|" in v:
        d_s, lab = v.split("|", 1)
        d = _parse_ipcsr_prompt_war_date_from_form(d_s.strip())
        return d, _normalize_ipcsr_session_label(lab)
    d = _parse_ipcsr_prompt_war_date_from_form(v)
    return d, ""


def _encode_in_person_overview_session(*, city: str, prompt_war_on_iso: str, session_label: str) -> str:
    """Stable token for ``inPersonOverview`` query param (city + PW session)."""
    b = json.dumps(
        {"c": city, "i": prompt_war_on_iso, "l": session_label or ""},
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    token = base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")
    return f"s:{token}"


def _decode_in_person_overview(raw: str | None) -> tuple[str, str | None, str]:
    """From ``inPersonOverview``: (``global``|``city``, city_or_None, raw_pw_session)."""
    v = (raw or "").strip()
    if not v or v == "global":
        return "global", None, ""
    if v.startswith("s:"):
        pad = "=" * (-len(v[2:]) % 4)
        try:
            payload = base64.urlsafe_b64decode(v[2:] + pad).decode("utf-8")
            o = json.loads(payload)
            city = str(o.get("c") or "").strip() or None
            iso = str(o.get("i") or "").strip()
            lab = str(o.get("l") or "")
            if not city or not iso:
                return "global", None, ""
            raw_sess = f"{iso}|{lab}" if lab else iso
            return "city", city, raw_sess
        except (ValueError, json.JSONDecodeError, OSError, TypeError):
            return "global", None, ""
    return "global", None, ""


def _encode_virtual_overview_challenge(challenge_id: int) -> str:
    """Stable token for ``virtualOverview`` query param (single arena challenge)."""
    b = json.dumps({"id": int(challenge_id)}, separators=(",", ":")).encode("utf-8")
    token = base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")
    return f"v:{token}"


def _decode_virtual_overview(raw: str | None) -> tuple[str, int | None]:
    """From ``virtualOverview``: (``global``|``arena``, challenge_id_or_None)."""
    v = (raw or "").strip()
    if not v or v == "global":
        return "global", None
    if v.startswith("v:"):
        pad = "=" * (-len(v[2:]) % 4)
        try:
            o = json.loads(base64.urlsafe_b64decode(v[2:] + pad).decode("utf-8"))
            cid = int(o["id"])
            return "arena", cid
        except (ValueError, json.JSONDecodeError, OSError, TypeError, KeyError):
            return "global", None
    return "global", None


def _mdc_table_for_mode(mode: str) -> str:
    if mode == "in_person":
        return TABLE_IN_PERSON_MDC
    if mode == "virtual":
        return TABLE_VIRTUAL_MDC
    raise ValueError(f"unknown mdc mode: {mode!r}")


def _format_dt_display(v) -> str:
    """UI date/time: DD-MM-YYYY HH:MM:SS for datetimes, DD-MM-YYYY for date-only."""
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.strftime("%d-%m-%Y %H:%M:%S")
    if isinstance(v, date):
        return v.strftime("%d-%m-%Y")
    if hasattr(v, "strftime"):
        try:
            return v.strftime("%d-%m-%Y %H:%M:%S")
        except Exception:  # noqa: BLE001
            return str(v)
    return str(v)


def _format_submission_submitted_at(value) -> str:
    """Human-readable submission time: DD-MM-YYYY HH:MM (preserves timezone from ISO strings)."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%d-%m-%Y %H:%M")
    if isinstance(value, date):
        return value.strftime("%d-%m-%Y")
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return ""
        try:
            normalized = s.replace("Z", "+00:00", 1) if len(s) > 1 and s[-1] in "Zz" else s
            dt = datetime.fromisoformat(normalized)
            return dt.strftime("%d-%m-%Y %H:%M")
        except ValueError:
            if len(s) >= 16 and "T" in s[:11]:
                return f"{s[8:10]}-{s[5:7]}-{s[0:4]} {s[11:16]}"
            return s
    if hasattr(value, "strftime"):
        try:
            return value.strftime("%d-%m-%Y %H:%M")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            return str(value)
    return str(value)


engine: Engine = create_engine(DATABASE_URL, future=True)


class _StripCdiPathMiddleware:
    """
    Strip ``/prompt-wars`` (or APPLICATION_ROOT / module slug) from PATH_INFO before
    Flask matches URLs. ``before_request`` runs too late — routing already used PATH_INFO.
    """

    __slots__ = ("app", "prefix")

    def __init__(self, app, prefix: str):
        self.app = app
        p = (prefix or "").strip().rstrip("/")
        self.prefix = p if (not p or p.startswith("/")) else ("/" + p)

    def __call__(self, environ, start_response):
        if self.prefix:
            path = environ.get("PATH_INFO") or "/"
            if path == self.prefix or path.startswith(self.prefix + "/"):
                script = (environ.get("SCRIPT_NAME") or "").rstrip("/")
                environ["SCRIPT_NAME"] = (script + self.prefix) if script else self.prefix
                rest = path[len(self.prefix) :] or "/"
                if rest == "":
                    rest = "/"
                if not rest.startswith("/"):
                    rest = "/" + rest
                environ["PATH_INFO"] = rest
        return self.app(environ, start_response)


app = Flask(__name__, template_folder="templates", static_folder="static", static_url_path="/static")
app.secret_key = SESSION_SECRET
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
if CDI_MOUNT_PREFIX:
    app.wsgi_app = _StripCdiPathMiddleware(app.wsgi_app, CDI_MOUNT_PREFIX)


@app.template_filter("pw_submitted_at")
def _pw_submitted_at_jinja(value) -> str:
    """Jinja: format ``submitted_at`` ISO string or datetime as DD-MM-YYYY HH:MM."""
    out = _format_submission_submitted_at(value)
    return out if out else "—"


@app.before_request
def _pw_set_script_name_for_cdi():
    """
    Prefer SCRIPT_NAME from ``X-Forwarded-Prefix`` when PATH_INFO is already normalized
    (e.g. by ``_StripCdiPathMiddleware``). PATH_INFO rewriting here is a fallback only.
    """
    app_root = (APPLICATION_ROOT or CDI_MOUNT_PREFIX or "").strip().rstrip("/")
    if app_root and not app_root.startswith("/"):
        app_root = "/" + app_root
    path_info = request.environ.get("PATH_INFO") or "/"
    if app_root and (path_info == app_root or path_info.startswith(app_root + "/")):
        request.environ["SCRIPT_NAME"] = app_root
        rest = path_info[len(app_root) :] or "/"
        if rest == "":
            rest = "/"
        if not rest.startswith("/"):
            rest = "/" + rest
        request.environ["PATH_INFO"] = rest
        return
    prefix = request.environ.get("HTTP_X_FORWARDED_PREFIX", "").strip().rstrip("/")
    if prefix and not prefix.startswith("/"):
        prefix = "/" + prefix
    if not prefix and APPLICATION_ROOT:
        prefix = APPLICATION_ROOT.rstrip("/")
    if prefix:
        request.environ["SCRIPT_NAME"] = prefix


# Portal page registry (pageId must match path_page_rules and portal RBAC).
MODULE_PAGES: list[dict[str, str]] = [
    {"pageId": "overview_dashboard", "label": "Overview · Dashboard", "path": "/"},
    {"pageId": "overview_logs", "label": "Overview · Logs", "path": "/overview/logs"},
    {"pageId": "overview_settings", "label": "Overview · Settings", "path": "/overview/settings"},
    {"pageId": "in_person_dashboard", "label": "In-person · Dashboard", "path": "/in-person"},
    {"pageId": "in_person_leaderboard", "label": "In-person · Leaderboard", "path": "/in-person/leaderboard"},
    {"pageId": "in_person_users", "label": "In-person · Users", "path": "/in-person/users"},
    {"pageId": "in_person_import", "label": "In-person · Import", "path": "/in-person/import"},
    {"pageId": "in_person_settings", "label": "In-person · Settings", "path": "/in-person/settings"},
    {"pageId": "virtual_dashboard", "label": "Virtual · Dashboard", "path": "/virtual"},
    {"pageId": "virtual_leaderboard", "label": "Virtual · Leaderboard", "path": "/virtual/leaderboard"},
    {"pageId": "virtual_challenges", "label": "Virtual · Challenges", "path": "/virtual/challenges"},
    {"pageId": "virtual_users", "label": "Virtual · Users", "path": "/virtual/users"},
    {"pageId": "virtual_import", "label": "Virtual · Import", "path": "/virtual/import"},
    {"pageId": "virtual_settings", "label": "Virtual · Settings", "path": "/virtual/settings"},
]

# Longest-prefix wins inside h2s_cdi_auth; order here is readability only.
_H2S_PATH_PAGE_RULES: list[tuple[str, str]] = [
    ("/admin/import/virtual/challenge-submissions", "overview_settings"),
    ("/admin/import/virtual/main-data-center", "overview_settings"),
    ("/admin/import/in-person/main-data-center", "overview_settings"),
    ("/admin/import/in-person/action-center", "overview_settings"),
    ("/admin/import", "overview_settings"),
    ("/admin", "overview_settings"),
    ("/api/import/latest", "overview_settings"),
    ("/api/credits/grant", "overview_settings"),
    ("/overview/settings", "overview_settings"),
    ("/overview/logs", "overview_logs"),
    ("/api/import/virtual/challenge-submissions", "virtual_import"),
    ("/api/import/virtual/main-data-center", "virtual_import"),
    ("/virtual/import", "virtual_import"),
    ("/api/import/in-person/main-data-center", "in_person_import"),
    ("/api/import/in-person/action-center", "in_person_import"),
    ("/api/in-person/attendance-cities", "in_person_import"),
    ("/api/in-person/action-center/leaderboard", "in_person_dashboard"),
    ("/api/import/in-person", "in_person_import"),
    ("/in-person/import", "in_person_import"),
    ("/in-person/leaderboard", "in_person_leaderboard"),
    ("/api/virtual/main-data-center/registrations", "virtual_users"),
    ("/virtual/users/export.csv", "virtual_users"),
    ("/virtual/users", "virtual_users"),
    ("/api/in-person/main-data-center/registrations", "in_person_users"),
    ("/in-person/users/export.csv", "in_person_users"),
    ("/in-person/users", "in_person_users"),
    ("/api/virtual/challenges", "virtual_challenges"),
    ("/virtual/challenges", "virtual_challenges"),
    ("/api/virtual/submission-leaderboard", "virtual_leaderboard"),
    ("/api/virtual/global-submission-leaderboard", "virtual_leaderboard"),
    ("/api/distribution", "virtual_leaderboard"),
    ("/api/leaderboard", "virtual_leaderboard"),
    ("/virtual/leaderboard", "virtual_leaderboard"),
    ("/api/stats/city", "in_person_dashboard"),
    ("/api/funnel", "in_person_dashboard"),
    ("/in-person", "in_person_dashboard"),
    ("/virtual/settings", "virtual_settings"),
    ("/virtual", "virtual_dashboard"),
    ("/", "overview_dashboard"),
]
# Portal may redirect using pageId as a single path segment (see h2s_cdi_auth._first_allowed_path).
_H2S_PATH_PAGE_RULES.extend((f"/{p['pageId']}", p["pageId"]) for p in MODULE_PAGES)

register_h2s_cdi_auth(
    app,
    public_paths=(
        "/static",
        "/favicon.ico",
        "/api/health",
        "/login",
        "/logout",
        "/admin/login",
    ),
    path_page_rules=_H2S_PATH_PAGE_RULES,
    default_page=None,
)


def _pw_cdi_first_allowed_path(pages: list[str] | None) -> str:
    """
    ``h2s_cdi_auth`` uses ``pages[0]`` from the JWT; portal order is often alphabetical,
    so ``in_person_dashboard`` wins over ``overview_dashboard``. Prefer the overview
    home when it is among allowed pages; otherwise use ``MODULE_PAGES`` order.
    """
    _root = (request.environ.get("SCRIPT_NAME") or "").rstrip("/")
    if not pages:
        return f"{_root}/dashboard"
    if "overview_dashboard" in pages:
        pid = "overview_dashboard"
    else:
        order = [p["pageId"] for p in MODULE_PAGES]
        pid = min(pages, key=lambda p: (order.index(p) if p in order else 9999, p))
    return f"{_root}/{pid}"


import h2s_cdi_auth as _h2s_cdi_auth_module  # noqa: E402

_h2s_cdi_auth_module._first_allowed_path = _pw_cdi_first_allowed_path

# Master Audit Log: install once at boot. After this call every HTTP request,
# every SQL statement (incl. SELECT), and every auth event is captured into
# audit.audit_events asynchronously, and DB row triggers (database/audit.sql)
# write field-level diffs into audit.audit_data_changes synchronously.
import audit  # noqa: E402
from audit.decorators import audit_view  # noqa: E402

audit.install(app, engine)

# Map Flask endpoint → (module id, sub-page key) for sidebar + module selector.
_PW_ENDPOINT_NAV: dict[str, tuple[str, str]] = {
    "main_dashboard": ("overview", "dashboard"),
    "overview_logs": ("overview", "logs"),
    "overview_settings": ("overview", "settings"),
    "in_person_page": ("in_person", "dashboard"),
    "in_person_leaderboard": ("in_person", "leaderboard"),
    "in_person_users": ("in_person", "users"),
    "in_person_users_export_csv": ("in_person", "users"),
    "in_person_settings": ("in_person", "settings"),
    "in_person_import": ("in_person", "import"),
    "api_in_person_mdc_registration": ("in_person", "users"),
    "api_virtual_mdc_registration": ("virtual", "users"),
    "api_import_virtual_main_data_center": ("virtual", "import"),
    "api_import_virtual_challenge_submissions": ("virtual", "import"),
    "admin_import_virtual_main_data_center": ("overview", "settings"),
    "admin_import_virtual_challenge_submissions": ("overview", "settings"),
    "virtual_users_export_csv": ("virtual", "users"),
    "virtual_page": ("virtual", "dashboard"),
    "virtual_submission_leaderboard": ("virtual", "leaderboard"),
    "api_virtual_submission_leaderboard": ("virtual", "leaderboard"),
    "virtual_users": ("virtual", "users"),
    "virtual_settings": ("virtual", "settings"),
    "virtual_import": ("virtual", "import"),
    "virtual_challenges": ("virtual", "challenges"),
    "virtual_challenges_create": ("virtual", "challenges"),
    "virtual_challenges_update": ("virtual", "challenges"),
    "virtual_challenges_delete": ("virtual", "challenges"),
    "api_virtual_challenges": ("virtual", "challenges"),
    "api_virtual_challenge_eligibility": ("virtual", "challenges"),
    "admin_page": ("overview", "settings"),
    "portal_login": ("overview", "settings"),
    "logout": ("overview", "settings"),
    "admin_import_in_person": ("overview", "settings"),
    "admin_import_in_person_data_center": ("overview", "settings"),
    "admin_import_in_person_action_center": ("overview", "settings"),
    "api_import_in_person_action_center": ("in_person", "import"),
    "api_in_person_attendance_cities": ("in_person", "import"),
    "api_in_person_action_center_leaderboard": ("in_person", "dashboard"),
    "api_in_person_mdc_stats": ("in_person", "dashboard"),
    "api_virtual_mdc_stats": ("virtual", "dashboard"),
    "admin_result": ("overview", "settings"),
}


def _pw_subnav_rows(module: str) -> list[dict[str, str]]:
    if module == "overview":
        spec = (
            ("dashboard", "Dashboard", "main_dashboard", "dashboard"),
            ("logs", "Logs", "overview_logs", "receipt_long"),
            ("settings", "Settings", "overview_settings", "settings"),
        )
    elif module == "in_person":
        spec = (
            ("dashboard", "Dashboard", "in_person_page", "analytics"),
            ("leaderboard", "Leaderboard", "in_person_leaderboard", "leaderboard"),
            ("users", "Users", "in_person_users", "group"),
            ("import", "Import", "in_person_import", "upload_file"),
            ("settings", "Settings", "in_person_settings", "tune"),
        )
    else:
        spec = (
            ("dashboard", "Dashboard", "virtual_page", "stadia_controller"),
            ("leaderboard", "Leaderboard", "virtual_submission_leaderboard", "leaderboard"),
            ("challenges", "Challenges", "virtual_challenges", "flag"),
            ("users", "Users", "virtual_users", "group"),
            ("import", "Import", "virtual_import", "upload_file"),
            ("settings", "Settings", "virtual_settings", "tune"),
        )
    return [{"key": k, "label": lab, "endpoint": ep, "icon": ic} for k, lab, ep, ic in spec]


@app.context_processor
def _inject_ui_context() -> dict:
    """Event IDs + module navigation for sidebar / header."""
    ep = request.endpoint or ""
    pw_module, pw_nav_sub = _PW_ENDPOINT_NAV.get(ep, ("overview", "dashboard"))
    pw_subnav = [
        {
            "key": r["key"],
            "label": r["label"],
            "icon": r["icon"],
            "href": url_for(r["endpoint"]),
            "active": r["key"] == pw_nav_sub,
        }
        for r in _pw_subnav_rows(pw_module)
    ]
    pw_modules = [
        {"id": "overview", "label": "Overview", "href": url_for("main_dashboard")},
        {"id": "in_person", "label": "Prompt Wars In-person", "href": url_for("in_person_page")},
        {"id": "virtual", "label": "Prompt Wars Virtual", "href": url_for("virtual_page")},
    ]
    _portal = get_portal_url().rstrip("/")
    return {
        "in_person_event_id": request.args.get("inPersonEventId", type=int) or DEFAULT_IN_PERSON_EVENT_ID,
        "virtual_event_id": request.args.get("virtualEventId", type=int) or DEFAULT_VIRTUAL_EVENT_ID,
        "challenge_id": request.args.get("challengeId", type=int) or DEFAULT_CHALLENGE_ID,
        "pw_module": pw_module,
        "pw_nav_sub": pw_nav_sub,
        "pw_subnav": pw_subnav,
        "pw_modules": pw_modules,
        "portal_url": _portal,
        "portal_dashboard_url": f"{_portal}/dashboard",
    }


def _fetch_allowed_city_ids(conn, event_id: int) -> set[int]:
    rows = conn.execute(
        text("SELECT id FROM cities WHERE event_id = :eid"),
        {"eid": event_id},
    ).fetchall()
    return {int(r[0]) for r in rows}


def _upsert_participant(conn, external_user_id: str, display_name: str | None) -> int:
    row = conn.execute(
        text(
            """
            INSERT INTO participants (external_user_id, display_name)
            VALUES (:ext, :dn)
            ON CONFLICT (external_user_id) DO UPDATE
            SET display_name = COALESCE(EXCLUDED.display_name, participants.display_name)
            RETURNING id
            """
        ),
        {"ext": external_user_id, "dn": display_name},
    ).one()
    return int(row[0])


@app.get("/api/health")
def health():
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return jsonify({"ok": True, "database": "up"})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "database": "down", "detail": str(exc)}), 503


def _import_in_person_core():
    event_id_raw = request.form.get("event_id")
    if not event_id_raw:
        return jsonify({"error": "event_id is required"}), 400
    try:
        event_id = int(event_id_raw)
    except ValueError:
        return jsonify({"error": "event_id must be an integer"}), 400

    rsvp_file = request.files.get("rsvps")
    sub_file = request.files.get("submissions")
    if not rsvp_file or not sub_file:
        return jsonify({"error": "Both rsvps and submissions CSV files are required"}), 400

    archived_rsvp = archive_upload(
        rsvp_file,
        engine=engine,
        module="in_person_rsvps",
        source_route=request.path,
        event_id=event_id,
    )
    archived_sub = archive_upload(
        sub_file,
        engine=engine,
        module="in_person_submissions",
        source_route=request.path,
        event_id=event_id,
    )
    archive_ids = [a.id for a in (archived_rsvp, archived_sub) if a.id is not None]

    def _mark_all(status: str, *, error: str | None = None, rows_written: int | None = None) -> None:
        for aid in archive_ids:
            mark_archive_status(
                aid,
                status,
                engine=engine,
                error=error,
                rows_written=rows_written,
            )

    job_id = None
    try:
        df_r = etl_in_person.parse_rsvps_csv(archived_rsvp.fresh_stream())
        df_s = etl_in_person.parse_submissions_csv(archived_sub.fresh_stream())
        _mark_all("parsed")

        with engine.connect() as conn:
            ev = conn.execute(
                text("SELECT id, kind FROM events WHERE id = :id"),
                {"id": event_id},
            ).fetchone()
            if not ev:
                _mark_all("failed", error="event not found")
                return jsonify({"error": "event not found"}), 404
            if str(ev[1]) != "in_person":
                _mark_all("failed", error="event must be in_person kind")
                return jsonify({"error": "event must be in_person kind"}), 400

            allowed = _fetch_allowed_city_ids(conn, event_id)
        etl_in_person.validate_city_ids_for_event(df_r, df_s, allowed)

        with engine.begin() as conn:
            res = conn.execute(
                text(
                    """
                    INSERT INTO import_jobs (module, status, started_at, row_counts)
                    VALUES ('in_person', 'running', now(), '{}'::jsonb)
                    RETURNING id
                    """
                ),
            )
            job_id = int(res.scalar_one())
            for aid in archive_ids:
                mark_archive_status(aid, "parsed", engine=engine, import_job_id=job_id)

            result = etl_in_person.to_etl_result(df_r, df_s)

            rsvp_inserted = 0
            for row in result.rsvp_rows:
                pid = _upsert_participant(conn, row["user_id"], row.get("display_name"))
                conn.execute(
                    text(
                        """
                        INSERT INTO rsvps (participant_id, city_id, event_id, rsvped_at, import_job_id)
                        VALUES (:pid, :cid, :eid, CAST(:ts AS timestamptz), :jid)
                        ON CONFLICT (participant_id, city_id, event_id) DO UPDATE
                        SET rsvped_at = COALESCE(EXCLUDED.rsvped_at, rsvps.rsvped_at),
                            import_job_id = EXCLUDED.import_job_id
                        """
                    ),
                    {
                        "pid": pid,
                        "cid": row["city_id"],
                        "eid": event_id,
                        "ts": row["rsvped_at"],
                        "jid": job_id,
                    },
                )
                rsvp_inserted += 1

            sub_inserted = 0
            for row in result.submission_rows:
                pid = _upsert_participant(conn, row["user_id"], row.get("display_name"))
                conn.execute(
                    text(
                        """
                        INSERT INTO submissions (participant_id, city_id, event_id, submitted_at, import_job_id)
                        VALUES (:pid, :cid, :eid, CAST(:ts AS timestamptz), :jid)
                        ON CONFLICT (participant_id, city_id, event_id) DO UPDATE
                        SET submitted_at = COALESCE(EXCLUDED.submitted_at, submissions.submitted_at),
                            import_job_id = EXCLUDED.import_job_id
                        """
                    ),
                    {
                        "pid": pid,
                        "cid": row["city_id"],
                        "eid": event_id,
                        "ts": row["submitted_at"],
                        "jid": job_id,
                    },
                )
                sub_inserted += 1

            counts = {
                "rsvp_rows": rsvp_inserted,
                "submission_rows": sub_inserted,
                **result.join_stats,
            }
            conn.execute(
                text(
                    """
                    UPDATE import_jobs
                    SET status = 'success', finished_at = now(), row_counts = CAST(:rc AS jsonb), error_message = NULL
                    WHERE id = :jid
                    """
                ),
                {"rc": json.dumps(counts), "jid": job_id},
            )

        rows_total = rsvp_inserted + sub_inserted
        _mark_all("success", rows_written=rows_total)
        return jsonify(
            {
                "import_job_id": job_id,
                "status": "success",
                "row_counts": counts,
                "archive_paths": [a.stored_path for a in (archived_rsvp, archived_sub)],
            }
        )
    except ValueError as ve:
        _mark_all("failed", error=str(ve))
        if job_id is not None:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        """
                        UPDATE import_jobs
                        SET status = 'failed', finished_at = now(), error_message = :msg
                        WHERE id = :jid
                        """
                    ),
                    {"msg": str(ve), "jid": job_id},
                )
        return jsonify({"error": str(ve)}), 400
    except Exception as exc:  # noqa: BLE001
        _mark_all("failed", error=str(exc))
        if job_id is not None:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        """
                        UPDATE import_jobs
                        SET status = 'failed', finished_at = now(), error_message = :msg
                        WHERE id = :jid
                        """
                    ),
                    {"msg": str(exc), "jid": job_id},
                )
        return jsonify({"error": str(exc)}), 500


# RETURNING (xmax = 0): true for newly inserted row, false when ON CONFLICT chose UPDATE (PG heap tuple).
_MDC_UPSERT_SQL = """
    INSERT INTO {table} (
      event_id, email, form_timestamp, utm_source, utm_medium, utm_campaign, utm_term, utm_content,
      org_name, org_state, org_city, class_stream, portfolio, domain, designation, designation_years_experience,
      founded_info, degree,
      profile_name, full_name, mobile, whatsapp, country, state, city, dob, gender, occupation,
      github_url, linkedin_url, attendance_city
    ) VALUES (
      :event_id, :email, :form_timestamp, :utm_source, :utm_medium, :utm_campaign, :utm_term, :utm_content,
      :org_name, :org_state, :org_city, :class_stream, :portfolio, :domain, :designation, :designation_years_experience,
      :founded_info, :degree,
      :profile_name, :full_name, :mobile, :whatsapp, :country, :state, :city, :dob, :gender, :occupation,
      :github_url, :linkedin_url, :attendance_city
    )
    ON CONFLICT (event_id, email_normalized) DO UPDATE SET
      email = EXCLUDED.email,
      form_timestamp = EXCLUDED.form_timestamp,
      utm_source = EXCLUDED.utm_source,
      utm_medium = EXCLUDED.utm_medium,
      utm_campaign = EXCLUDED.utm_campaign,
      utm_term = EXCLUDED.utm_term,
      utm_content = EXCLUDED.utm_content,
      org_name = EXCLUDED.org_name,
      org_state = EXCLUDED.org_state,
      org_city = EXCLUDED.org_city,
      class_stream = EXCLUDED.class_stream,
      portfolio = EXCLUDED.portfolio,
      domain = EXCLUDED.domain,
      designation = EXCLUDED.designation,
      designation_years_experience = EXCLUDED.designation_years_experience,
      founded_info = EXCLUDED.founded_info,
      degree = EXCLUDED.degree,
      profile_name = EXCLUDED.profile_name,
      full_name = EXCLUDED.full_name,
      mobile = EXCLUDED.mobile,
      whatsapp = EXCLUDED.whatsapp,
      country = EXCLUDED.country,
      state = EXCLUDED.state,
      city = EXCLUDED.city,
      dob = EXCLUDED.dob,
      gender = EXCLUDED.gender,
      occupation = EXCLUDED.occupation,
      github_url = EXCLUDED.github_url,
      linkedin_url = EXCLUDED.linkedin_url,
      attendance_city = EXCLUDED.attendance_city,
      updated_at = now()
    RETURNING (xmax = 0) AS was_insert
    """

_IN_PERSON_MDC_UPSERT = text(_MDC_UPSERT_SQL.format(table=TABLE_IN_PERSON_MDC))
_VIRTUAL_MDC_UPSERT = text(_MDC_UPSERT_SQL.format(table=TABLE_VIRTUAL_MDC))

_VCSR_UPSERT = text(
    """
    INSERT INTO virtual_challenge_submission_rows (
      event_id, challenge_id, import_job_id, virtual_mdc_registration_id, source_sheet_name,
      team_name, leader_name, leader_email, leader_phone, team_size, problem_statements,
      total_score, deployed_link, linkedin_post, github_repository_link,
      export_created_at, export_created_by_name, export_created_by_email,
      export_updated_at, export_updated_by_name, export_updated_by_email
    ) VALUES (
      :event_id, :challenge_id, :import_job_id, :virtual_mdc_registration_id, :source_sheet_name,
      :team_name, :leader_name, :leader_email, :leader_phone, :team_size, :problem_statements,
      :total_score, :deployed_link, :linkedin_post, :github_repository_link,
      :export_created_at, :export_created_by_name, :export_created_by_email,
      :export_updated_at, :export_updated_by_name, :export_updated_by_email
    )
    ON CONFLICT (challenge_id, team_name_normalized) DO UPDATE SET
      import_job_id = EXCLUDED.import_job_id,
      virtual_mdc_registration_id = EXCLUDED.virtual_mdc_registration_id,
      source_sheet_name = EXCLUDED.source_sheet_name,
      leader_name = EXCLUDED.leader_name,
      leader_email = EXCLUDED.leader_email,
      leader_phone = EXCLUDED.leader_phone,
      team_size = EXCLUDED.team_size,
      problem_statements = EXCLUDED.problem_statements,
      total_score = EXCLUDED.total_score,
      deployed_link = EXCLUDED.deployed_link,
      linkedin_post = EXCLUDED.linkedin_post,
      github_repository_link = EXCLUDED.github_repository_link,
      export_created_at = EXCLUDED.export_created_at,
      export_created_by_name = EXCLUDED.export_created_by_name,
      export_created_by_email = EXCLUDED.export_created_by_email,
      export_updated_at = EXCLUDED.export_updated_at,
      export_updated_by_name = EXCLUDED.export_updated_by_name,
      export_updated_by_email = EXCLUDED.export_updated_by_email,
      updated_at = now()
    """
)

_IPCSR_UPSERT = text(
    """
    INSERT INTO in_person_challenge_submission_rows (
      event_id, attendance_city, prompt_war_on, session_label, import_job_id, in_person_mdc_registration_id,
      sheet_kind, source_sheet_name,
      team_name, leader_name, leader_email, leader_phone, team_size, problem_statements,
      total_score, deployed_link, deployed_changes_notes, github_repository_link,
      export_created_at, export_created_by_name, export_created_by_email,
      export_updated_at, export_updated_by_name, export_updated_by_email
    ) VALUES (
      :event_id, :attendance_city, :prompt_war_on, :session_label, :import_job_id, :in_person_mdc_registration_id,
      :sheet_kind, :source_sheet_name,
      :team_name, :leader_name, :leader_email, :leader_phone, :team_size, :problem_statements,
      :total_score, :deployed_link, :deployed_changes_notes, :github_repository_link,
      :export_created_at, :export_created_by_name, :export_created_by_email,
      :export_updated_at, :export_updated_by_name, :export_updated_by_email
    )
    ON CONFLICT (
      event_id, attendance_city_normalized, prompt_war_on, session_label_normalized, sheet_kind, team_name_normalized
    ) DO UPDATE SET
      import_job_id = EXCLUDED.import_job_id,
      in_person_mdc_registration_id = EXCLUDED.in_person_mdc_registration_id,
      prompt_war_on = EXCLUDED.prompt_war_on,
      session_label = EXCLUDED.session_label,
      source_sheet_name = EXCLUDED.source_sheet_name,
      leader_name = EXCLUDED.leader_name,
      leader_email = EXCLUDED.leader_email,
      leader_phone = EXCLUDED.leader_phone,
      team_size = EXCLUDED.team_size,
      problem_statements = EXCLUDED.problem_statements,
      total_score = EXCLUDED.total_score,
      deployed_link = EXCLUDED.deployed_link,
      deployed_changes_notes = EXCLUDED.deployed_changes_notes,
      github_repository_link = EXCLUDED.github_repository_link,
      export_created_at = EXCLUDED.export_created_at,
      export_created_by_name = EXCLUDED.export_created_by_name,
      export_created_by_email = EXCLUDED.export_created_by_email,
      export_updated_at = EXCLUDED.export_updated_at,
      export_updated_by_name = EXCLUDED.export_updated_by_name,
      export_updated_by_email = EXCLUDED.export_updated_by_email,
      updated_at = now()
    """
)


def _import_in_person_main_data_center_core():
    event_id = DEFAULT_IN_PERSON_EVENT_ID

    upload = request.files.get("main_data_center")
    if not upload or not upload.filename:
        return jsonify({"error": "A CSV or XLSX file is required (field main_data_center)"}), 400

    fn = (upload.filename or "").lower()
    if not (fn.endswith(".csv") or fn.endswith(".xlsx") or fn.endswith(".xls")):
        return jsonify({"error": "File must be .csv, .xlsx, or .xls"}), 400

    archived = archive_upload(
        upload,
        engine=engine,
        module="in_person_mdc",
        source_route=request.path,
        event_id=event_id,
    )

    try:
        rows, parse_stats = etl_data_center.parse_main_data_center_file(
            archived.fresh_stream(), upload.filename or ""
        )
    except ValueError as ve:
        mark_archive_status(archived.id, "failed", engine=engine, error=str(ve))
        return jsonify({"error": str(ve)}), 400

    mark_archive_status(archived.id, "parsed", engine=engine)

    with engine.connect() as conn:
        ev = conn.execute(
            text("SELECT id, kind FROM events WHERE id = :id"),
            {"id": event_id},
        ).fetchone()
        if not ev:
            mark_archive_status(archived.id, "failed", engine=engine, error="event not found")
            return jsonify({"error": "event not found"}), 404
        if str(ev[1]) != "in_person":
            mark_archive_status(archived.id, "failed", engine=engine, error="event must be in_person kind")
            return jsonify({"error": "event must be in_person kind"}), 400

    rows_created = 0
    rows_updated = 0
    try:
        with engine.begin() as conn:
            batch = [{**r, "event_id": event_id} for r in rows]
            for row in batch:
                res = conn.execute(_IN_PERSON_MDC_UPSERT, row)
                was_insert = res.scalar_one()
                if bool(was_insert):
                    rows_created += 1
                else:
                    rows_updated += 1
    except Exception as exc:  # noqa: BLE001
        mark_archive_status(archived.id, "failed", engine=engine, error=str(exc))
        return jsonify({"error": str(exc), "parse_stats": parse_stats}), 500

    rows_written = rows_created + rows_updated
    mark_archive_status(archived.id, "success", engine=engine, rows_written=rows_written)
    payload = {
        "status": "success",
        "rows_created": rows_created,
        "rows_updated": rows_updated,
        "rows_written": rows_written,
        "rows_skipped": int(parse_stats.get("rows_skipped_no_email") or 0),
        "rows_read": int(parse_stats.get("rows_read") or 0),
        "rows_after_dedupe": int(parse_stats.get("rows_after_dedupe") or 0),
        "duplicate_emails_collapsed": int(parse_stats.get("duplicate_emails_collapsed") or 0),
        "archive_path": archived.stored_path,
    }
    return jsonify(payload)


def _import_virtual_main_data_center_core():
    event_id = DEFAULT_VIRTUAL_EVENT_ID

    upload = request.files.get("virtual_main_data_center")
    if not upload or not upload.filename:
        return jsonify({"error": "A CSV or XLSX file is required (field virtual_main_data_center)"}), 400

    fn = (upload.filename or "").lower()
    if not (fn.endswith(".csv") or fn.endswith(".xlsx") or fn.endswith(".xls")):
        return jsonify({"error": "File must be .csv, .xlsx, or .xls"}), 400

    archived = archive_upload(
        upload,
        engine=engine,
        module="virtual_mdc",
        source_route=request.path,
        event_id=event_id,
    )

    try:
        rows, parse_stats = etl_data_center.parse_main_data_center_file(
            archived.fresh_stream(), upload.filename or ""
        )
    except ValueError as ve:
        mark_archive_status(archived.id, "failed", engine=engine, error=str(ve))
        return jsonify({"error": str(ve)}), 400

    mark_archive_status(archived.id, "parsed", engine=engine)

    with engine.connect() as conn:
        ev = conn.execute(
            text("SELECT id, kind FROM events WHERE id = :id"),
            {"id": event_id},
        ).fetchone()
        if not ev:
            mark_archive_status(archived.id, "failed", engine=engine, error="event not found")
            return jsonify({"error": "event not found"}), 404
        if str(ev[1]) != "virtual":
            mark_archive_status(archived.id, "failed", engine=engine, error="event must be virtual kind")
            return jsonify({"error": "event must be virtual kind"}), 400

    rows_created = 0
    rows_updated = 0
    try:
        with engine.begin() as conn:
            batch = [{**r, "event_id": event_id} for r in rows]
            for row in batch:
                res = conn.execute(_VIRTUAL_MDC_UPSERT, row)
                was_insert = res.scalar_one()
                if bool(was_insert):
                    rows_created += 1
                else:
                    rows_updated += 1
    except Exception as exc:  # noqa: BLE001
        mark_archive_status(archived.id, "failed", engine=engine, error=str(exc))
        return jsonify({"error": str(exc), "parse_stats": parse_stats}), 500

    rows_written = rows_created + rows_updated
    mark_archive_status(archived.id, "success", engine=engine, rows_written=rows_written)
    payload = {
        "status": "success",
        "rows_created": rows_created,
        "rows_updated": rows_updated,
        "rows_written": rows_written,
        "rows_skipped": int(parse_stats.get("rows_skipped_no_email") or 0),
        "rows_read": int(parse_stats.get("rows_read") or 0),
        "rows_after_dedupe": int(parse_stats.get("rows_after_dedupe") or 0),
        "duplicate_emails_collapsed": int(parse_stats.get("duplicate_emails_collapsed") or 0),
        "archive_path": archived.stored_path,
    }
    return jsonify(payload)


def _import_virtual_challenge_submissions_core():
    """Multi-sheet .xlsx: tabs ``Submission …`` → challenges; rows upserted by team per challenge."""
    event_id = DEFAULT_VIRTUAL_EVENT_ID

    upload = request.files.get("virtual_challenge_submissions")
    if not upload or not upload.filename:
        return jsonify({"error": "An .xlsx file is required (field virtual_challenge_submissions)"}), 400

    fn = (upload.filename or "").lower()
    if not fn.endswith(".xlsx"):
        return jsonify({"error": "File must be .xlsx"}), 400

    archived = archive_upload(
        upload,
        engine=engine,
        module="virtual_challenge_submissions",
        source_route=request.path,
        event_id=event_id,
    )

    with engine.connect() as conn:
        ch_rows = conn.execute(
            text(
                """
                SELECT id, title, import_sheet_suffix
                FROM challenges
                WHERE event_id = :eid
                ORDER BY id
                """
            ),
            {"eid": event_id},
        ).mappings().all()
        challenges = [dict(r) for r in ch_rows]

    if not challenges:
        mark_archive_status(
            archived.id,
            "failed",
            engine=engine,
            error="No challenges defined for this virtual event. Create challenges first.",
        )
        return (
            jsonify(
                {
                    "error": "No challenges defined for this virtual event. Create challenges first.",
                }
            ),
            400,
        )

    try:
        rows, parse_stats = etl_virtual_challenge_submissions.parse_virtual_challenge_submissions_workbook(
            archived.fresh_stream(), upload.filename or "", challenges
        )
    except ValueError as ve:
        mark_archive_status(archived.id, "failed", engine=engine, error=str(ve))
        return jsonify({"error": str(ve)}), 400

    emails_set = sorted({(r.get("leader_email") or "").strip().lower() for r in rows if (r.get("leader_email") or "").strip()})
    if not emails_set:
        mark_archive_status(archived.id, "failed", engine=engine, error="No leader emails in parsed rows")
        return jsonify({"error": "No leader emails in parsed rows"}), 400

    with engine.connect() as conn:
        ev = conn.execute(
            text("SELECT id, kind FROM events WHERE id = :id"),
            {"id": event_id},
        ).fetchone()
        if not ev:
            mark_archive_status(archived.id, "failed", engine=engine, error="event not found")
            return jsonify({"error": "event not found"}), 404
        if str(ev[1]) != "virtual":
            mark_archive_status(archived.id, "failed", engine=engine, error="event must be virtual kind")
            return jsonify({"error": "event must be virtual kind"}), 400

        m_stmt = text(
            f"""
            SELECT id, email_normalized
            FROM {TABLE_VIRTUAL_MDC}
            WHERE event_id = :eid AND email_normalized IN :emails
            """
        ).bindparams(bindparam("emails", expanding=True))
        mrows = conn.execute(m_stmt, {"eid": event_id, "emails": emails_set}).fetchall()
        mdc_by_email = {str(r[1]): int(r[0]) for r in mrows}

    missing = [e for e in emails_set if e not in mdc_by_email]
    if missing:
        msg = (
            "Leader email(s) not found in Virtual Main Data Center for this event "
            f"(showing up to 20 of {len(missing)}): {', '.join(missing[:20])}"
        )
        mark_archive_status(archived.id, "failed", engine=engine, error=msg)
        id_to_title = {int(c["id"]): (c.get("title") or "") for c in challenges}
        sheets_disp: dict[str, dict] = {}
        for sn, info in (parse_stats.get("sheets") or {}).items():
            cid = int(info.get("challenge_id") or 0)
            sheets_disp[str(sn)] = {
                **dict(info),
                "challenge_title": id_to_title.get(cid, ""),
            }
        parse_for_ui = {**parse_stats, "sheets": sheets_disp}
        return jsonify(
            {
                "error": msg,
                "missing_emails": missing,
                "nothing_written": True,
                "target_table": "virtual_challenge_submission_rows",
                "virtual_event_id": event_id,
                "parse_stats": parse_for_ui,
                "rows_ready_to_import": len(rows),
                "archive_path": archived.stored_path,
            }
        ), 400

    mark_archive_status(archived.id, "parsed", engine=engine)

    job_id: int | None = None
    try:
        with engine.begin() as conn:
            res = conn.execute(
                text(
                    """
                    INSERT INTO import_jobs (module, status, started_at, row_counts)
                    VALUES ('virtual_challenge_submissions', 'running', now(), '{}'::jsonb)
                    RETURNING id
                    """
                ),
            )
            job_id = int(res.scalar_one())
            mark_archive_status(archived.id, "parsed", engine=engine, import_job_id=job_id)

            for r in rows:
                email_n = (r.get("leader_email") or "").strip().lower()
                vid = mdc_by_email[email_n]
                params = {
                    "event_id": event_id,
                    "challenge_id": int(r["challenge_id"]),
                    "import_job_id": job_id,
                    "virtual_mdc_registration_id": vid,
                    "source_sheet_name": r["source_sheet_name"],
                    "team_name": (r.get("team_name") or "").strip(),
                    "leader_name": r.get("leader_name"),
                    "leader_email": (r.get("leader_email") or "").strip(),
                    "leader_phone": r.get("leader_phone"),
                    "team_size": r.get("team_size"),
                    "problem_statements": r.get("problem_statements"),
                    "total_score": r.get("total_score"),
                    "deployed_link": r.get("deployed_link"),
                    "linkedin_post": r.get("linkedin_post"),
                    "github_repository_link": r.get("github_repository_link"),
                    "export_created_at": r.get("export_created_at"),
                    "export_created_by_name": r.get("export_created_by_name"),
                    "export_created_by_email": r.get("export_created_by_email"),
                    "export_updated_at": r.get("export_updated_at"),
                    "export_updated_by_name": r.get("export_updated_by_name"),
                    "export_updated_by_email": r.get("export_updated_by_email"),
                }
                conn.execute(_VCSR_UPSERT, params)

            counts = {**parse_stats, "rows_written": len(rows)}
            conn.execute(
                text(
                    """
                    UPDATE import_jobs
                    SET status = 'success', finished_at = now(), row_counts = CAST(:rc AS jsonb), error_message = NULL
                    WHERE id = :jid
                    """
                ),
                {"rc": json.dumps(counts), "jid": job_id},
            )

        mark_archive_status(archived.id, "success", engine=engine, rows_written=len(rows))
        id_to_title = {int(c["id"]): (c.get("title") or "") for c in challenges}
        sheets_disp: dict[str, dict] = {}
        for sn, info in (parse_stats.get("sheets") or {}).items():
            cid = int(info.get("challenge_id") or 0)
            sheets_disp[str(sn)] = {
                **dict(info),
                "challenge_title": id_to_title.get(cid, ""),
            }
        parse_for_ui = {**parse_stats, "sheets": sheets_disp}
        return jsonify(
            {
                "status": "success",
                "import_job_id": job_id,
                "rows_written": len(rows),
                "parse_stats": parse_for_ui,
                "archive_path": archived.stored_path,
                "target_table": "virtual_challenge_submission_rows",
                "virtual_event_id": event_id,
                "merge_policy": (
                    "Upsert: each row is keyed by (challenge_id, team name). "
                    "Re-importing the same team updates the existing row."
                ),
            }
        )
    except Exception as exc:  # noqa: BLE001
        mark_archive_status(archived.id, "failed", engine=engine, error=str(exc))
        if job_id is not None:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        """
                        UPDATE import_jobs
                        SET status = 'failed', finished_at = now(), error_message = :msg
                        WHERE id = :jid
                        """
                    ),
                    {"msg": str(exc), "jid": job_id},
                )
        return jsonify({"error": str(exc), "parse_stats": parse_stats}), 500


def _import_in_person_action_center_core():
    """Two-tab .xlsx: Warm Up Challenge + Main Challenge; rows upserted per PW session (city + date + label) + sheet kind + team."""
    event_id = DEFAULT_IN_PERSON_EVENT_ID
    attendance_city_raw = (request.form.get("attendance_city") or "").strip()

    upload = request.files.get("in_person_action_center")
    if not upload or not upload.filename:
        return jsonify({"error": "An .xlsx file is required (field in_person_action_center)"}), 400
    if not attendance_city_raw:
        return jsonify({"error": "attendance_city is required (select the Prompt War / attendance city)"}), 400

    pw_on = _parse_ipcsr_prompt_war_date_from_form(request.form.get("prompt_war_on"))
    if pw_on is None:
        return jsonify({"error": "prompt_war_on is required (Prompt War date, YYYY-MM-DD)"}), 400
    if pw_on == IPCSR_LEGACY_PROMPT_WAR_DATE:
        return (
            jsonify(
                {
                    "error": (
                        "That Prompt War date is reserved for legacy data. "
                        "Pick the real event date for this import."
                    )
                }
            ),
            400,
        )
    if pw_on > date.today() + timedelta(days=730):
        return jsonify({"error": "Prompt War date is too far in the future."}), 400
    session_label_imp = _normalize_ipcsr_session_label(request.form.get("session_label"))

    fn = (upload.filename or "").lower()
    if not fn.endswith(".xlsx"):
        return jsonify({"error": "File must be .xlsx"}), 400

    with engine.connect() as conn:
        city_opts = _load_mdc_attendance_city_options(conn, event_id, mode="in_person")
    city_canonical = next(
        (c for c in city_opts if c.strip().lower() == attendance_city_raw.strip().lower()),
        None,
    )
    if not city_canonical:
        return (
            jsonify(
                {
                    "error": (
                        "attendance_city is not in the In-person Main Data Center roster "
                        "(no registrations with that attendance city). Import MDC first or pick another city."
                    ),
                    "nothing_written": True,
                }
            ),
            400,
        )

    archived = archive_upload(
        upload,
        engine=engine,
        module="in_person_action_center",
        source_route=request.path,
        event_id=event_id,
    )

    try:
        rows, parse_stats = etl_in_person_challenge_submissions.parse_in_person_action_center_workbook(
            archived.fresh_stream(), upload.filename or ""
        )
    except ValueError as ve:
        mark_archive_status(archived.id, "failed", engine=engine, error=str(ve))
        return jsonify({"error": str(ve)}), 400

    emails_set = sorted(
        {(r.get("leader_email") or "").strip().lower() for r in rows if (r.get("leader_email") or "").strip()}
    )
    if not emails_set:
        mark_archive_status(archived.id, "failed", engine=engine, error="No leader emails in parsed rows")
        return jsonify({"error": "No leader emails in parsed rows"}), 400

    with engine.connect() as conn:
        ev = conn.execute(
            text("SELECT id, kind FROM events WHERE id = :id"),
            {"id": event_id},
        ).fetchone()
        if not ev:
            mark_archive_status(archived.id, "failed", engine=engine, error="event not found")
            return jsonify({"error": "event not found"}), 404
        if str(ev[1]) != "in_person":
            mark_archive_status(archived.id, "failed", engine=engine, error="event must be in_person kind")
            return jsonify({"error": "event must be in_person kind"}), 400

        m_stmt = text(
            f"""
            SELECT id, email_normalized, btrim(COALESCE(attendance_city, '')) AS ac
            FROM {TABLE_IN_PERSON_MDC}
            WHERE event_id = :eid AND email_normalized IN :emails
            """
        ).bindparams(bindparam("emails", expanding=True))
        mrows = conn.execute(m_stmt, {"eid": event_id, "emails": emails_set}).mappings().all()
        mdc_by_email = {str(r["email_normalized"]): int(r["id"]) for r in mrows}
        mdc_ac_by_email = {str(r["email_normalized"]): (str(r["ac"]) or "") for r in mrows}

    missing = [e for e in emails_set if e not in mdc_by_email]
    if missing:
        msg = (
            "Leader email(s) not found in In-person Main Data Center for this event "
            f"(showing up to 20 of {len(missing)}): {', '.join(missing[:20])}"
        )
        mark_archive_status(archived.id, "failed", engine=engine, error=msg)
        sheets_disp: dict[str, dict] = {}
        for sn, info in (parse_stats.get("sheets") or {}).items():
            sk = str(info.get("sheet_kind") or "")
            sheets_disp[str(sn)] = {
                **dict(info),
                "sheet_kind_label": sk,
            }
        parse_for_ui = {**parse_stats, "sheets": sheets_disp}
        return jsonify(
            {
                "error": msg,
                "missing_emails": missing,
                "nothing_written": True,
                "target_table": TABLE_IN_PERSON_CHALLENGE_SUBMISSIONS,
                "in_person_event_id": event_id,
                "parse_stats": parse_for_ui,
                "rows_ready_to_import": len(rows),
                "archive_path": archived.stored_path,
            }
        ), 400

    sel_city_norm = city_canonical.strip().lower()
    mismatched = 0
    for r in rows:
        em = (r.get("leader_email") or "").strip().lower()
        ac = (mdc_ac_by_email.get(em) or "").strip().lower()
        if ac and ac != sel_city_norm:
            mismatched += 1

    parse_stats["mismatched_attendance_city"] = mismatched
    mark_archive_status(archived.id, "parsed", engine=engine)

    job_id: int | None = None
    try:
        with engine.begin() as conn:
            res = conn.execute(
                text(
                    """
                    INSERT INTO import_jobs (module, status, started_at, row_counts)
                    VALUES ('in_person_challenge_submissions', 'running', now(), '{}'::jsonb)
                    RETURNING id
                    """
                ),
            )
            job_id = int(res.scalar_one())
            mark_archive_status(archived.id, "parsed", engine=engine, import_job_id=job_id)

            for r in rows:
                email_n = (r.get("leader_email") or "").strip().lower()
                mid = mdc_by_email[email_n]
                params = {
                    "event_id": event_id,
                    "attendance_city": city_canonical,
                    "prompt_war_on": pw_on,
                    "session_label": session_label_imp,
                    "import_job_id": job_id,
                    "in_person_mdc_registration_id": mid,
                    "sheet_kind": str(r["sheet_kind"]),
                    "source_sheet_name": r["source_sheet_name"],
                    "team_name": (r.get("team_name") or "").strip(),
                    "leader_name": r.get("leader_name"),
                    "leader_email": (r.get("leader_email") or "").strip(),
                    "leader_phone": r.get("leader_phone"),
                    "team_size": r.get("team_size"),
                    "problem_statements": r.get("problem_statements"),
                    "total_score": r.get("total_score"),
                    "deployed_link": r.get("deployed_link"),
                    "deployed_changes_notes": r.get("deployed_changes_notes"),
                    "github_repository_link": r.get("github_repository_link"),
                    "export_created_at": r.get("export_created_at"),
                    "export_created_by_name": r.get("export_created_by_name"),
                    "export_created_by_email": r.get("export_created_by_email"),
                    "export_updated_at": r.get("export_updated_at"),
                    "export_updated_by_name": r.get("export_updated_by_name"),
                    "export_updated_by_email": r.get("export_updated_by_email"),
                }
                conn.execute(_IPCSR_UPSERT, params)

            counts = {**parse_stats, "rows_written": len(rows)}
            conn.execute(
                text(
                    """
                    UPDATE import_jobs
                    SET status = 'success', finished_at = now(), row_counts = CAST(:rc AS jsonb), error_message = NULL
                    WHERE id = :jid
                    """
                ),
                {"rc": json.dumps(counts), "jid": job_id},
            )

        mark_archive_status(archived.id, "success", engine=engine, rows_written=len(rows))
        sheets_disp: dict[str, dict] = {}
        for sn, info in (parse_stats.get("sheets") or {}).items():
            sk = str(info.get("sheet_kind") or "")
            sheets_disp[str(sn)] = {**dict(info), "sheet_kind_label": sk}
        parse_for_ui = {**parse_stats, "sheets": sheets_disp}
        return jsonify(
            {
                "status": "success",
                "import_job_id": job_id,
                "rows_written": len(rows),
                "parse_stats": parse_for_ui,
                "archive_path": archived.stored_path,
                "target_table": TABLE_IN_PERSON_CHALLENGE_SUBMISSIONS,
                "in_person_event_id": event_id,
                "attendance_city": city_canonical,
                "prompt_war_on": pw_on.isoformat(),
                "session_label": session_label_imp,
                "merge_policy": (
                    "Upsert: each row is keyed by (event, attendance city / PW, Prompt War date, optional session label, "
                    "sheet kind warmup|main, team name). Re-importing the same team in the same session updates the row."
                ),
            }
        )
    except Exception as exc:  # noqa: BLE001
        mark_archive_status(archived.id, "failed", engine=engine, error=str(exc))
        if job_id is not None:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        """
                        UPDATE import_jobs
                        SET status = 'failed', finished_at = now(), error_message = :msg
                        WHERE id = :jid
                        """
                    ),
                    {"msg": str(exc), "jid": job_id},
                )
        return jsonify({"error": str(exc), "parse_stats": parse_stats}), 500


@app.post("/admin/import")
def admin_import_in_person():
    out = _import_in_person_core()
    resp, status = out if isinstance(out, tuple) else (out, 200)
    payload = resp.get_json(silent=True) if hasattr(resp, "get_json") else None
    if status != 200:
        msg = (payload or {}).get("error") if isinstance(payload, dict) else str(payload)
        return (
            render_template(
                "admin_result.html",
                title="Import result",
                ok=False,
                message=msg or "Import failed",
                data=payload,
            ),
            status,
        )
    return render_template(
        "admin_result.html",
        title="Import result",
        ok=True,
        message="Import completed",
        data=payload,
    )


@app.post("/api/import/in-person")
def api_import_in_person():
    return _import_in_person_core()


@app.post("/admin/import/in-person/main-data-center")
def admin_import_in_person_data_center():
    out = _import_in_person_main_data_center_core()
    resp, status = out if isinstance(out, tuple) else (out, 200)
    payload = resp.get_json(silent=True) if hasattr(resp, "get_json") else None
    if status != 200:
        msg = (payload or {}).get("error") if isinstance(payload, dict) else str(payload)
        return (
            render_template(
                "main_data_center_import_result.html",
                title="Main Data Center import",
                ok=False,
                message=msg or "Import failed",
                stats=None,
                error_detail=payload if isinstance(payload, dict) else None,
                mdc_result_module="in_person",
            ),
            status,
        )
    return render_template(
        "main_data_center_import_result.html",
        title="Main Data Center import",
        ok=True,
        message="Main Data Center import completed",
        stats=payload,
        error_detail=None,
        mdc_result_module="in_person",
    )


@app.post("/api/import/in-person/main-data-center")
def api_import_in_person_main_data_center():
    return _import_in_person_main_data_center_core()


@app.post("/api/import/in-person/action-center")
def api_import_in_person_action_center():
    return _import_in_person_action_center_core()


@app.post("/admin/import/in-person/action-center")
def admin_import_in_person_action_center():
    out = _import_in_person_action_center_core()
    resp, status = out if isinstance(out, tuple) else (out, 200)
    payload = resp.get_json(silent=True) if hasattr(resp, "get_json") else None
    if status != 200:
        msg = (payload or {}).get("error") if isinstance(payload, dict) else str(payload)
        return (
            render_template(
                "in_person_action_center_import_result.html",
                title="In-person Action Center import",
                ok=False,
                message=msg or "Import failed",
                stats=None,
                error_detail=payload if isinstance(payload, dict) else None,
            ),
            status,
        )
    stats = payload if isinstance(payload, dict) else {}
    return render_template(
        "in_person_action_center_import_result.html",
        title="In-person Action Center import",
        ok=True,
        message="Action Center import completed",
        stats=stats,
        error_detail=None,
    )


@app.get("/api/in-person/attendance-cities")
def api_in_person_attendance_cities():
    eid = request.args.get("inPersonEventId", type=int) or DEFAULT_IN_PERSON_EVENT_ID
    try:
        with engine.connect() as conn:
            ev = conn.execute(
                text("SELECT id, kind FROM events WHERE id = :id"),
                {"id": eid},
            ).fetchone()
            if not ev or str(ev[1]) != "in_person":
                return jsonify({"error": "event must be in_person"}), 400
            cities = _load_mdc_attendance_city_options(conn, eid, mode="in_person")
        return jsonify({"event_id": eid, "attendance_cities": cities})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.get("/api/in-person/action-center/leaderboard")
def api_in_person_action_center_leaderboard():
    eid = request.args.get("inPersonEventId", type=int) or DEFAULT_IN_PERSON_EVENT_ID
    city_raw = (request.args.get("attendance_city") or "").strip() or None
    lim = request.args.get("limit", default=10, type=int) or 10
    pw_arg = request.args.get("prompt_war_on")
    pw_d = _parse_ipcsr_prompt_war_date_from_form(pw_arg) if (pw_arg or "").strip() else None
    lab = _normalize_ipcsr_session_label(request.args.get("session_label"))
    if city_raw and pw_d is None:
        pw_d = IPCSR_LEGACY_PROMPT_WAR_DATE
        lab = ""
    payload = _in_person_submission_leaderboard(
        eid, city_raw, lim, prompt_war_on=pw_d, session_label=lab
    )
    return jsonify(payload)


@app.post("/api/import/virtual/main-data-center")
def api_import_virtual_main_data_center():
    return _import_virtual_main_data_center_core()


@app.post("/admin/import/virtual/main-data-center")
def admin_import_virtual_main_data_center():
    out = _import_virtual_main_data_center_core()
    resp, status = out if isinstance(out, tuple) else (out, 200)
    payload = resp.get_json(silent=True) if hasattr(resp, "get_json") else None
    if status != 200:
        msg = (payload or {}).get("error") if isinstance(payload, dict) else str(payload)
        return (
            render_template(
                "main_data_center_import_result.html",
                title="Virtual Main Data Center import",
                ok=False,
                message=msg or "Import failed",
                stats=None,
                error_detail=payload if isinstance(payload, dict) else None,
                mdc_result_module="virtual",
            ),
            status,
        )
    stats = payload if isinstance(payload, dict) else {}
    return render_template(
        "main_data_center_import_result.html",
        title="Virtual Main Data Center import",
        ok=True,
        message="Virtual Main Data Center import completed",
        stats=stats,
        error_detail=None,
        mdc_result_module="virtual",
    )


@app.post("/api/import/virtual/challenge-submissions")
def api_import_virtual_challenge_submissions():
    return _import_virtual_challenge_submissions_core()


@app.post("/admin/import/virtual/challenge-submissions")
def admin_import_virtual_challenge_submissions():
    out = _import_virtual_challenge_submissions_core()
    resp, status = out if isinstance(out, tuple) else (out, 200)
    payload = resp.get_json(silent=True) if hasattr(resp, "get_json") else None
    if status != 200:
        msg = (payload or {}).get("error") if isinstance(payload, dict) else str(payload)
        return (
            render_template(
                "virtual_challenge_submissions_import_result.html",
                title="Virtual challenge submissions import",
                ok=False,
                message=msg or "Import failed",
                stats=None,
                error_detail=payload if isinstance(payload, dict) else None,
            ),
            status,
        )
    stats = payload if isinstance(payload, dict) else {}
    return render_template(
        "virtual_challenge_submissions_import_result.html",
        title="Virtual challenge submissions import",
        ok=True,
        message="Challenge submissions import completed",
        stats=stats,
        error_detail=None,
    )


@app.get("/api/in-person/main-data-center/registrations/<int:reg_id>")
@audit_view(
    entity="in_person_main_data_center_registrations",
    module="in_person",
    record_pk_fn=lambda reg_id, *a, **kw: {"id": int(reg_id)},
)
def api_in_person_mdc_registration(reg_id: int):
    eid = DEFAULT_IN_PERSON_EVENT_ID
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    f"""
                    SELECT id, event_id, email, form_timestamp, utm_source, utm_medium, utm_campaign,
                           utm_term, utm_content, org_name, org_state, org_city, class_stream, portfolio,
                           domain, designation, designation_years_experience, founded_info, degree, profile_name, full_name, mobile,
                           whatsapp, country, state, city, dob, gender, occupation, github_url,
                           linkedin_url, attendance_city, created_at, updated_at
                    FROM {TABLE_IN_PERSON_MDC}
                    WHERE id = :id AND event_id = :eid
                    """
                ),
                {"id": reg_id, "eid": eid},
            ).mappings().fetchone()
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500
    if not row:
        return jsonify({"error": "not found"}), 404
    base = _serialize_mdc_row_json(dict(row))
    try:
        with engine.connect() as conn:
            subs = conn.execute(
                text(
                    f"""
                    SELECT sheet_kind, attendance_city, team_name, total_score, deployed_link,
                           github_repository_link, source_sheet_name, export_created_at
                    FROM {TABLE_IN_PERSON_CHALLENGE_SUBMISSIONS}
                    WHERE event_id = :eid
                      AND leader_email_normalized = (
                        SELECT email_normalized FROM {TABLE_IN_PERSON_MDC}
                        WHERE id = :rid AND event_id = :eid2
                      )
                    ORDER BY sheet_kind ASC, team_name ASC
                    """
                ),
                {"eid": eid, "eid2": eid, "rid": reg_id},
            ).mappings().all()
        team_submissions = []
        for s in subs:
            d = dict(s)
            ts = d.get("export_created_at")
            if ts is not None and hasattr(ts, "isoformat"):
                d["export_created_at"] = ts.isoformat()
            elif ts is not None:
                d["export_created_at"] = str(ts)
            if d.get("total_score") is not None:
                d["total_score"] = float(d["total_score"])
            team_submissions.append(d)
        base["team_submissions"] = team_submissions
    except Exception as exc:  # noqa: BLE001
        base["team_submissions"] = []
        base["team_submissions_error"] = str(exc)
    return jsonify(base)


@app.get("/api/virtual/main-data-center/registrations/<int:reg_id>")
@audit_view(
    entity="virtual_main_data_center_registrations",
    module="virtual",
    record_pk_fn=lambda reg_id, *a, **kw: {"id": int(reg_id)},
)
def api_virtual_mdc_registration(reg_id: int):
    eid = DEFAULT_VIRTUAL_EVENT_ID
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    f"""
                    SELECT id, event_id, email, form_timestamp, utm_source, utm_medium, utm_campaign,
                           utm_term, utm_content, org_name, org_state, org_city, class_stream, portfolio,
                           domain, designation, designation_years_experience, founded_info, degree, profile_name, full_name, mobile,
                           whatsapp, country, state, city, dob, gender, occupation, github_url,
                           linkedin_url, created_at, updated_at
                    FROM {TABLE_VIRTUAL_MDC}
                    WHERE id = :id AND event_id = :eid
                    """
                ),
                {"id": reg_id, "eid": eid},
            ).mappings().fetchone()
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500
    if not row:
        return jsonify({"error": "not found"}), 404
    return jsonify(_serialize_mdc_row_json(dict(row)))


@app.get("/api/funnel")
def funnel_summary():
    event_id = request.args.get("event_id", type=int)
    if not event_id:
        return jsonify({"error": "event_id is required"}), 400
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT city_id, city_name, rsvp_count, submission_count, conversion_rate
                FROM v_in_person_conversion
                WHERE event_id = :eid
                ORDER BY city_name
                """
            ),
            {"eid": event_id},
        ).mappings().all()
    return jsonify({"event_id": event_id, "cities": [dict(r) for r in rows]})


@app.get("/api/stats/city/<int:city_id>")
def stats_city(city_id: int):
    with engine.connect() as conn:
        city = conn.execute(
            text(
                """
                SELECT c.id, c.event_id, c.name,
                  COALESCE(v.rsvp_count, 0) AS rsvp_count,
                  COALESCE(v.submission_count, 0) AS submission_count,
                  COALESCE(v.conversion_rate, 0) AS conversion_rate
                FROM cities c
                LEFT JOIN v_in_person_conversion v ON v.city_id = c.id
                WHERE c.id = :cid
                """
            ),
            {"cid": city_id},
        ).mappings().fetchone()
        if not city:
            return jsonify({"error": "city not found"}), 404

        mia = conn.execute(
            text(
                """
                SELECT p.id AS participant_id, p.external_user_id, p.display_name
                FROM rsvps r
                JOIN participants p ON p.id = r.participant_id
                WHERE r.city_id = :cid
                  AND NOT EXISTS (
                    SELECT 1 FROM submissions s
                    WHERE s.participant_id = r.participant_id
                      AND s.city_id = r.city_id
                      AND s.event_id = r.event_id
                  )
                ORDER BY p.external_user_id
                """
            ),
            {"cid": city_id},
        ).mappings().all()

    payload = {
        "city": dict(city),
        "missing_in_action": [dict(r) for r in mia],
    }
    return jsonify(payload)


@app.get("/api/leaderboard")
def leaderboard():
    event_id = request.args.get("event_id", type=int)
    challenge_id = request.args.get("challenge_id", type=int)
    limit = request.args.get("limit", default=100, type=int) or 100
    offset = request.args.get("offset", default=0, type=int) or 0
    limit = min(max(limit, 1), 500)
    offset = max(offset, 0)

    if bool(event_id) == bool(challenge_id):
        return jsonify({"error": "Provide exactly one of event_id or challenge_id"}), 400

    with engine.connect() as conn:
        if challenge_id:
            ch = conn.execute(
                text("SELECT id, event_id FROM challenges WHERE id = :id"),
                {"id": challenge_id},
            ).fetchone()
            if not ch:
                return jsonify({"error": "challenge not found"}), 404
            cid, vid = int(ch[0]), int(ch[1])
            rows = conn.execute(
                text(
                    """
                    SELECT ranked.participant_id, ranked.display_name, ranked.score,
                           ranked.rank, ranked.updated_hint
                    FROM (
                      SELECT base.participant_id, base.display_name, base.score,
                             RANK() OVER (ORDER BY base.score DESC) AS rank,
                             base.updated_hint
                      FROM (
                        SELECT p.id AS participant_id,
                               COALESCE(p.display_name, p.external_user_id, 'Participant ' || p.id) AS display_name,
                               COALESCE(SUM(l.delta), 0) AS score,
                               MAX(l.created_at) AS updated_hint
                        FROM registrations reg
                        JOIN participants p ON p.id = reg.participant_id
                        LEFT JOIN credit_ledger l
                          ON l.participant_id = p.id AND l.challenge_id = :cid
                        WHERE reg.event_id = :eid
                        GROUP BY p.id, p.display_name, p.external_user_id
                      ) base
                    ) ranked
                    ORDER BY ranked.rank, ranked.participant_id
                    LIMIT :lim OFFSET :off
                    """
                ),
                {"cid": cid, "eid": vid, "lim": limit, "off": offset},
            ).mappings().all()
            scope = {"challenge_id": cid, "virtual_event_id": vid}
        else:
            rows = conn.execute(
                text(
                    """
                    SELECT ranked.participant_id, ranked.display_name, ranked.score,
                           ranked.rank, ranked.updated_hint
                    FROM (
                      SELECT base.participant_id, base.display_name, base.score,
                             RANK() OVER (ORDER BY base.score DESC) AS rank,
                             base.updated_hint
                      FROM (
                        SELECT p.id AS participant_id,
                               COALESCE(p.display_name, p.external_user_id, 'Participant ' || p.id) AS display_name,
                               COALESCE(pb.balance, 0) AS score,
                               pb.updated_at AS updated_hint
                        FROM registrations reg
                        JOIN participants p ON p.id = reg.participant_id
                        LEFT JOIN participant_balances pb
                          ON pb.participant_id = p.id AND pb.event_id = reg.event_id
                        WHERE reg.event_id = :eid
                      ) base
                    ) ranked
                    ORDER BY ranked.rank, ranked.participant_id
                    LIMIT :lim OFFSET :off
                    """
                ),
                {"eid": event_id, "lim": limit, "off": offset},
            ).mappings().all()
            scope = {"event_id": event_id}

    def _serialize(row):
        d = dict(row)
        if d.get("score") is not None:
            d["score"] = float(d["score"])
        uh = d.get("updated_hint")
        if uh is not None:
            d["updated_hint"] = uh.isoformat() if hasattr(uh, "isoformat") else str(uh)
        d["rank"] = int(d["rank"])
        return d

    return jsonify({"scope": scope, "rows": [_serialize(r) for r in rows]})


@app.get("/api/virtual/submission-leaderboard")
def api_virtual_submission_leaderboard():
    """Team scores from imported XLSX rows; not the credit_ledger leaderboard."""
    eid = request.args.get("virtualEventId", type=int) or DEFAULT_VIRTUAL_EVENT_ID
    cid = request.args.get("challenge_id", type=int)
    if cid is None:
        return jsonify({"error": "challenge_id is required"}), 400
    limit = request.args.get("limit", default=50, type=int) or 50
    offset = request.args.get("offset", default=0, type=int) or 0
    payload = _submission_leaderboard_payload(event_id=eid, challenge_id=cid, limit=limit, offset=offset)
    if payload.get("error") == "challenge not found":
        return jsonify({"error": "challenge not found"}), 404
    if payload.get("error"):
        return jsonify({"error": payload["error"]}), 500
    ch = payload.get("challenge") or {}
    return jsonify(
        {
            "scope": {
                "virtual_event_id": int(eid),
                "challenge_id": int(ch.get("id", cid)),
                "challenge_title": ch.get("title") or "",
            },
            "rows": payload["rows"],
            "total": payload["total"],
            "limit": min(max(int(limit), 1), 500),
            "offset": max(int(offset), 0),
        }
    )


@app.get("/api/virtual/global-submission-leaderboard")
def api_virtual_global_submission_leaderboard():
    """Per-team average score across virtual arena challenges for one event (see ``_virtual_global_submission_leaderboard``)."""
    eid = request.args.get("virtualEventId", type=int) or DEFAULT_VIRTUAL_EVENT_ID
    limit = request.args.get("limit", default=50, type=int) or 50
    offset = request.args.get("offset", default=0, type=int) or 0
    payload = _virtual_global_submission_leaderboard(
        event_id=int(eid), limit=limit, offset=offset
    )
    if payload.get("error"):
        return jsonify({"error": payload["error"]}), 500
    return jsonify(
        {
            "scope": payload.get("scope") or {"virtual_event_id": int(eid), "global": True},
            "rows": payload["rows"],
            "total": payload["total"],
            "limit": min(max(int(limit), 1), 500),
            "offset": max(int(offset), 0),
        }
    )


@app.post("/api/credits/grant")
def credits_grant():
    body = request.get_json(silent=True) or {}
    participant_id = body.get("participant_id")
    delta = body.get("delta")
    reason = body.get("reason")
    challenge_id = body.get("challenge_id")
    event_id = body.get("event_id")
    idempotency_key = body.get("idempotency_key")

    if participant_id is None or delta is None or not reason:
        return jsonify({"error": "participant_id, delta, and reason are required"}), 400
    try:
        participant_id = int(participant_id)
        delta_dec = Decimal(str(delta))
    except Exception:  # noqa: BLE001
        return jsonify({"error": "invalid participant_id or delta"}), 400

    if challenge_id is not None:
        try:
            challenge_id = int(challenge_id)
        except Exception:  # noqa: BLE001
            return jsonify({"error": "invalid challenge_id"}), 400
    if event_id is not None:
        try:
            event_id = int(event_id)
        except Exception:  # noqa: BLE001
            return jsonify({"error": "invalid event_id"}), 400

    ev_for_balance: int | None = None
    with engine.connect() as conn:
        if idempotency_key:
            exists = conn.execute(
                text("SELECT 1 FROM credit_ledger WHERE idempotency_key = :k LIMIT 1"),
                {"k": idempotency_key},
            ).fetchone()
            if exists:
                return jsonify({"ok": True, "duplicate": True})

        if challenge_id is not None:
            row = conn.execute(
                text("SELECT event_id FROM challenges WHERE id = :id"),
                {"id": challenge_id},
            ).fetchone()
            if not row:
                return jsonify({"error": "challenge not found"}), 404
            ev_for_balance = int(row[0])
        elif event_id is not None:
            row = conn.execute(
                text("SELECT id, kind FROM events WHERE id = :id"),
                {"id": event_id},
            ).fetchone()
            if not row:
                return jsonify({"error": "event not found"}), 404
            if str(row[1]) != "virtual":
                return jsonify({"error": "event_id must reference a virtual event"}), 400
            ev_for_balance = int(row[0])
        else:
            return jsonify({"error": "challenge_id or event_id is required for balance scope"}), 400

        force_ineligible = bool(body.get("force"))
        if challenge_id is not None and not _is_participant_eligible_for_challenge(
            conn, challenge_id, participant_id
        ):
            if not force_ineligible:
                return jsonify({
                    "error": "participant ineligible for challenge",
                    "challenge_id": challenge_id,
                    "participant_id": participant_id,
                    "hint": "Registrant must exist in virtual_main_data_center_registrations "
                            "with form_timestamp <= challenges.closes_at. Pass force=true to override.",
                }), 409
            extra_meta_force = True
        else:
            extra_meta_force = False

    metadata_payload = dict(body.get("metadata") or {})
    if extra_meta_force:
        metadata_payload["force_ineligible"] = True

    try:
        with engine.begin() as conn:
            ledger_id = conn.execute(
                text(
                    """
                    INSERT INTO credit_ledger (participant_id, challenge_id, delta, reason, metadata, idempotency_key)
                    VALUES (:pid, :cid, :delta, :reason, CAST(:meta AS jsonb), :ikey)
                    RETURNING id
                    """
                ),
                {
                    "pid": participant_id,
                    "cid": challenge_id,
                    "delta": float(delta_dec),
                    "reason": str(reason),
                    "meta": json.dumps(metadata_payload),
                    "ikey": idempotency_key,
                },
            ).scalar_one()

            conn.execute(
                text(
                    """
                    INSERT INTO participant_balances (participant_id, event_id, balance, updated_at)
                    VALUES (:pid, :eid, :delta, now())
                    ON CONFLICT (participant_id, event_id) DO UPDATE
                    SET balance = participant_balances.balance + EXCLUDED.balance,
                        updated_at = now()
                    """
                ),
                {"pid": participant_id, "eid": ev_for_balance, "delta": float(delta_dec)},
            )

        return jsonify({"ok": True, "ledger_id": int(ledger_id), "event_id": ev_for_balance})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.get("/api/distribution")
def credits_distribution():
    event_id = request.args.get("event_id", type=int)
    challenge_id = request.args.get("challenge_id", type=int)
    bins = request.args.get("bins", default=10, type=int) or 10
    bins = min(max(bins, 3), 50)

    if bool(event_id) == bool(challenge_id):
        return jsonify({"error": "Provide exactly one of event_id or challenge_id"}), 400

    with engine.connect() as conn:
        if challenge_id:
            ch = conn.execute(
                text("SELECT id, event_id FROM challenges WHERE id = :id"),
                {"id": challenge_id},
            ).fetchone()
            if not ch:
                return jsonify({"error": "challenge not found"}), 404
            cid, vid = int(ch[0]), int(ch[1])
            scores = conn.execute(
                text(
                    """
                    SELECT COALESCE(SUM(l.delta), 0) AS score
                    FROM registrations reg
                    JOIN participants p ON p.id = reg.participant_id
                    LEFT JOIN credit_ledger l ON l.participant_id = p.id AND l.challenge_id = :cid
                    WHERE reg.event_id = :eid
                    GROUP BY p.id
                    """
                ),
                {"cid": cid, "eid": vid},
            ).scalars().all()
            scope = {"challenge_id": cid}
        else:
            scores = conn.execute(
                text(
                    """
                    SELECT COALESCE(pb.balance, 0) AS score
                    FROM registrations reg
                    JOIN participants p ON p.id = reg.participant_id
                    LEFT JOIN participant_balances pb
                      ON pb.participant_id = p.id AND pb.event_id = reg.event_id
                    WHERE reg.event_id = :eid
                    """
                ),
                {"eid": event_id},
            ).scalars().all()
            scope = {"event_id": event_id}

    vals = [float(s) for s in scores]
    if not vals:
        return jsonify({"scope": scope, "bins": [], "min": 0, "max": 0})

    vmin, vmax = min(vals), max(vals)
    if vmin == vmax:
        return jsonify(
            {
                "scope": scope,
                "bins": [{"low": vmin, "high": vmax, "count": len(vals)}],
                "min": vmin,
                "max": vmax,
            }
        )

    width = (vmax - vmin) / bins
    bucket_counts = [0 for _ in range(bins)]
    for v in vals:
        idx = int((v - vmin) / width) if width > 0 else 0
        if idx >= bins:
            idx = bins - 1
        bucket_counts[idx] += 1

    out_bins = []
    for i in range(bins):
        low = vmin + i * width
        high = vmin + (i + 1) * width if i < bins - 1 else vmax
        out_bins.append({"low": low, "high": high, "count": bucket_counts[i]})

    return jsonify({"scope": scope, "bins": out_bins, "min": vmin, "max": vmax})


@app.get("/api/import/latest")
def import_latest():
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT id, module, status, started_at, finished_at, error_message, row_counts, created_at
                FROM import_jobs
                ORDER BY id DESC
                LIMIT 1
                """
            ),
        ).mappings().fetchone()
    if not row:
        return jsonify({"job": None})
    d = dict(row)
    for k in ("started_at", "finished_at", "created_at"):
        if d.get(k) is not None and hasattr(d[k], "isoformat"):
            d[k] = d[k].isoformat()
    return jsonify({"job": d})


def _fmt_int(n: int) -> str:
    return f"{int(n):,}"


def _fmt_credits(n: float) -> str:
    x = float(n)
    sign = "-" if x < 0 else ""
    ax = abs(x)
    if ax >= 1_000_000:
        s = f"{ax / 1_000_000:.1f}M"
        return sign + s.replace(".0M", "M")
    if ax >= 1000:
        v = ax / 1000.0
        s = f"{v:.1f}k"
        if s.endswith(".0k"):
            s = f"{int(round(v))}k"
        return sign + s
    if ax == int(ax):
        return sign + _fmt_int(int(ax))
    return sign + f"{ax:,.2f}"


def _load_mdc_brief(event_id: int, *, mode: str) -> dict:
    """Compact Main Data Center stats for the Overview dashboard.

    Keeps query count low (~5) per module so the overview stays cheap.
    """
    table = _mdc_table_for_mode(mode)
    is_virtual = mode == "virtual"
    out: dict = {
        "total": 0,
        "last7": 0,
        "top_city": None,
        "top_city_count": 0,
        "top_state": None,
        "top_state_count": 0,
        "average_age": None,
        "error": None,
    }
    try:
        with engine.connect() as conn:
            out["total"] = int(
                conn.execute(
                    text(f"SELECT COUNT(*) FROM {table} WHERE event_id = :e"),
                    {"e": event_id},
                ).scalar()
                or 0
            )
            out["last7"] = int(
                conn.execute(
                    text(
                        f"""
                        SELECT COUNT(*) FROM {table}
                        WHERE event_id = :e
                          AND form_timestamp IS NOT NULL
                          AND form_timestamp >= now() - interval '7 days'
                        """
                    ),
                    {"e": event_id},
                ).scalar()
                or 0
            )
            if is_virtual:
                city_expr = "NULLIF(btrim(city), '')"
            else:
                city_expr = "COALESCE(NULLIF(btrim(attendance_city), ''), NULLIF(btrim(city), ''))"
            row = conn.execute(
                text(
                    f"""
                    SELECT {city_expr} AS label, COUNT(*)::BIGINT AS cnt
                    FROM {table}
                    WHERE event_id = :e
                    GROUP BY 1
                    HAVING {city_expr} IS NOT NULL
                    ORDER BY cnt DESC, label ASC
                    LIMIT 1
                    """
                ),
                {"e": event_id},
            ).fetchone()
            if row and row[0]:
                out["top_city"] = str(row[0])
                out["top_city_count"] = int(row[1] or 0)
            row = conn.execute(
                text(
                    f"""
                    SELECT INITCAP(lower(btrim(state))) AS label, COUNT(*)::BIGINT AS cnt
                    FROM {table}
                    WHERE event_id = :e AND state IS NOT NULL AND btrim(state) <> ''
                    GROUP BY 1
                    ORDER BY cnt DESC, label ASC
                    LIMIT 1
                    """
                ),
                {"e": event_id},
            ).fetchone()
            if row and row[0]:
                out["top_state"] = str(row[0])
                out["top_state_count"] = int(row[1] or 0)
            avg = conn.execute(
                text(
                    f"""
                    SELECT AVG(EXTRACT(YEAR FROM AGE(CURRENT_DATE, dob)))::double precision
                    FROM {table}
                    WHERE event_id = :e AND dob IS NOT NULL
                    """
                ),
                {"e": event_id},
            ).scalar()
            if avg is not None:
                out["average_age"] = round(float(avg), 1)
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)
    return out


def _empty_score_rollup() -> dict[str, object]:
    dash = "—"
    return {
        "error": None,
        "row_n": 0,
        "scored_n": 0,
        "min_score": None,
        "max_score": None,
        "avg_score": None,
        "stddev_score": None,
        "challenge_id": None,
        "challenge_title": None,
        "min_score_fmt": dash,
        "max_score_fmt": dash,
        "avg_score_fmt": dash,
        "stddev_score_fmt": dash,
    }


def _fmt_score_g(v: object | None) -> str:
    if v is None:
        return "—"
    try:
        x = float(v)
    except (TypeError, ValueError):
        return "—"
    s = f"{x:.4f}".rstrip("0").rstrip(".")
    return s if s else "0"


def _virtual_challenge_submission_score_rollup(
    conn, event_id: int, challenge_id: int | None
) -> dict[str, object]:
    """MIN/MAX/AVG/STDDEV of ``total_score`` on imported virtual arena rows (optionally one challenge)."""
    out: dict[str, object] = dict(_empty_score_rollup())
    out["challenge_id"] = int(challenge_id) if challenge_id is not None else None
    try:
        eid = int(event_id)
        params: dict[str, object] = {"eid": eid}
        ch_sql = ""
        if challenge_id is not None:
            cid = int(challenge_id)
            ch_sql = " AND r.challenge_id = :cid"
            params["cid"] = cid
            tr = conn.execute(
                text("SELECT title FROM challenges WHERE id = :cid AND event_id = :eid"),
                {"cid": cid, "eid": eid},
            ).fetchone()
            out["challenge_title"] = str(tr[0]) if tr and tr[0] is not None else None
        row = conn.execute(
            text(
                f"""
                SELECT
                  COUNT(*)::bigint AS row_n,
                  COUNT(*) FILTER (WHERE r.total_score IS NOT NULL)::bigint AS scored_n,
                  MIN(r.total_score)::double precision AS min_score,
                  MAX(r.total_score)::double precision AS max_score,
                  AVG(r.total_score)::double precision AS avg_score,
                  STDDEV_POP(r.total_score)::double precision AS stddev_score
                FROM virtual_challenge_submission_rows r
                INNER JOIN challenges c ON c.id = r.challenge_id AND c.event_id = r.event_id
                INNER JOIN events e ON e.id = r.event_id AND e.kind = 'virtual'
                WHERE r.event_id = :eid
                {ch_sql}
                """
            ),
            params,
        ).mappings().fetchone()
        if not row:
            return out
        out["row_n"] = int(row["row_n"] or 0)
        out["scored_n"] = int(row["scored_n"] or 0)
        for k in ("min_score", "max_score", "avg_score", "stddev_score"):
            v = row.get(k)
            fv = float(v) if v is not None else None
            out[k] = fv
            out[f"{k}_fmt"] = _fmt_score_g(v)
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)
    return out


def _in_person_ipcsr_score_rollup(conn, event_id: int) -> dict[str, object]:
    """MIN/MAX/AVG/STDDEV of ``total_score`` on main-sheet in-person Action Center imports."""
    out: dict[str, object] = dict(_empty_score_rollup())
    try:
        eid = int(event_id)
        row = conn.execute(
            text(
                """
                SELECT
                  COUNT(*)::bigint AS row_n,
                  COUNT(*) FILTER (WHERE total_score IS NOT NULL)::bigint AS scored_n,
                  MIN(total_score)::double precision AS min_score,
                  MAX(total_score)::double precision AS max_score,
                  AVG(total_score)::double precision AS avg_score,
                  STDDEV_POP(total_score)::double precision AS stddev_score
                FROM in_person_challenge_submission_rows
                WHERE event_id = :eid AND sheet_kind = 'main'
                """
            ),
            {"eid": eid},
        ).mappings().fetchone()
        if not row:
            return out
        out["row_n"] = int(row["row_n"] or 0)
        out["scored_n"] = int(row["scored_n"] or 0)
        for k in ("min_score", "max_score", "avg_score", "stddev_score"):
            v = row.get(k)
            out[k] = float(v) if v is not None else None
            out[f"{k}_fmt"] = _fmt_score_g(v)
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)
    return out


def _fetch_overview_stats(
    in_person_event_id: int,
    virtual_event_id: int,
    *,
    arena_challenge_id: int | None = None,
) -> dict:
    req = int(arena_challenge_id) if arena_challenge_id is not None else None
    resolved_cid, _chs = _resolve_virtual_arena_challenge_id(
        virtual_event_id,
        requested=req,
    )
    overview_arena_cid = int(resolved_cid) if resolved_cid is not None else int(DEFAULT_CHALLENGE_ID)
    try:
        with engine.connect() as conn:
            rsvp_d = int(
                conn.execute(
                    text("SELECT COUNT(DISTINCT participant_id) FROM rsvps WHERE event_id = :e"),
                    {"e": in_person_event_id},
                ).scalar()
                or 0
            )
            sub_d = int(
                conn.execute(
                    text("SELECT COUNT(DISTINCT participant_id) FROM submissions WHERE event_id = :e"),
                    {"e": in_person_event_id},
                ).scalar()
                or 0
            )
            reg_v = int(
                conn.execute(
                    text("SELECT COUNT(*) FROM registrations WHERE event_id = :v"),
                    {"v": virtual_event_id},
                ).scalar()
                or 0
            )
            cred = float(
                conn.execute(
                    text("SELECT COALESCE(SUM(balance), 0) FROM participant_balances WHERE event_id = :v"),
                    {"v": virtual_event_id},
                ).scalar()
                or 0
            )
            live_ch = int(
                conn.execute(
                    text("SELECT COUNT(*) FROM challenges WHERE event_id = :v AND status = 'live'"),
                    {"v": virtual_event_id},
                ).scalar()
                or 0
            )
            v_stats_global = _virtual_challenge_submission_score_rollup(conn, virtual_event_id, None)
            if resolved_cid is not None:
                v_stats_challenge = _virtual_challenge_submission_score_rollup(
                    conn, virtual_event_id, int(resolved_cid)
                )
            else:
                v_stats_challenge = dict(_empty_score_rollup())
            ipcsr_stats = _in_person_ipcsr_score_rollup(conn, in_person_event_id)
        total_reg = rsvp_d + reg_v
        conv = (100.0 * sub_d / rsvp_d) if rsvp_d else 0.0
        mdc_ip = _load_mdc_brief(in_person_event_id, mode="in_person")
        mdc_v = _load_mdc_brief(virtual_event_id, mode="virtual")
        mdc_total = (mdc_ip.get("total") or 0) + (mdc_v.get("total") or 0)
        mdc_last7 = (mdc_ip.get("last7") or 0) + (mdc_v.get("last7") or 0)
        ip_ac_global = _in_person_submission_leaderboard(in_person_event_id, None, 10)
        v_ac_global = _virtual_global_submission_leaderboard(event_id=virtual_event_id, limit=10)
        ip_ac_cities = _in_person_pw_options(in_person_event_id)
        if resolved_cid is not None:
            v_arena_top3 = _submission_leaderboard_payload(
                event_id=int(virtual_event_id),
                challenge_id=int(resolved_cid),
                limit=10,
                offset=0,
            )
        else:
            v_arena_top3 = {"rows": [], "total": 0, "error": None, "challenge": None}
        return {
            "total_registrations_fmt": _fmt_int(total_reg),
            "submissions_fmt": _fmt_int(sub_d),
            "credits_fmt": _fmt_credits(cred),
            "in_person_rsvps_fmt": _fmt_int(rsvp_d),
            "in_person_submissions_fmt": _fmt_int(sub_d),
            "in_person_conversion_fmt": f"{conv:.1f}%",
            "virtual_registrations_fmt": _fmt_int(reg_v),
            "live_challenges_fmt": _fmt_int(live_ch),
            "mdc_total_fmt": _fmt_int(mdc_total),
            "mdc_last7_fmt": _fmt_int(mdc_last7),
            "mdc_in_person": {
                "total_fmt": _fmt_int(mdc_ip.get("total") or 0),
                "last7_fmt": _fmt_int(mdc_ip.get("last7") or 0),
                "top_city": mdc_ip.get("top_city") or "—",
                "top_city_count_fmt": _fmt_int(mdc_ip.get("top_city_count") or 0),
                "top_state": mdc_ip.get("top_state") or "—",
                "top_state_count_fmt": _fmt_int(mdc_ip.get("top_state_count") or 0),
                "average_age": mdc_ip.get("average_age"),
                "error": mdc_ip.get("error"),
            },
            "mdc_virtual": {
                "total_fmt": _fmt_int(mdc_v.get("total") or 0),
                "last7_fmt": _fmt_int(mdc_v.get("last7") or 0),
                "top_city": mdc_v.get("top_city") or "—",
                "top_city_count_fmt": _fmt_int(mdc_v.get("top_city_count") or 0),
                "top_state": mdc_v.get("top_state") or "—",
                "top_state_count_fmt": _fmt_int(mdc_v.get("top_state_count") or 0),
                "average_age": mdc_v.get("average_age"),
                "error": mdc_v.get("error"),
            },
            "in_person_ac_global_top10": ip_ac_global,
            "virtual_ac_global_top10": v_ac_global,
            "in_person_ac_cities": ip_ac_cities,
            "virtual_score_stats_global": v_stats_global,
            "virtual_score_stats_challenge": v_stats_challenge,
            "in_person_action_score_stats": ipcsr_stats,
            "virtual_arena_top3": v_arena_top3,
            "overview_arena_challenge_id": overview_arena_cid,
        }
    except Exception as exc:  # noqa: BLE001
        err = "—"
        empty_mdc = {
            "total_fmt": err,
            "last7_fmt": err,
            "top_city": err,
            "top_city_count_fmt": err,
            "top_state": err,
            "top_state_count_fmt": err,
            "average_age": None,
            "error": None,
        }
        return {
            "total_registrations_fmt": err,
            "submissions_fmt": err,
            "credits_fmt": err,
            "in_person_rsvps_fmt": err,
            "in_person_submissions_fmt": err,
            "in_person_conversion_fmt": err,
            "virtual_registrations_fmt": err,
            "live_challenges_fmt": err,
            "mdc_total_fmt": err,
            "mdc_last7_fmt": err,
            "mdc_in_person": dict(empty_mdc),
            "mdc_virtual": dict(empty_mdc),
            "in_person_ac_global_top10": {"rows": [], "total": 0, "error": str(exc), "scope": {}},
            "virtual_ac_global_top10": {"rows": [], "total": 0, "error": str(exc), "scope": {}},
            "in_person_ac_cities": [],
            "virtual_score_stats_global": dict(_empty_score_rollup(), error=str(exc)),
            "virtual_score_stats_challenge": dict(_empty_score_rollup(), error=str(exc)),
            "in_person_action_score_stats": dict(_empty_score_rollup(), error=str(exc)),
            "virtual_arena_top3": {"rows": [], "total": 0, "error": str(exc), "challenge": None},
            "overview_arena_challenge_id": int(DEFAULT_CHALLENGE_ID),
            "error": str(exc),
        }


def _audit_logs_schema_ok(conn) -> bool:
    row = conn.execute(
        text(
            "SELECT to_regclass('audit.audit_events') IS NOT NULL "
            "AND to_regclass('audit.audit_data_changes') IS NOT NULL"
        )
    ).scalar()
    return bool(row)


def _fmt_audit_ts(value: datetime | None) -> str:
    if value is None:
        return "—"
    if getattr(value, "tzinfo", None) is not None:
        value = value.astimezone(timezone.utc)
    else:
        value = value.replace(tzinfo=timezone.utc)
    return value.strftime("%Y-%m-%d %H:%M:%S") + " UTC"


def _parse_logs_window_hours(raw: str | None) -> int | None:
    """Returns None for all-time queries, otherwise positive hours (capped)."""
    if raw is None or (isinstance(raw, str) and raw.strip() == ""):
        return 168
    try:
        v = int(str(raw).strip())
    except ValueError:
        return 168
    if v <= 0:
        return None
    return min(v, 24 * 90)


def _activity_action_options(conn, since: datetime | None) -> list[str]:
    wh = ["1=1"]
    params: dict = {}
    if since is not None:
        wh.append("occurred_at >= :since")
        params["since"] = since
    sql = (
        "SELECT DISTINCT action FROM audit.audit_events WHERE "
        + " AND ".join(wh)
        + " ORDER BY 1 LIMIT 48"
    )
    rows = conn.execute(text(sql), params).fetchall()
    return [str(r[0]) for r in rows if r and r[0]]


def _load_overview_activity_logs(
    conn,
    *,
    q: str,
    page: int,
    per_page: int,
    since: datetime | None,
    action_eq: str,
) -> dict[str, object]:
    wh: list[str] = ["1=1"]
    params: dict[str, object] = {}
    if since is not None:
        wh.append("occurred_at >= :since")
        params["since"] = since
    if action_eq:
        wh.append("action = :action_eq")
        params["action_eq"] = action_eq[:120]
    if q:
        params["pat"] = f"%{q}%"
        wh.append(
            "("
            "action ILIKE :pat OR COALESCE(endpoint, '') ILIKE :pat OR COALESCE(url, '') ILIKE :pat "
            "OR COALESCE(principal, '') ILIKE :pat OR COALESCE(principal_email, '') ILIKE :pat "
            "OR COALESCE(request_id, '') ILIKE :pat OR COALESCE(sql_kind, '') ILIKE :pat "
            "OR COALESCE(sql_statement, '') ILIKE :pat OR COALESCE(module, '') ILIKE :pat "
            "OR COALESCE(entity, '') ILIKE :pat OR COALESCE(user_agent, '') ILIKE :pat "
            "OR COALESCE(source, '') ILIKE :pat OR COALESCE(extra::text, '') ILIKE :pat"
            ")"
        )
    where_sql = " AND ".join(wh)
    count_sql = f"SELECT COUNT(*)::BIGINT FROM audit.audit_events WHERE {where_sql}"
    total = int(conn.execute(text(count_sql), params).scalar() or 0)
    offset = max(0, (page - 1) * per_page)
    list_sql = f"""
        SELECT occurred_at, action, endpoint, url, http_method, status, latency_ms::float,
               principal, principal_email, request_id, sql_kind,
               LEFT(COALESCE(sql_statement, ''), 400) AS sql_snip,
               module, entity, source, extra
        FROM audit.audit_events
        WHERE {where_sql}
        ORDER BY occurred_at DESC
        LIMIT :lim OFFSET :off
    """
    params_rows = dict(params)
    params_rows["lim"] = per_page
    params_rows["off"] = offset
    rows_out: list[dict[str, object]] = []
    for r in conn.execute(text(list_sql), params_rows).mappings().all():
        extra = r.get("extra")
        if extra is not None and not isinstance(extra, str):
            try:
                extra_s = json.dumps(extra)[:500]
            except (TypeError, ValueError):
                extra_s = str(extra)[:500]
        else:
            extra_s = (str(extra) if extra is not None else "")[:500]
        lat = r.get("latency_ms")
        rows_out.append(
            {
                "occurred_at_fmt": _fmt_audit_ts(r["occurred_at"]),
                "action": r["action"] or "",
                "endpoint": r["endpoint"] or "",
                "url": r["url"] or "",
                "http_method": r["http_method"] or "",
                "status": r["status"],
                "latency_ms_fmt": "" if lat is None else f"{float(lat):.1f}",
                "principal": r["principal"] or "",
                "principal_email": r["principal_email"] or "",
                "request_id": r["request_id"] or "",
                "sql_kind": r["sql_kind"] or "",
                "sql_snip": r["sql_snip"] or "",
                "module": r["module"] or "",
                "entity": r["entity"] or "",
                "source": r["source"] or "",
                "extra_snip": extra_s,
            }
        )
    total_pages = max(1, (total + per_page - 1) // per_page) if total else 1
    return {
        "error": None,
        "rows": rows_out,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
        "search": q,
        "action": action_eq,
        "action_options": _activity_action_options(conn, since),
    }


def _load_overview_data_logs(
    conn,
    *,
    q: str,
    page: int,
    per_page: int,
    since: datetime | None,
) -> dict[str, object]:
    wh: list[str] = ["1=1"]
    params: dict[str, object] = {}
    if since is not None:
        wh.append("occurred_at >= :since")
        params["since"] = since
    if q:
        params["pat"] = f"%{q}%"
        wh.append(
            "("
            "table_name ILIKE :pat OR schema_name ILIKE :pat OR op ILIKE :pat "
            "OR COALESCE(principal, '') ILIKE :pat OR COALESCE(principal_email, '') ILIKE :pat "
            "OR COALESCE(request_id, '') ILIKE :pat OR COALESCE(source, '') ILIKE :pat "
            "OR COALESCE(record_pk::text, '') ILIKE :pat OR COALESCE(changed_columns::text, '') ILIKE :pat "
            "OR COALESCE(changes::text, '') ILIKE :pat OR COALESCE(statement_tag, '') ILIKE :pat"
            ")"
        )
    where_sql = " AND ".join(wh)
    total = int(conn.execute(text(f"SELECT COUNT(*)::BIGINT FROM audit.audit_data_changes WHERE {where_sql}"), params).scalar() or 0)
    offset = max(0, (page - 1) * per_page)
    list_sql = f"""
        SELECT occurred_at, schema_name, table_name, op, record_pk, changed_columns,
               LEFT(COALESCE(changes::text, ''), 350) AS changes_snip,
               principal, principal_email, request_id, source
        FROM audit.audit_data_changes
        WHERE {where_sql}
        ORDER BY occurred_at DESC
        LIMIT :lim OFFSET :off
    """
    params_rows = dict(params)
    params_rows["lim"] = per_page
    params_rows["off"] = offset
    rows_out: list[dict[str, object]] = []
    for r in conn.execute(text(list_sql), params_rows).mappings().all():
        pk = r.get("record_pk")
        if pk is not None and not isinstance(pk, str):
            try:
                pk_s = json.dumps(pk)
            except (TypeError, ValueError):
                pk_s = str(pk)
        else:
            pk_s = str(pk) if pk is not None else ""
        cols = r.get("changed_columns")
        if isinstance(cols, list):
            cols_s = ", ".join(str(c) for c in cols[:40])
            if len(cols) > 40:
                cols_s += ", …"
        elif cols is None:
            cols_s = ""
        else:
            cols_s = str(cols)
        rows_out.append(
            {
                "occurred_at_fmt": _fmt_audit_ts(r["occurred_at"]),
                "table_ref": f'{r["schema_name"]}.{r["table_name"]}',
                "op": r["op"] or "",
                "record_pk": pk_s[:400],
                "changed_columns": cols_s[:400],
                "changes_snip": (r["changes_snip"] or "")[:400],
                "principal": r["principal"] or "",
                "principal_email": r["principal_email"] or "",
                "request_id": r["request_id"] or "",
                "source": r["source"] or "",
            }
        )
    total_pages = max(1, (total + per_page - 1) // per_page) if total else 1
    return {
        "error": None,
        "rows": rows_out,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
        "search": q,
    }


def _load_funnel_bundle(in_person_event_id: int) -> tuple[dict, list[str], list[float]]:
    funnel: dict = {"cities": [], "error": None}
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT city_id, city_name, rsvp_count, submission_count, conversion_rate
                    FROM v_in_person_conversion
                    WHERE event_id = :eid
                    ORDER BY city_name
                    """
                ),
                {"eid": in_person_event_id},
            ).mappings().all()
        funnel = {"event_id": in_person_event_id, "cities": [dict(r) for r in rows]}
    except Exception as exc:  # noqa: BLE001
        funnel = {"cities": [], "error": str(exc)}
    cities = funnel.get("cities") or []
    labels = [str(c["city_name"]) for c in cities]
    rates = [float(c.get("conversion_rate") or 0) for c in cities]
    return funnel, labels, rates


def _parse_mdc_dashboard_iso_date(raw: str | None) -> date | None:
    """Parse ``YYYY-MM-DD`` from query string for MDC dashboard range."""
    s = (raw or "").strip()[:16]
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


_MDC_STATS_TS_TAIL = (
    " AND form_timestamp IS NOT NULL"
    " AND (form_timestamp AT TIME ZONE 'Asia/Kolkata')::date BETWEEN :mdc_d0 AND :mdc_d1"
)


def _normalize_mdc_stats_date_range(
    raw_from: date | None,
    raw_to: date | None,
    data_min: date | None,
    data_max: date | None,
) -> tuple[date | None, date | None]:
    """Return inclusive ``(from, to)`` IST calendar dates, or ``(None, None)`` for 'all data'."""
    if data_min is None or data_max is None:
        return None, None
    if raw_from is None and raw_to is None:
        return None, None
    d0 = raw_from or data_min
    d1 = raw_to if raw_to is not None else (raw_from or data_max)
    if d0 > d1:
        d0, d1 = d1, d0
    if d0 < data_min:
        d0 = data_min
    if d1 > data_max:
        d1 = data_max
    if d0 > d1:
        d0 = d1
    if (d1 - d0).days > 400:
        d1 = d0 + timedelta(days=400)
    return d0, d1


def _load_mdc_stats(
    event_id: int,
    *,
    mode: str = "in_person",
    date_from: date | None = None,
    date_to: date | None = None,
) -> dict:
    """Aggregates for Main Data Center registrations (in-person or virtual physical table).

    Optional ``date_from`` / ``date_to`` (IST calendar dates) restrict rows to registrations whose
    ``form_timestamp`` falls on those days in Asia/Kolkata. When omitted, all rows are included.
    """
    table = _mdc_table_for_mode(mode)
    is_virtual = mode == "virtual"
    out: dict = {
        "error": None,
        "total_registrations": 0,
        "with_attendance_city": 0,
        "skip_attendance_city": bool(is_virtual),
        "distinct_countries": 0,
        "distinct_states": 0,
        "attendance_cities": [],
        "utm_sources": [],
        "last_updated": None,
        "chart_date_min": None,
        "chart_date_max": None,
        "mdc_date_from": None,
        "mdc_date_to": None,
        "mdc_filter_by_registration_date": False,
        # Dashboard analytics (charts / pills)
        "pill_top_city": None,
        "pill_top_city_count": 0,
        "pill_top_state": None,
        "pill_top_state_count": 0,
        "average_age": None,
        "with_dob_count": 0,
        "registrations_last_7_days": 0,
        "timeline_labels": [],
        "timeline_counts": [],
        "hourly_counts": [0] * 24,
        "state_distribution": [],
        "city_pivot": [],
        "gender_breakdown": [],
        "top_occupations": [],
    }
    try:
        with engine.connect() as conn:
            mm = conn.execute(
                text(
                    f"""
                    SELECT
                      MIN((form_timestamp AT TIME ZONE 'Asia/Kolkata')::date) AS d0,
                      MAX((form_timestamp AT TIME ZONE 'Asia/Kolkata')::date) AS d1
                    FROM {table}
                    WHERE event_id = :eid AND form_timestamp IS NOT NULL
                    """
                ),
                {"eid": event_id},
            ).fetchone()
            dmin = mm[0] if mm and mm[0] is not None else None
            dmax = mm[1] if mm and mm[1] is not None else None
            if isinstance(dmin, datetime):
                dmin = dmin.date()
            if isinstance(dmax, datetime):
                dmax = dmax.date()
            if dmin is not None:
                out["chart_date_min"] = dmin.isoformat()
            if dmax is not None:
                out["chart_date_max"] = dmax.isoformat()

            mdf = date_from
            mdt = date_to
            if dmin is not None and dmax is not None:
                if mdf is None and mdt is not None:
                    mdf = dmin
                if mdt is None and mdf is not None:
                    mdt = dmax
            df, dt = _normalize_mdc_stats_date_range(mdf, mdt, dmin, dmax)
            use_ts = df is not None and dt is not None
            ts_tail = _MDC_STATS_TS_TAIL if use_ts else ""
            qp: dict = {"eid": event_id}
            if use_ts:
                qp["mdc_d0"] = df
                qp["mdc_d1"] = dt
                out["mdc_date_from"] = df.isoformat()
                out["mdc_date_to"] = dt.isoformat()
                out["mdc_filter_by_registration_date"] = True

            total = conn.execute(
                text(f"SELECT COUNT(*) FROM {table} WHERE event_id = :eid{ts_tail}"),
                qp,
            ).scalar()
            out["total_registrations"] = int(total or 0)

            if not is_virtual:
                with_city = conn.execute(
                    text(
                        f"""
                        SELECT COUNT(*) FROM {table}
                        WHERE event_id = :eid{ts_tail}
                          AND attendance_city IS NOT NULL
                          AND btrim(attendance_city) <> ''
                        """
                    ),
                    qp,
                ).scalar()
                out["with_attendance_city"] = int(with_city or 0)
            else:
                out["with_attendance_city"] = 0

            countries = conn.execute(
                text(
                    f"""
                    SELECT COUNT(DISTINCT btrim(country)) FROM {table}
                    WHERE event_id = :eid{ts_tail} AND country IS NOT NULL AND btrim(country) <> ''
                    """
                ),
                qp,
            ).scalar()
            out["distinct_countries"] = int(countries or 0)

            states = conn.execute(
                text(
                    f"""
                    SELECT COUNT(DISTINCT btrim(state)) FROM {table}
                    WHERE event_id = :eid{ts_tail} AND state IS NOT NULL AND btrim(state) <> ''
                    """
                ),
                qp,
            ).scalar()
            out["distinct_states"] = int(states or 0)

            if not is_virtual:
                top_cities = conn.execute(
                    text(
                        f"""
                        SELECT btrim(attendance_city) AS city, COUNT(*)::BIGINT AS cnt
                        FROM {table}
                        WHERE event_id = :eid{ts_tail}
                          AND attendance_city IS NOT NULL
                          AND btrim(attendance_city) <> ''
                        GROUP BY 1
                        ORDER BY cnt DESC, city ASC
                        """
                    ),
                    qp,
                ).mappings().all()
                out["attendance_cities"] = [{"city": r["city"], "count": int(r["cnt"])} for r in top_cities]

            top_utm = conn.execute(
                text(
                    f"""
                    SELECT COALESCE(NULLIF(btrim(utm_source), ''), '(none)') AS src, COUNT(*)::BIGINT AS cnt
                    FROM {table}
                    WHERE event_id = :eid{ts_tail}
                    GROUP BY 1
                    ORDER BY cnt DESC, src ASC
                    LIMIT 8
                    """
                ),
                qp,
            ).mappings().all()
            out["utm_sources"] = [{"source": r["src"], "count": int(r["cnt"])} for r in top_utm]

            lu = conn.execute(
                text(
                    f"""
                    SELECT MAX(updated_at) AS lu FROM {table} WHERE event_id = :eid{ts_tail}
                    """
                ),
                qp,
            ).scalar()
            if lu is not None:
                out["last_updated"] = _format_dt_display(lu) or None

            if is_virtual:
                top_city_sql = f"""
                    WITH city_counts AS (
                      SELECT NULLIF(btrim(city), '') AS city_label,
                             COUNT(*)::BIGINT AS cnt
                      FROM {table}
                      WHERE event_id = :eid{ts_tail}
                      GROUP BY 1
                    )
                    SELECT city_label, cnt FROM city_counts
                    WHERE city_label IS NOT NULL AND btrim(city_label) <> ''
                    ORDER BY cnt DESC, city_label ASC
                    LIMIT 1
                    """
            else:
                top_city_sql = f"""
                    WITH city_counts AS (
                      SELECT COALESCE(NULLIF(btrim(attendance_city), ''), NULLIF(btrim(city), '')) AS city_label,
                             COUNT(*)::BIGINT AS cnt
                      FROM {table}
                      WHERE event_id = :eid{ts_tail}
                      GROUP BY 1
                    )
                    SELECT city_label, cnt FROM city_counts
                    WHERE city_label IS NOT NULL AND btrim(city_label) <> ''
                    ORDER BY cnt DESC, city_label ASC
                    LIMIT 1
                    """
            top_city_row = conn.execute(text(top_city_sql), qp).fetchone()
            if top_city_row and int(top_city_row[1] or 0) > 0:
                out["pill_top_city"] = str(top_city_row[0])
                out["pill_top_city_count"] = int(top_city_row[1])

            top_state_row = conn.execute(
                text(
                    f"""
                    SELECT INITCAP(lower(btrim(state))) AS st, COUNT(*)::BIGINT AS cnt
                    FROM {table}
                    WHERE event_id = :eid{ts_tail} AND state IS NOT NULL AND btrim(state) <> ''
                    GROUP BY 1
                    ORDER BY cnt DESC, st ASC
                    LIMIT 1
                    """
                ),
                qp,
            ).fetchone()
            if top_state_row and int(top_state_row[1] or 0) > 0:
                out["pill_top_state"] = str(top_state_row[0])
                out["pill_top_state_count"] = int(top_state_row[1])

            age_row = conn.execute(
                text(
                    f"""
                    SELECT
                      AVG(EXTRACT(YEAR FROM AGE(CURRENT_DATE, dob)))::double precision AS avg_y,
                      COUNT(*) FILTER (WHERE dob IS NOT NULL)::BIGINT AS n_dob
                    FROM {table}
                    WHERE event_id = :eid{ts_tail}
                    """
                ),
                qp,
            ).fetchone()
            if age_row:
                avg_y, n_dob = age_row[0], int(age_row[1] or 0)
                out["with_dob_count"] = n_dob
                if avg_y is not None and n_dob > 0:
                    out["average_age"] = round(float(avg_y), 1)

            if use_ts:
                seven_end = dt
                seven_start = max(df, dt - timedelta(days=6))
                r7_qp = dict(qp)
                r7_qp["mdc_s7"] = seven_start
                r7_qp["mdc_e7"] = seven_end
                r7 = conn.execute(
                    text(
                        f"""
                        SELECT COUNT(*)::BIGINT FROM {table}
                        WHERE event_id = :eid
                          AND form_timestamp IS NOT NULL
                          AND (form_timestamp AT TIME ZONE 'Asia/Kolkata')::date
                              BETWEEN :mdc_s7 AND :mdc_e7
                        """
                    ),
                    r7_qp,
                ).scalar()
            else:
                r7 = conn.execute(
                    text(
                        f"""
                        SELECT COUNT(*)::BIGINT FROM {table}
                        WHERE event_id = :eid
                          AND form_timestamp IS NOT NULL
                          AND form_timestamp >= now() - interval '7 days'
                        """
                    ),
                    qp,
                ).scalar()
            out["registrations_last_7_days"] = int(r7 or 0)

            trows_where = (
                f"WHERE event_id = :eid{ts_tail}"
                if use_ts
                else "WHERE event_id = :eid AND form_timestamp IS NOT NULL"
            )
            trows = conn.execute(
                text(
                    f"""
                    SELECT (form_timestamp AT TIME ZONE 'Asia/Kolkata')::date AS d, COUNT(*)::BIGINT AS cnt
                    FROM {table}
                    {trows_where}
                    GROUP BY 1
                    """
                ),
                qp,
            ).mappings().all()
            by_day: dict = {}
            for tr in trows:
                dkey = tr["d"]
                if dkey is not None:
                    by_day[dkey] = int(tr["cnt"] or 0)
            today_ist = conn.execute(
                text("SELECT (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Kolkata')::date AS d")
            ).scalar()
            if use_ts and df is not None and dt is not None:
                labels = []
                counts = []
                dcur = df
                while dcur <= dt:
                    labels.append(dcur.strftime("%d-%m-%Y"))
                    counts.append(by_day.get(dcur, 0))
                    dcur += timedelta(days=1)
                out["timeline_labels"] = labels
                out["timeline_counts"] = counts
            elif today_ist is not None:
                start_d = today_ist - timedelta(days=119)
                labels = []
                counts = []
                for i in range(120):
                    dcur = start_d + timedelta(days=i)
                    labels.append(dcur.strftime("%d-%m-%Y"))
                    counts.append(by_day.get(dcur, 0))
                out["timeline_labels"] = labels
                out["timeline_counts"] = counts

            # Hour buckets: TIMESTAMPTZ -> local IST wall clock, then EXTRACT(HOUR).
            # (Same semantics as timestamps that already include +05:30 in the export.)
            hrows_where = (
                f"WHERE event_id = :eid{ts_tail}"
                if use_ts
                else "WHERE event_id = :eid AND form_timestamp IS NOT NULL"
            )
            hrows = conn.execute(
                text(
                    f"""
                    SELECT EXTRACT(HOUR FROM (form_timestamp AT TIME ZONE 'Asia/Kolkata'))::int AS hr,
                           COUNT(*)::BIGINT AS cnt
                    FROM {table}
                    {hrows_where}
                    GROUP BY 1
                    """
                ),
                qp,
            ).mappings().all()
            hourly = [0] * 24
            for hr in hrows:
                h = int(hr["hr"])
                if 0 <= h <= 23:
                    hourly[h] = int(hr["cnt"] or 0)
            out["hourly_counts"] = hourly

            srows = conn.execute(
                text(
                    f"""
                    SELECT INITCAP(lower(btrim(state))) AS st, COUNT(*)::BIGINT AS cnt
                    FROM {table}
                    WHERE event_id = :eid{ts_tail} AND state IS NOT NULL AND btrim(state) <> ''
                    GROUP BY 1
                    ORDER BY cnt DESC
                    LIMIT 36
                    """
                ),
                qp,
            ).mappings().all()
            out["state_distribution"] = [{"name": str(r["st"]), "value": int(r["cnt"])} for r in srows]

            if is_virtual:
                city_pivot_sql = f"""
                    SELECT COALESCE(NULLIF(btrim(city), ''), '(Unknown)') AS city_label,
                           COUNT(*)::BIGINT AS cnt
                    FROM {table}
                    WHERE event_id = :eid{ts_tail}
                    GROUP BY 1
                    ORDER BY cnt DESC
                    LIMIT 40
                    """
            else:
                city_pivot_sql = f"""
                    SELECT COALESCE(NULLIF(btrim(attendance_city), ''), NULLIF(btrim(city), ''), '(Unknown)') AS city_label,
                           COUNT(*)::BIGINT AS cnt
                    FROM {table}
                    WHERE event_id = :eid{ts_tail}
                    GROUP BY 1
                    ORDER BY cnt DESC
                    LIMIT 40
                    """
            crows = conn.execute(text(city_pivot_sql), qp).mappings().all()
            out["city_pivot"] = [{"city": str(r["city_label"]), "count": int(r["cnt"])} for r in crows]

            grows = conn.execute(
                text(
                    f"""
                    SELECT COALESCE(NULLIF(btrim(gender), ''), '(unspecified)') AS g, COUNT(*)::BIGINT AS cnt
                    FROM {table}
                    WHERE event_id = :eid{ts_tail}
                    GROUP BY 1
                    ORDER BY cnt DESC
                    LIMIT 8
                    """
                ),
                qp,
            ).mappings().all()
            out["gender_breakdown"] = [{"gender": str(r["g"]), "count": int(r["cnt"])} for r in grows]

            ocrows = conn.execute(
                text(
                    f"""
                    SELECT COALESCE(NULLIF(btrim(occupation), ''), '(unspecified)') AS occ, COUNT(*)::BIGINT AS cnt
                    FROM {table}
                    WHERE event_id = :eid{ts_tail}
                    GROUP BY 1
                    ORDER BY cnt DESC
                    LIMIT 8
                    """
                ),
                qp,
            ).mappings().all()
            out["top_occupations"] = [{"occupation": str(r["occ"]), "count": int(r["cnt"])} for r in ocrows]
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)
    return out


def _serialize_mdc_row_json(row: dict) -> dict:
    out: dict = {}
    for k, v in row.items():
        if k == "email_normalized":
            continue
        if v is None:
            out[k] = None
        elif isinstance(v, (datetime, date)):
            out[k] = _format_dt_display(v)
        else:
            out[k] = v
    return out


def _virtual_challenge_filter_sql(challenge_id: int | None, *, mode: str) -> tuple[str, dict]:
    """Eligibility predicate for virtual MDC rows against a specific challenge.

    A registrant is eligible iff their `form_timestamp` is on/before the
    challenge's `closes_at` (registering before opens_at OR between opens_at
    and closes_at both qualify; only "after closes_at" is excluded).

    Returns (sql_fragment, extra_params). The fragment is empty when the
    filter does not apply (non-virtual mode or no challenge_id provided).
    """
    if mode != "virtual" or not challenge_id:
        return "", {}
    try:
        cid = int(challenge_id)
    except (TypeError, ValueError):
        return "", {}
    return (
        " AND form_timestamp IS NOT NULL"
        " AND form_timestamp <= (SELECT closes_at FROM challenges WHERE id = :cid)",
        {"cid": cid},
    )


def _mdc_parse_filter_datetime_start(raw: str | None) -> datetime | None:
    """Parse start of range for form_timestamp filter (UTC)."""
    s = (raw or "").strip()[:40]
    if not s:
        return None
    s2 = s.replace("T", " ")
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(s2, fmt)
            if fmt == "%Y-%m-%d":
                dt = dt.replace(hour=0, minute=0, second=0, microsecond=0)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    s3 = (raw or "").strip()[:40]
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(s3, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _mdc_parse_filter_datetime_end(raw: str | None) -> datetime | None:
    """Parse end of range for form_timestamp filter (UTC, inclusive)."""
    s = (raw or "").strip()[:40]
    if not s:
        return None
    s2 = s.replace("T", " ")
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(s2, fmt)
            if fmt == "%Y-%m-%d":
                dt = dt.replace(hour=23, minute=59, second=59, microsecond=999999)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    s3 = (raw or "").strip()[:40]
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(s3, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _mdc_parse_filter_date_only(raw: str | None) -> date | None:
    s = (raw or "").strip()[:32]
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def _parse_mdc_filter_years_experience(raw: str | None) -> int | None:
    """Parse 0–99 years filter from query string; invalid or empty → None."""
    s = (raw or "").strip()[:8]
    if not s or not s.isdigit():
        return None
    n = int(s)
    if n < 0 or n > 99:
        return None
    return n


def _parse_mdc_users_advanced_from_request(args) -> dict[str, object] | None:
    """Parse advanced roster filters from query/form args. Returns None if nothing set."""
    text: dict[str, str] = {}
    for col in MDC_USERS_ADVANCED_TEXT_COLUMNS:
        raw = (args.get(f"af_{col}") or "").strip()[:200]
        if raw:
            text[col] = raw
    def _norm_dt_local(val: str | None) -> str:
        s = (val or "").strip()[:40]
        if not s:
            return ""
        if " " in s and "T" not in s.upper():
            return s.replace(" ", "T", 1)
        return s

    raw_filters = {
        "form_ts_from": _norm_dt_local(args.get("form_ts_from")),
        "form_ts_to": _norm_dt_local(args.get("form_ts_to")),
        "dob_from": (args.get("dob_from") or "").strip()[:32],
        "dob_to": (args.get("dob_to") or "").strip()[:32],
        "designation_years_min": (args.get("designation_years_min") or "").strip()[:8],
        "designation_years_max": (args.get("designation_years_max") or "").strip()[:8],
    }
    form_ts_from = _mdc_parse_filter_datetime_start(raw_filters["form_ts_from"])
    form_ts_to = _mdc_parse_filter_datetime_end(raw_filters["form_ts_to"])
    dob_from = _mdc_parse_filter_date_only(raw_filters["dob_from"])
    dob_to = _mdc_parse_filter_date_only(raw_filters["dob_to"])
    designation_years_min = _parse_mdc_filter_years_experience(raw_filters["designation_years_min"])
    designation_years_max = _parse_mdc_filter_years_experience(raw_filters["designation_years_max"])
    if (
        designation_years_min is not None
        and designation_years_max is not None
        and designation_years_min > designation_years_max
    ):
        designation_years_min, designation_years_max = designation_years_max, designation_years_min
    if (
        not text
        and form_ts_from is None
        and form_ts_to is None
        and dob_from is None
        and dob_to is None
        and designation_years_min is None
        and designation_years_max is None
    ):
        return None
    return {
        "text": text,
        "form_ts_from": form_ts_from,
        "form_ts_to": form_ts_to,
        "dob_from": dob_from,
        "dob_to": dob_to,
        "designation_years_min": designation_years_min,
        "designation_years_max": designation_years_max,
        "raw": raw_filters,
    }


def _mdc_users_advanced_apply_sql(
    advanced: dict[str, object] | None,
    conditions: list[str],
    params: dict,
) -> None:
    if not advanced:
        return
    for col, val in (advanced.get("text") or {}).items():
        if col not in MDC_USERS_ADVANCED_TEXT_COLUMNS:
            continue
        pname = f"adv_{col}"
        conditions.append(
            f"lower(btrim(COALESCE({col}, ''))) = lower(btrim(:{pname}))"
        )
        params[pname] = (val or "").strip()[:200]
    fts_f = advanced.get("form_ts_from")
    fts_t = advanced.get("form_ts_to")
    if fts_f is not None:
        conditions.append("form_timestamp >= :adv_fts_from")
        params["adv_fts_from"] = fts_f
    if fts_t is not None:
        conditions.append("form_timestamp <= :adv_fts_to")
        params["adv_fts_to"] = fts_t
    dob_f = advanced.get("dob_from")
    dob_t = advanced.get("dob_to")
    if dob_f is not None:
        conditions.append("dob >= :adv_dob_from")
        params["adv_dob_from"] = dob_f
    if dob_t is not None:
        conditions.append("dob <= :adv_dob_to")
        params["adv_dob_to"] = dob_t
    dym = advanced.get("designation_years_min")
    dyx = advanced.get("designation_years_max")
    if dym is not None:
        conditions.append("designation_years_experience >= :adv_dyoe_min")
        params["adv_dyoe_min"] = dym
    if dyx is not None:
        conditions.append("designation_years_experience <= :adv_dyoe_max")
        params["adv_dyoe_max"] = dyx


def _mdc_users_preserve_query_dict(
    search_s: str,
    attendance_city: str | None,
    challenge_id: int | None,
    advanced: dict[str, object] | None,
) -> dict[str, str]:
    """Flat query-string parts (no page) for pagination, export, and per-page form."""
    out: dict[str, str] = {}
    if search_s:
        out["q"] = search_s
    if attendance_city:
        out["attendance_city"] = attendance_city
    if challenge_id:
        out["challengeId"] = str(int(challenge_id))
    if not advanced:
        return out
    for col, val in (advanced.get("text") or {}).items():
        out[f"af_{col}"] = val
    raw = advanced.get("raw") or {}
    if raw.get("form_ts_from"):
        out["form_ts_from"] = str(raw["form_ts_from"])
    if raw.get("form_ts_to"):
        out["form_ts_to"] = str(raw["form_ts_to"])
    if raw.get("dob_from"):
        out["dob_from"] = str(raw["dob_from"])
    if raw.get("dob_to"):
        out["dob_to"] = str(raw["dob_to"])
    if raw.get("designation_years_min"):
        out["designation_years_min"] = str(raw["designation_years_min"])
    if raw.get("designation_years_max"):
        out["designation_years_max"] = str(raw["designation_years_max"])
    return out


def _mdc_users_build_filter(
    event_id: int,
    search: str,
    attendance_city: str | None,
    *,
    mode: str = "in_person",
    challenge_id: int | None = None,
    advanced: dict[str, object] | None = None,
) -> tuple[str, dict]:
    """Build WHERE clause; `search` must already be trimmed (empty means no text filter)."""
    conditions = ["event_id = :eid"]
    params: dict = {"eid": event_id}
    if attendance_city:
        conditions.append("btrim(COALESCE(attendance_city, '')) = :acity")
        params["acity"] = attendance_city
    if search:
        conditions.append(
            "("
            "email ILIKE :q OR COALESCE(full_name, '') ILIKE :q OR "
            "COALESCE(profile_name, '') ILIKE :q OR COALESCE(mobile, '') ILIKE :q"
            ")"
        )
        params["q"] = f"%{search}%"
    _mdc_users_advanced_apply_sql(advanced, conditions, params)
    extra_where, extra_params = _virtual_challenge_filter_sql(challenge_id, mode=mode)
    if extra_where:
        conditions.append(extra_where.lstrip().removeprefix("AND ").strip())
        params.update(extra_params)
    return " AND ".join(conditions), params


def _get_virtual_challenge(challenge_id: int) -> dict | None:
    """Return a challenge row scoped to a virtual event, or None if missing/non-virtual."""
    try:
        cid = int(challenge_id)
    except (TypeError, ValueError):
        return None
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT c.id, c.event_id, c.title, c.description, c.slug, c.import_sheet_suffix,
                           c.opens_at, c.closes_at, c.status, c.created_at, c.updated_at,
                           e.kind AS event_kind, e.name AS event_name
                    FROM challenges c
                    JOIN events e ON e.id = c.event_id
                    WHERE c.id = :cid
                    """
                ),
                {"cid": cid},
            ).mappings().fetchone()
        if not row:
            return None
        if str(row.get("event_kind")) != "virtual":
            return None
        return dict(row)
    except Exception:  # noqa: BLE001
        return None


def _load_virtual_challenges(event_id: int) -> list[dict]:
    """List of challenges for a virtual event with eligible/total counts."""
    try:
        with engine.connect() as conn:
            ev = conn.execute(
                text("SELECT id, kind FROM events WHERE id = :eid"),
                {"eid": event_id},
            ).fetchone()
            if not ev or str(ev[1]) != "virtual":
                return []
            total = int(
                conn.execute(
                    text(
                        f"SELECT COUNT(*) FROM {TABLE_VIRTUAL_MDC} WHERE event_id = :eid"
                    ),
                    {"eid": event_id},
                ).scalar()
                or 0
            )
            rows = conn.execute(
                text(
                    f"""
                    SELECT c.id, c.title, c.description, c.slug, c.import_sheet_suffix,
                           c.opens_at, c.closes_at, c.status,
                           c.created_at, c.updated_at,
                           COUNT(m.id) FILTER (
                             WHERE m.form_timestamp IS NOT NULL
                               AND m.form_timestamp <= c.closes_at
                           )::BIGINT AS eligible_count
                    FROM challenges c
                    LEFT JOIN {TABLE_VIRTUAL_MDC} m
                      ON m.event_id = c.event_id
                    WHERE c.event_id = :eid
                    GROUP BY c.id
                    ORDER BY
                      CASE c.status WHEN 'live' THEN 0 WHEN 'draft' THEN 1 ELSE 2 END,
                      c.opens_at NULLS LAST,
                      c.id
                    """
                ),
                {"eid": event_id},
            ).mappings().all()
        out: list[dict] = []
        for r in rows:
            d = dict(r)
            d["eligible_count"] = int(d.get("eligible_count") or 0)
            d["total_registrations"] = total
            out.append(d)
        return out
    except Exception:  # noqa: BLE001
        return []


def _load_virtual_challenges_brief(event_id: int) -> list[dict]:
    """Lightweight challenge list for picker dropdowns (id, title, dates, status)."""
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT id, title, import_sheet_suffix, opens_at, closes_at, status
                    FROM challenges
                    WHERE event_id = :eid
                    ORDER BY
                      CASE status WHEN 'live' THEN 0 WHEN 'draft' THEN 1 ELSE 2 END,
                      opens_at NULLS LAST,
                      id
                    """
                ),
                {"eid": event_id},
            ).mappings().all()
        return [dict(r) for r in rows]
    except Exception:  # noqa: BLE001
        return []


def _resolve_virtual_arena_challenge_id(
    event_id: int,
    *,
    requested: int | None,
    challenges: list[dict] | None = None,
) -> tuple[int | None, list[dict]]:
    """Challenge id for Virtual arena charts / submission leaderboard for this event.

    If ``requested`` is in the event's challenge list, use it. If the URL omits a
    challenge, prefer ``DEFAULT_CHALLENGE_ID`` when that row still exists; otherwise
    the first challenge for the event (by the same ordering as
    :func:`_load_virtual_challenges_brief`).     Stale ids (e.g. deleted challenge #1)
    are not used.
    """
    chs = challenges if challenges is not None else _load_virtual_challenges_brief(event_id)
    ids = [int(c["id"]) for c in chs]
    if not ids:
        return None, chs
    id_set = set(ids)
    if requested is not None and requested in id_set:
        return requested, chs
    if requested is None and DEFAULT_CHALLENGE_ID in id_set:
        return DEFAULT_CHALLENGE_ID, chs
    return ids[0], chs


def _effective_arena_challenge_seed(
    *,
    arena_challenge_id: int | None,
    eligibility_challenge_id: int | None,
    valid_ids: set[int],
) -> int | None:
    """Pick URL intent for which challenge powers the Virtual arena (LB + distribution).

    Explicit ``arenaChallengeId`` wins when valid. Otherwise a valid ``challengeId``
    (eligibility filter) still drives the arena for backward compatibility. If neither
    applies, returns None so :func:`_resolve_virtual_arena_challenge_id` applies defaults.
    """
    if arena_challenge_id is not None and arena_challenge_id in valid_ids:
        return arena_challenge_id
    if eligibility_challenge_id is not None and eligibility_challenge_id in valid_ids:
        return eligibility_challenge_id
    return None


def _load_virtual_eligibility_summary(event_id: int, challenge_id: int) -> dict:
    """Pill-card metrics for a specific virtual challenge: eligible vs total."""
    out: dict = {
        "challenge_id": int(challenge_id) if challenge_id else None,
        "title": None,
        "opens_at": None,
        "closes_at": None,
        "status": None,
        "total": 0,
        "eligible": 0,
        "eligible_last_7_days": 0,
        "error": None,
    }
    try:
        cid = int(challenge_id)
    except (TypeError, ValueError):
        out["error"] = "invalid challenge_id"
        return out
    try:
        with engine.connect() as conn:
            ch = conn.execute(
                text(
                    """
                    SELECT id, event_id, title, opens_at, closes_at, status
                    FROM challenges
                    WHERE id = :cid AND event_id = :eid
                    """
                ),
                {"cid": cid, "eid": event_id},
            ).mappings().fetchone()
            if not ch:
                out["error"] = "challenge not found for event"
                return out
            out["title"] = ch["title"]
            out["opens_at"] = _format_dt_display(ch["opens_at"]) or None
            out["closes_at"] = _format_dt_display(ch["closes_at"]) or None
            out["status"] = ch["status"]
            out["total"] = int(
                conn.execute(
                    text(
                        f"SELECT COUNT(*) FROM {TABLE_VIRTUAL_MDC} WHERE event_id = :eid"
                    ),
                    {"eid": event_id},
                ).scalar()
                or 0
            )
            out["eligible"] = int(
                conn.execute(
                    text(
                        f"""
                        SELECT COUNT(*) FROM {TABLE_VIRTUAL_MDC}
                        WHERE event_id = :eid
                          AND form_timestamp IS NOT NULL
                          AND form_timestamp <= (SELECT closes_at FROM challenges WHERE id = :cid)
                        """
                    ),
                    {"eid": event_id, "cid": cid},
                ).scalar()
                or 0
            )
            out["eligible_last_7_days"] = int(
                conn.execute(
                    text(
                        f"""
                        SELECT COUNT(*) FROM {TABLE_VIRTUAL_MDC}
                        WHERE event_id = :eid
                          AND form_timestamp IS NOT NULL
                          AND form_timestamp <= (SELECT closes_at FROM challenges WHERE id = :cid)
                          AND form_timestamp >= now() - interval '7 days'
                        """
                    ),
                    {"eid": event_id, "cid": cid},
                ).scalar()
                or 0
            )
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)
    return out


def _is_participant_eligible_for_challenge(conn, challenge_id: int, participant_id: int) -> bool:
    """True iff the participant's email matches a virtual MDC row registered <= challenge.closes_at.

    Returns False (deny) when either side is missing.
    """
    try:
        cid = int(challenge_id)
        pid = int(participant_id)
    except (TypeError, ValueError):
        return False
    row = conn.execute(
        text(
            f"""
            SELECT 1
            FROM challenges c
            JOIN events e ON e.id = c.event_id AND e.kind = 'virtual'
            JOIN participants p ON p.id = :pid
            JOIN {TABLE_VIRTUAL_MDC} m
              ON m.event_id = c.event_id
             AND m.email_normalized = lower(trim(COALESCE(p.email, '')))
            WHERE c.id = :cid
              AND COALESCE(NULLIF(trim(p.email), ''), NULL) IS NOT NULL
              AND m.form_timestamp IS NOT NULL
              AND m.form_timestamp <= c.closes_at
            LIMIT 1
            """
        ),
        {"cid": cid, "pid": pid},
    ).fetchone()
    return bool(row)


def _load_mdc_attendance_city_options(conn, event_id: int, *, mode: str = "in_person") -> list[str]:
    if mode == "virtual":
        return []
    table = _mdc_table_for_mode(mode)
    rows = conn.execute(
        text(
            f"""
            SELECT DISTINCT btrim(attendance_city) AS city
            FROM {table}
            WHERE event_id = :eid AND attendance_city IS NOT NULL AND btrim(attendance_city) <> ''
            ORDER BY 1 ASC
            """
        ),
        {"eid": event_id},
    ).scalars().all()
    return [str(x) for x in rows if x]


def _load_mdc_advanced_select_options(
    conn,
    event_id: int,
    *,
    mode: str,
    columns: tuple[str, ...] = MDC_USERS_ADVANCED_TEXT_COLUMNS,
    per_column_limit: int = MDC_USERS_ADVANCED_SELECT_LIMIT,
) -> dict[str, list[str]]:
    """Distinct non-empty values per advanced-filter column (dropdown options, cap per column)."""
    table = _mdc_table_for_mode(mode)
    out: dict[str, list[str]] = {c: [] for c in MDC_USERS_ADVANCED_TEXT_COLUMNS}
    lim = int(per_column_limit)
    for col in columns:
        if col not in MDC_USERS_ADVANCED_TEXT_COLUMNS:
            continue
        try:
            rows = conn.execute(
                text(
                    f"""
                    SELECT DISTINCT btrim({col}) AS v
                    FROM {table}
                    WHERE event_id = :eid AND {col} IS NOT NULL AND btrim({col}) <> ''
                    ORDER BY 1 ASC
                    LIMIT :lim
                    """
                ),
                {"eid": event_id, "lim": lim},
            ).scalars().all()
            out[col] = [str(x) for x in rows if x]
        except Exception:  # noqa: BLE001
            out[col] = []
    return out


def _mdc_users_active_chips(
    advanced: dict[str, object] | None,
    *,
    preserve: dict[str, str],
    per_page: int,
) -> list[dict]:
    """One chip per active advanced filter; ``remove_qs`` is the page query without that key."""
    if not advanced:
        return []
    label_map = dict(MDC_USERS_ADVANCED_FORM_FIELDS)
    chips: list[dict] = []
    base = dict(preserve)
    base["per_page"] = str(per_page)
    for col, val in (advanced.get("text") or {}).items():
        params = dict(base)
        params.pop(f"af_{col}", None)
        chips.append(
            {
                "key": f"af_{col}",
                "label": label_map.get(col, col),
                "value": val,
                "remove_qs": urlencode(params),
            }
        )
    raw = advanced.get("raw") or {}
    for k, lab in MDC_USERS_ADVANCED_CHIP_LABELS.items():
        v = raw.get(k)
        if not v:
            continue
        params = dict(base)
        params.pop(k, None)
        chips.append(
            {
                "key": k,
                "label": lab,
                "value": str(v),
                "remove_qs": urlencode(params),
            }
        )
    return chips


def _mdc_users_reset_advanced_qs(
    *,
    search_s: str,
    attendance_city: str | None,
    challenge_id: int | None,
    per_page: int,
) -> str:
    """Querystring that drops every advanced-filter param while keeping search, dropdowns, per-page."""
    params: dict[str, str] = {"per_page": str(per_page)}
    if search_s:
        params["q"] = search_s
    if attendance_city:
        params["attendance_city"] = attendance_city
    if challenge_id:
        params["challengeId"] = str(int(challenge_id))
    return urlencode(params)


def _load_mdc_users_page(
    event_id: int,
    page: int,
    per_page: int,
    search: str,
    attendance_city: str | None = None,
    *,
    mode: str = "in_person",
    challenge_id: int | None = None,
    advanced: dict[str, object] | None = None,
) -> dict:
    """Paginated Main Data Center registrations for the Vision roster table."""
    table = _mdc_table_for_mode(mode)
    per_page = max(10, min(int(per_page or 25), 100))
    page = max(1, int(page or 1))
    offset = (page - 1) * per_page
    search_s = (search or "").strip()[:200]
    ac = None if mode == "virtual" else ((attendance_city or "").strip()[:200] or None)
    cid = int(challenge_id) if (mode == "virtual" and challenge_id) else None
    adv_active = bool(advanced)
    preserve = _mdc_users_preserve_query_dict(search_s, ac, cid, advanced)
    export_query = urlencode(preserve)
    pagination_body = {"per_page": str(per_page), **preserve}
    preserve_query_str = urlencode(pagination_body)
    advanced_text = dict((advanced or {}).get("text") or {})
    advanced_raw = dict((advanced or {}).get("raw") or {})
    advanced_count = len(advanced_text) + sum(1 for v in advanced_raw.values() if v)
    advanced_chips = _mdc_users_active_chips(advanced, preserve=preserve, per_page=per_page)
    reset_advanced_qs = _mdc_users_reset_advanced_qs(
        search_s=search_s, attendance_city=ac, challenge_id=cid, per_page=per_page
    )
    out: dict = {
        "error": None,
        "rows": [],
        "total": 0,
        "page": page,
        "per_page": per_page,
        "search": search_s,
        "attendance_city": ac or "",
        "attendance_city_options": [],
        "total_pages": 1,
        "export_query": export_query,
        "preserve_query_str": preserve_query_str,
        "challenge_id": cid,
        "advanced": advanced,
        "advanced_active": adv_active,
        "advanced_count": advanced_count,
        "advanced_form_fields": MDC_USERS_ADVANCED_FORM_FIELDS,
        "advanced_field_groups": MDC_USERS_ADVANCED_FIELD_GROUPS,
        "advanced_select_options": {
            c: [] for c in MDC_USERS_ADVANCED_TEXT_COLUMNS
        },
        "advanced_select_limit": MDC_USERS_ADVANCED_SELECT_LIMIT,
        "advanced_text": advanced_text,
        "advanced_raw": advanced_raw,
        "advanced_chips": advanced_chips,
        "reset_advanced_qs": reset_advanced_qs,
        "preserve_items": list(preserve.items()),
    }
    try:
        with engine.connect() as conn:
            out["attendance_city_options"] = _load_mdc_attendance_city_options(conn, event_id, mode=mode)
            out["advanced_select_options"] = _load_mdc_advanced_select_options(
                conn, event_id, mode=mode
            )
            where_sql, params = _mdc_users_build_filter(
                event_id, search_s, ac, mode=mode, challenge_id=cid, advanced=advanced
            )
            total = int(
                conn.execute(
                    text(f"SELECT COUNT(*) FROM {table} WHERE {where_sql}"),
                    params,
                ).scalar()
                or 0
            )
            params_page = dict(params)
            params_page["lim"] = per_page
            params_page["off"] = offset
            rows = conn.execute(
                text(
                    f"""
                    SELECT id, full_name, email, city, state, country, attendance_city, occupation,
                           mobile, profile_name, form_timestamp, designation, designation_years_experience
                    FROM {table}
                    WHERE {where_sql}
                    ORDER BY form_timestamp DESC NULLS LAST, id DESC
                    LIMIT :lim OFFSET :off
                    """
                ),
                params_page,
            ).mappings().all()
        out["total"] = total
        out["total_pages"] = max(1, (total + per_page - 1) // per_page) if total else 1
        out["rows"] = [dict(r) for r in rows]
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)
    return out


def _fetch_mdc_users_export_rows(
    event_id: int,
    search: str,
    attendance_city: str | None,
    *,
    mode: str = "in_person",
    challenge_id: int | None = None,
    advanced: dict[str, object] | None = None,
) -> tuple[list[dict], str | None]:
    table = _mdc_table_for_mode(mode)
    search_s = (search or "").strip()[:200]
    ac = None if mode == "virtual" else ((attendance_city or "").strip()[:200] or None)
    cid = int(challenge_id) if (mode == "virtual" and challenge_id) else None
    where_sql, params = _mdc_users_build_filter(
        event_id, search_s, ac, mode=mode, challenge_id=cid, advanced=advanced
    )
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    f"""
                    SELECT id, full_name, email, city, state, country, attendance_city, occupation,
                           mobile, profile_name, form_timestamp, designation, designation_years_experience
                    FROM {table}
                    WHERE {where_sql}
                    ORDER BY form_timestamp DESC NULLS LAST, id DESC
                    """
                ),
                params,
            ).mappings().all()
        return [dict(r) for r in rows], None
    except Exception as exc:  # noqa: BLE001
        return [], str(exc)


def _mdc_users_rows_to_csv(rows: list[dict], *, mode: str = "in_person") -> bytes:
    include_attendance = mode != "virtual"
    headers = [
        "id",
        "full_name",
        "email",
        "city",
        "state",
        "country",
        *([] if not include_attendance else ["attendance_city"]),
        "occupation",
        "mobile",
        "profile_name",
        "form_timestamp",
    ]
    buf = StringIO()
    writer = csv.writer(buf)
    writer.writerow(headers)
    for r in rows:
        fts = r.get("form_timestamp")
        fts_s = _format_dt_display(fts) if fts is not None else ""
        row_vals = [
            r.get("id"),
            r.get("full_name") or "",
            r.get("email") or "",
            r.get("city") or "",
            r.get("state") or "",
            r.get("country") or "",
        ]
        if include_attendance:
            row_vals.append(r.get("attendance_city") or "")
        row_vals.extend(
            [
                r.get("occupation") or "",
                r.get("mobile") or "",
                r.get("profile_name") or "",
                fts_s,
            ]
        )
        writer.writerow(row_vals)
    return ("\ufeff" + buf.getvalue()).encode("utf-8")


def _serialize_lb_row(row) -> dict:
    d = dict(row)
    if d.get("score") is not None:
        d["score"] = float(d["score"])
    uh = d.get("updated_hint")
    if uh is not None:
        d["updated_hint"] = uh.isoformat() if hasattr(uh, "isoformat") else str(uh)
    d["rank"] = int(d["rank"])
    return d


def _load_virtual_bundle(challenge_id: int, bins: int = 10) -> tuple[dict, dict, list]:
    leaderboard: dict = {"rows": [], "error": None}
    distribution: dict = {"bins": [], "error": None}
    dist_bins: list = []
    try:
        with engine.connect() as conn:
            ch = conn.execute(
                text("SELECT id, event_id FROM challenges WHERE id = :id"),
                {"id": challenge_id},
            ).fetchone()
            if not ch:
                raise ValueError("challenge not found")
            cid, vid = int(ch[0]), int(ch[1])
            rows = conn.execute(
                text(
                    """
                    SELECT ranked.participant_id, ranked.display_name, ranked.score,
                           ranked.rank, ranked.updated_hint
                    FROM (
                      SELECT base.participant_id, base.display_name, base.score,
                             RANK() OVER (ORDER BY base.score DESC) AS rank,
                             base.updated_hint
                      FROM (
                        SELECT p.id AS participant_id,
                               COALESCE(p.display_name, p.external_user_id, 'Participant ' || p.id) AS display_name,
                               COALESCE(SUM(l.delta), 0) AS score,
                               MAX(l.created_at) AS updated_hint
                        FROM registrations reg
                        JOIN participants p ON p.id = reg.participant_id
                        LEFT JOIN credit_ledger l
                          ON l.participant_id = p.id AND l.challenge_id = :cid
                        WHERE reg.event_id = :eid
                        GROUP BY p.id, p.display_name, p.external_user_id
                      ) base
                    ) ranked
                    ORDER BY ranked.rank, ranked.participant_id
                    LIMIT 50 OFFSET 0
                    """
                ),
                {"cid": cid, "eid": vid},
            ).mappings().all()
            leaderboard = {"rows": [_serialize_lb_row(r) for r in rows]}

            scores = conn.execute(
                text(
                    """
                    SELECT COALESCE(SUM(l.delta), 0) AS score
                    FROM registrations reg
                    JOIN participants p ON p.id = reg.participant_id
                    LEFT JOIN credit_ledger l ON l.participant_id = p.id AND l.challenge_id = :cid
                    WHERE reg.event_id = :eid
                    GROUP BY p.id
                    """
                ),
                {"cid": cid, "eid": vid},
            ).scalars().all()

        vals = [float(s) for s in scores]
        bins = min(max(bins, 3), 50)
        if not vals:
            distribution = {"bins": [], "min": 0, "max": 0, "error": None}
        else:
            vmin, vmax = min(vals), max(vals)
            if vmin == vmax:
                distribution = {"bins": [{"low": vmin, "high": vmax, "count": len(vals)}], "min": vmin, "max": vmax}
            else:
                width = (vmax - vmin) / bins
                bucket_counts = [0 for _ in range(bins)]
                for v in vals:
                    idx = int((v - vmin) / width) if width > 0 else 0
                    if idx >= bins:
                        idx = bins - 1
                    bucket_counts[idx] += 1
                out_bins = []
                for i in range(bins):
                    low = vmin + i * width
                    high = vmin + (i + 1) * width if i < bins - 1 else vmax
                    out_bins.append({"low": low, "high": high, "count": bucket_counts[i]})
                distribution = {"bins": out_bins, "min": vmin, "max": vmax}
        dist_bins = distribution.get("bins") or []
    except Exception as exc:  # noqa: BLE001
        leaderboard = {"rows": [], "error": str(exc)}
        distribution = {"bins": [], "error": str(exc)}
        dist_bins = []
    return leaderboard, distribution, dist_bins


def _validate_virtual_submission_challenge(conn, *, event_id: int, challenge_id: int) -> dict | None:
    """Challenge must belong to event_id and event must be virtual."""
    row = conn.execute(
        text(
            """
            SELECT c.id, c.title, c.event_id
            FROM challenges c
            JOIN events e ON e.id = c.event_id AND e.kind = 'virtual'
            WHERE c.id = :cid AND c.event_id = :eid
            """
        ),
        {"cid": int(challenge_id), "eid": int(event_id)},
    ).mappings().fetchone()
    return dict(row) if row else None


def _submission_leaderboard_payload(
    *,
    event_id: int,
    challenge_id: int,
    limit: int,
    offset: int,
) -> dict:
    """
    Team rows from virtual_challenge_submission_rows for one challenge.
    Order: total_score DESC, export_created_at ASC (earlier submission wins ties), id ASC.
    """
    limit = min(max(int(limit or 50), 1), 500)
    offset = max(int(offset or 0), 0)
    out: dict = {"rows": [], "total": 0, "error": None, "challenge": None}
    try:
        with engine.connect() as conn:
            ch = _validate_virtual_submission_challenge(conn, event_id=event_id, challenge_id=challenge_id)
            if not ch:
                out["error"] = "challenge not found"
                return out
            out["challenge"] = {"id": int(ch["id"]), "title": ch.get("title") or "", "event_id": int(ch["event_id"])}
            total = conn.execute(
                text(
                    """
                    SELECT COUNT(*)::BIGINT
                    FROM virtual_challenge_submission_rows
                    WHERE event_id = :eid AND challenge_id = :cid
                    """
                ),
                {"eid": int(event_id), "cid": int(challenge_id)},
            ).scalar()
            out["total"] = int(total or 0)
            rows = conn.execute(
                text(
                    """
                    WITH ranked AS (
                        SELECT id,
                               team_name,
                               leader_name,
                               leader_email,
                               total_score,
                               export_created_at,
                               ROW_NUMBER() OVER (
                                 ORDER BY total_score DESC NULLS LAST,
                                          export_created_at ASC NULLS LAST,
                                          id ASC
                               ) AS rank
                        FROM virtual_challenge_submission_rows
                        WHERE event_id = :eid AND challenge_id = :cid
                    )
                    SELECT rank, team_name, leader_name, leader_email, total_score,
                           export_created_at AS submitted_at
                    FROM ranked
                    ORDER BY rank
                    LIMIT :lim OFFSET :off
                    """
                ),
                {"eid": int(event_id), "cid": int(challenge_id), "lim": limit, "off": offset},
            ).mappings().all()
            for r in rows:
                d = dict(r)
                if d.get("total_score") is not None:
                    d["total_score"] = float(d["total_score"])
                sa = d.get("submitted_at")
                if sa is not None and hasattr(sa, "isoformat"):
                    d["submitted_at"] = sa.isoformat()
                elif sa is not None:
                    d["submitted_at"] = str(sa)
                d["rank"] = int(d["rank"])
                out["rows"].append(d)
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)
    return out


def _virtual_global_submission_leaderboard(
    *,
    event_id: int,
    limit: int = 50,
    offset: int = 0,
    page: int | None = None,
    per_page: int | None = None,
) -> dict:
    """
    Aggregate ``virtual_challenge_submission_rows`` across all virtual arena challenges
    for one event. One row per (team_name_normalized, leader_email_normalized): each row's
    ``average_score`` is the mean of that team's per-arena scores (missing scores count as
    zero; denominator is the number of arenas they have a submission row in).
    ``submitted_at`` is the earliest export timestamp across that team's rows; display
    name/email come from the row with the highest single-arena score (then earliest
    submission, then id).
    """
    paginate = page is not None and per_page is not None
    if paginate:
        pp = min(max(int(per_page), 1), 100)
        pg = max(int(page), 1)
        eff_lim = pp
        eff_off = (pg - 1) * pp
    else:
        eff_lim = min(max(int(limit or 50), 1), 500)
        eff_off = max(int(offset or 0), 0)
    out: dict = {
        "rows": [],
        "total": 0,
        "error": None,
        "challenge": None,
        "scope": {"virtual_event_id": int(event_id), "global": True},
    }
    _vcsr_global_base = """
                    WITH base AS (
                        SELECT r.id,
                               r.team_name_normalized,
                               r.leader_email_normalized,
                               r.team_name,
                               r.leader_name,
                               r.leader_email,
                               r.total_score,
                               r.export_created_at,
                               r.challenge_id
                        FROM virtual_challenge_submission_rows r
                        INNER JOIN challenges c
                          ON c.id = r.challenge_id AND c.event_id = r.event_id
                        INNER JOIN events e ON e.id = r.event_id AND e.kind = 'virtual'
                        WHERE r.event_id = :eid
                    ),
                    agg AS (
                        SELECT team_name_normalized,
                               leader_email_normalized,
                               (SUM(COALESCE(total_score, 0))::numeric
                                / NULLIF(COUNT(*)::numeric, 0)) AS average_score,
                               MIN(export_created_at) AS submitted_at,
                               COUNT(DISTINCT challenge_id)::INTEGER AS arena_count,
                               (ARRAY_AGG(
                                   team_name ORDER BY total_score DESC NULLS LAST,
                                                        export_created_at ASC NULLS LAST,
                                                        id ASC
                               ))[1] AS team_name,
                               (ARRAY_AGG(
                                   leader_name ORDER BY total_score DESC NULLS LAST,
                                                         export_created_at ASC NULLS LAST,
                                                         id ASC
                               ))[1] AS leader_name,
                               (ARRAY_AGG(
                                   leader_email ORDER BY total_score DESC NULLS LAST,
                                                          export_created_at ASC NULLS LAST,
                                                          id ASC
                               ))[1] AS leader_email
                        FROM base
                        GROUP BY team_name_normalized, leader_email_normalized
                    )
    """
    try:
        with engine.connect() as conn:
            total = int(
                conn.execute(
                    text(_vcsr_global_base + " SELECT COUNT(*)::BIGINT FROM agg "),
                    {"eid": int(event_id)},
                ).scalar()
                or 0
            )
            out["total"] = total
            rows = conn.execute(
                text(
                    _vcsr_global_base
                    + """
                    , ranked AS (
                        SELECT team_name_normalized,
                               leader_email_normalized,
                               team_name,
                               leader_name,
                               leader_email,
                               average_score,
                               submitted_at,
                               arena_count,
                               ROW_NUMBER() OVER (
                                   ORDER BY average_score DESC NULLS LAST,
                                            submitted_at ASC NULLS LAST,
                                            team_name_normalized ASC,
                                            leader_email_normalized ASC
                               )::BIGINT AS rank
                        FROM agg
                    )
                    SELECT r.rank,
                           r.team_name,
                           r.leader_name,
                           r.leader_email,
                           r.average_score,
                           r.submitted_at,
                           r.arena_count
                    FROM ranked r
                    ORDER BY r.rank
                    LIMIT :lim OFFSET :off
                    """
                ),
                {"eid": int(event_id), "lim": eff_lim, "off": eff_off},
            ).mappings().all()
            if paginate:
                out["page"] = int(pg)
                out["per_page"] = int(pp)
                out["total_pages"] = max(1, (total + pp - 1) // pp) if total else 1
            for r in rows:
                d = dict(r)
                if d.get("average_score") is not None:
                    d["average_score"] = float(d["average_score"])
                sa = d.get("submitted_at")
                if sa is not None and hasattr(sa, "isoformat"):
                    d["submitted_at"] = sa.isoformat()
                elif sa is not None:
                    d["submitted_at"] = str(sa)
                d["rank"] = int(d["rank"])
                ac = d.get("arena_count")
                d["arena_count"] = int(ac) if ac is not None else 0
                out["rows"].append(d)
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)
    return out


def _in_person_submission_leaderboard(
    event_id: int,
    attendance_city: str | None,
    limit: int,
    *,
    prompt_war_on: date | None = None,
    session_label: str = "",
    page: int | None = None,
    per_page: int | None = None,
) -> dict:
    """
    Main-challenge team rows from in_person_challenge_submission_rows (warmup excluded).
    Order: total_score DESC, export_created_at ASC, id ASC.

    When ``attendance_city`` is set: filter by Prompt War session. If ``prompt_war_on`` is
    ``None``, use the legacy cohort (sentinel date + empty session label).

    When ``page`` and ``per_page`` are set, returns that page of rows (``per_page`` capped at 100)
    and sets ``total_pages`` on the result. Otherwise returns the top ``limit`` rows (``limit``
    capped at 100).
    """
    paginate = page is not None and per_page is not None
    if paginate:
        pp = min(max(int(per_page), 1), 100)
        pg = max(int(page), 1)
    else:
        limit = min(max(int(limit or 10), 1), 100)
    eff_pw: date | None = None
    eff_sl = ""
    if attendance_city and str(attendance_city).strip():
        if prompt_war_on is None:
            eff_pw = IPCSR_LEGACY_PROMPT_WAR_DATE
            eff_sl = ""
        else:
            eff_pw = prompt_war_on
            eff_sl = session_label or ""
    out: dict = {
        "rows": [],
        "total": 0,
        "error": None,
        "scope": {
            "event_id": int(event_id),
            "attendance_city": attendance_city,
            "prompt_war_on": eff_pw.isoformat() if eff_pw else None,
            "session_label": eff_sl if eff_pw else None,
        },
    }
    try:
        with engine.connect() as conn:
            base_where = f"event_id = :eid AND sheet_kind = 'main'"
            params: dict = {"eid": int(event_id)}
            if attendance_city and str(attendance_city).strip():
                base_where += " AND lower(btrim(attendance_city)) = lower(btrim(:acity))"
                params["acity"] = str(attendance_city).strip()
                base_where += " AND prompt_war_on = :pwon AND session_label_normalized = lower(btrim(:slab))"
                params["pwon"] = eff_pw
                params["slab"] = eff_sl
            total = conn.execute(
                text(
                    f"""
                    SELECT COUNT(*)::BIGINT
                    FROM {TABLE_IN_PERSON_CHALLENGE_SUBMISSIONS}
                    WHERE {base_where}
                    """
                ),
                params,
            ).scalar()
            out["total"] = int(total or 0)
            row_params = dict(params)
            if paginate:
                row_params["lim"] = pp
                row_params["off"] = (pg - 1) * pp
                out["page"] = int(pg)
                out["per_page"] = int(pp)
                tot = out["total"]
                out["total_pages"] = max(1, (tot + pp - 1) // pp) if tot else 1
                limit_sql = "LIMIT :lim OFFSET :off"
            else:
                row_params["lim"] = limit
                limit_sql = "LIMIT :lim"
            rows = conn.execute(
                text(
                    f"""
                    WITH ranked AS (
                        SELECT id,
                               team_name,
                               leader_name,
                               leader_email,
                               attendance_city,
                               total_score,
                               export_created_at,
                               ROW_NUMBER() OVER (
                                 ORDER BY total_score DESC NULLS LAST,
                                          export_created_at ASC NULLS LAST,
                                          id ASC
                               ) AS rank
                        FROM {TABLE_IN_PERSON_CHALLENGE_SUBMISSIONS}
                        WHERE {base_where}
                    )
                    SELECT rank, team_name, leader_name, leader_email, attendance_city, total_score,
                           export_created_at AS submitted_at
                    FROM ranked
                    ORDER BY rank
                    {limit_sql}
                    """
                ),
                row_params,
            ).mappings().all()
            for r in rows:
                d = dict(r)
                if d.get("total_score") is not None:
                    d["total_score"] = float(d["total_score"])
                sa = d.get("submitted_at")
                if sa is not None and hasattr(sa, "isoformat"):
                    d["submitted_at"] = sa.isoformat()
                elif sa is not None:
                    d["submitted_at"] = str(sa)
                d["rank"] = int(d["rank"])
                out["rows"].append(d)
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)
    return out


def _in_person_pw_options(event_id: int) -> list[dict]:
    """Distinct Prompt War sessions (city + date + label) with main-challenge team counts."""
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    f"""
                    SELECT attendance_city AS city,
                           prompt_war_on,
                           session_label,
                           COUNT(*)::BIGINT AS team_count
                    FROM {TABLE_IN_PERSON_CHALLENGE_SUBMISSIONS}
                    WHERE event_id = :eid AND sheet_kind = 'main'
                    GROUP BY attendance_city, prompt_war_on, session_label
                    ORDER BY attendance_city ASC, prompt_war_on DESC, session_label ASC
                    """
                ),
                {"eid": int(event_id)},
            ).mappings().all()
        out: list[dict] = []
        for r in rows:
            c = r.get("city")
            if not c:
                continue
            pwo = r["prompt_war_on"]
            if isinstance(pwo, datetime):
                pwd = pwo.date()
                iso = pwd.isoformat()
            elif isinstance(pwo, date):
                pwd = pwo
                iso = pwd.isoformat()
            else:
                iso = str(pwo)[:10]
                pwd = date.fromisoformat(iso[:10])
            sl = str(r.get("session_label") or "")
            out.append(
                {
                    "city": str(c),
                    "prompt_war_on_iso": iso,
                    "session_label": sl,
                    "display": _ipcsr_pw_session_display(city=str(c), prompt_war_on=pwd, session_label=sl),
                    "team_count": int(r["team_count"] or 0),
                }
            )
        return out
    except Exception:  # noqa: BLE001
        return []


def _virtual_arena_challenge_stats(*, event_id: int, challenge_id: int) -> dict:
    """
    Per-arena-challenge: registration counts at opens_at / closes_at (MDC),
    submission totals, distinct MDC-linked rows.
    """
    out: dict = {
        "error": None,
        "challenge_id": int(challenge_id),
        "opens_at": None,
        "closes_at": None,
        "opens_at_display": None,
        "closes_at_display": None,
        "opens_at_set": False,
        "registrations_at_open": None,
        "registrations_at_close": 0,
        "total_submissions": 0,
        "unique_mdc_submissions": 0,
    }
    try:
        with engine.connect() as conn:
            ch = conn.execute(
                text(
                    """
                    SELECT c.id, c.title, c.event_id, c.opens_at, c.closes_at
                    FROM challenges c
                    JOIN events e ON e.id = c.event_id AND e.kind = 'virtual'
                    WHERE c.id = :cid AND c.event_id = :eid
                    """
                ),
                {"cid": int(challenge_id), "eid": int(event_id)},
            ).mappings().fetchone()
            if not ch:
                out["error"] = "challenge not found"
                return out
            oa = ch.get("opens_at")
            ca = ch.get("closes_at")
            out["opens_at"] = oa
            out["closes_at"] = ca
            out["opens_at_display"] = _format_dt_display(oa) if oa is not None else None
            out["closes_at_display"] = _format_dt_display(ca) if ca is not None else None
            out["opens_at_set"] = bool(oa is not None)

            reg_row = conn.execute(
                text(
                    f"""
                    SELECT
                      (SELECT COUNT(*)::BIGINT FROM {TABLE_VIRTUAL_MDC} m
                       WHERE m.event_id = c.event_id
                         AND m.form_timestamp IS NOT NULL
                         AND m.form_timestamp <= c.closes_at) AS reg_close,
                      CASE WHEN c.opens_at IS NULL THEN NULL::BIGINT
                           ELSE (
                             SELECT COUNT(*)::BIGINT FROM {TABLE_VIRTUAL_MDC} m2
                             WHERE m2.event_id = c.event_id
                               AND m2.form_timestamp IS NOT NULL
                               AND m2.form_timestamp <= c.opens_at
                           )
                      END AS reg_open
                    FROM challenges c
                    WHERE c.id = :cid AND c.event_id = :eid
                    """
                ),
                {"cid": int(challenge_id), "eid": int(event_id)},
            ).mappings().fetchone()
            if reg_row:
                out["registrations_at_close"] = int(reg_row["reg_close"] or 0)
                ro = reg_row.get("reg_open")
                out["registrations_at_open"] = int(ro) if ro is not None else None

            sub_row = conn.execute(
                text(
                    """
                    SELECT COUNT(*)::BIGINT AS total,
                           COUNT(DISTINCT virtual_mdc_registration_id)
                             FILTER (WHERE virtual_mdc_registration_id IS NOT NULL)::BIGINT AS uniq_mdc
                    FROM virtual_challenge_submission_rows
                    WHERE event_id = :eid AND challenge_id = :cid
                    """
                ),
                {"eid": int(event_id), "cid": int(challenge_id)},
            ).mappings().fetchone()
            if sub_row:
                out["total_submissions"] = int(sub_row["total"] or 0)
                out["unique_mdc_submissions"] = int(sub_row["uniq_mdc"] or 0)
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)
    return out


@app.get("/")
def main_dashboard():
    in_person_event_id = request.args.get("inPersonEventId", type=int) or DEFAULT_IN_PERSON_EVENT_ID
    virtual_event_id = request.args.get("virtualEventId", type=int) or DEFAULT_VIRTUAL_EVENT_ID
    challenge_id = request.args.get("challengeId", type=int) or DEFAULT_CHALLENGE_ID
    v_ov_raw = (request.args.get("virtualOverview") or "").strip()
    if v_ov_raw:
        v_lb_scope, v_ov_cid = _decode_virtual_overview(v_ov_raw)
        if v_lb_scope == "arena" and v_ov_cid is not None:
            challenge_id = int(v_ov_cid)
    else:
        raw_v_lb = (request.args.get("vLbScope") or "global").strip().lower()
        v_lb_scope = raw_v_lb if raw_v_lb in ("global", "arena") else "global"
    ip_ac_opts = _in_person_pw_options(in_person_event_id)
    ov_raw = (request.args.get("inPersonOverview") or "").strip()
    legacy_city = (request.args.get("inPersonTopCity") or "").strip() or None
    legacy_sess = (request.args.get("inPersonTopPwSession") or "").strip()
    if ov_raw:
        ip_lb_scope, focus_city_raw, raw_sess = _decode_in_person_overview(ov_raw)
    elif legacy_city:
        ip_lb_scope, focus_city_raw, raw_sess = "city", legacy_city, legacy_sess
    else:
        ip_lb_scope, focus_city_raw, raw_sess = "global", None, ""
    focus_pw_d, focus_pw_lab = _parse_main_dashboard_pw_session(raw_sess)
    focus_session_value = ""
    ip_focus_lb = None
    if ip_lb_scope == "city" and focus_city_raw:
        if focus_pw_d is None:
            focus_pw_d = IPCSR_LEGACY_PROMPT_WAR_DATE
            focus_pw_lab = ""
        ip_focus_lb = _in_person_submission_leaderboard(
            in_person_event_id,
            focus_city_raw,
            10,
            prompt_war_on=focus_pw_d,
            session_label=focus_pw_lab,
        )
        focus_session_value = f"{focus_pw_d.isoformat()}|{focus_pw_lab}"
    if ip_lb_scope == "global":
        in_person_overview_value = "global"
    elif focus_city_raw and focus_pw_d is not None:
        in_person_overview_value = _encode_in_person_overview_session(
            city=focus_city_raw,
            prompt_war_on_iso=focus_pw_d.isoformat(),
            session_label=focus_pw_lab or "",
        )
    else:
        in_person_overview_value = "global"
    ip_overview_options = [{"value": "global", "label": "Global (main challenge)"}]
    for o in ip_ac_opts:
        ip_overview_options.append(
            {
                "value": _encode_in_person_overview_session(
                    city=o["city"],
                    prompt_war_on_iso=o["prompt_war_on_iso"],
                    session_label=str(o.get("session_label") or ""),
                ),
                "label": o["display"],
            }
        )
    in_person_ac_focus_session_display = (
        _ipcsr_pw_session_display(city=focus_city_raw, prompt_war_on=focus_pw_d, session_label=focus_pw_lab)
        if ip_lb_scope == "city" and focus_city_raw and focus_pw_d
        else None
    )
    hero_ist_now = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d %H:%M IST")
    ip_per_city_leaderboard_url = None
    if ip_lb_scope == "city" and focus_city_raw and focus_pw_d is not None:
        ip_per_city_leaderboard_url = url_for(
            "in_person_leaderboard",
            inPersonEventId=in_person_event_id,
            virtualEventId=virtual_event_id,
            challengeId=challenge_id,
            ipActionCenterCity=focus_city_raw,
            ipPromptWarDate=focus_pw_d.isoformat(),
            ipPromptWarLabel=focus_pw_lab or "",
        )
    virtual_lb_challenges = _load_virtual_challenges_brief(virtual_event_id)
    overview = _fetch_overview_stats(
        in_person_event_id,
        virtual_event_id,
        arena_challenge_id=challenge_id,
    )
    ocid = overview.get("overview_arena_challenge_id")
    if isinstance(ocid, int):
        challenge_id = ocid
    virtual_overview_options = [{"value": "global", "label": "Global (all arenas)"}]
    seen_v_cids: set[int] = set()
    for ch in virtual_lb_challenges:
        cid = int(ch["id"])
        seen_v_cids.add(cid)
        virtual_overview_options.append(
            {
                "value": _encode_virtual_overview_challenge(cid),
                "label": str(ch.get("title") or f"Challenge {cid}"),
            }
        )
    if v_lb_scope == "arena" and int(challenge_id) not in seen_v_cids:
        virtual_overview_options.append(
            {
                "value": _encode_virtual_overview_challenge(int(challenge_id)),
                "label": f"Challenge {challenge_id}",
            }
        )
    if v_lb_scope == "global":
        virtual_overview_value = "global"
    else:
        virtual_overview_value = _encode_virtual_overview_challenge(int(challenge_id))
    return render_template(
        "main_dashboard.html",
        in_person_event_id=in_person_event_id,
        virtual_event_id=virtual_event_id,
        challenge_id=challenge_id,
        overview=overview,
        in_person_ac_focus_city=focus_city_raw,
        in_person_ac_focus_lb=ip_focus_lb,
        in_person_ac_focus_session_value=focus_session_value,
        in_person_overview_value=in_person_overview_value,
        ip_overview_options=ip_overview_options,
        in_person_ac_focus_session_display=in_person_ac_focus_session_display,
        hero_ist_now=hero_ist_now,
        ip_per_city_leaderboard_url=ip_per_city_leaderboard_url,
        ip_lb_scope=ip_lb_scope,
        v_lb_scope=v_lb_scope,
        virtual_overview_value=virtual_overview_value,
        virtual_overview_options=virtual_overview_options,
    )


@app.get("/api/in-person/main-data-center/stats")
def api_in_person_mdc_stats():
    """JSON payload for Main Data Center charts (optional ``mdc_date_from`` / ``mdc_date_to``, IST dates)."""
    eid = request.args.get("inPersonEventId", type=int) or DEFAULT_IN_PERSON_EVENT_ID
    d0 = _parse_mdc_dashboard_iso_date(request.args.get("mdc_date_from"))
    d1 = _parse_mdc_dashboard_iso_date(request.args.get("mdc_date_to"))
    payload = _load_mdc_stats(eid, mode="in_person", date_from=d0, date_to=d1)
    return jsonify(payload)


@app.get("/api/virtual/main-data-center/stats")
def api_virtual_mdc_stats():
    """JSON payload for Virtual Main Data Center charts (optional ``mdc_date_from`` / ``mdc_date_to``, IST dates)."""
    eid = request.args.get("virtualEventId", type=int) or DEFAULT_VIRTUAL_EVENT_ID
    d0 = _parse_mdc_dashboard_iso_date(request.args.get("mdc_date_from"))
    d1 = _parse_mdc_dashboard_iso_date(request.args.get("mdc_date_to"))
    payload = _load_mdc_stats(eid, mode="virtual", date_from=d0, date_to=d1)
    return jsonify(payload)


@app.get("/in-person")
def in_person_page():
    in_person_event_id = request.args.get("inPersonEventId", type=int) or DEFAULT_IN_PERSON_EVENT_ID
    virtual_event_id = request.args.get("virtualEventId", type=int) or DEFAULT_VIRTUAL_EVENT_ID
    challenge_id = request.args.get("challengeId", type=int) or DEFAULT_CHALLENGE_ID
    mdc_df = _parse_mdc_dashboard_iso_date(request.args.get("mdc_date_from"))
    mdc_dt = _parse_mdc_dashboard_iso_date(request.args.get("mdc_date_to"))
    data_center = _load_mdc_stats(
        in_person_event_id,
        mode="in_person",
        date_from=mdc_df,
        date_to=mdc_dt,
    )
    ip_ac_city = (request.args.get("ipActionCenterCity") or "").strip() or None
    ip_pw_date = _parse_ipcsr_prompt_war_date_from_form(request.args.get("ipPromptWarDate"))
    ip_pw_label = _normalize_ipcsr_session_label(request.args.get("ipPromptWarLabel"))
    if ip_ac_city:
        if ip_pw_date is None:
            ip_pw_date = IPCSR_LEGACY_PROMPT_WAR_DATE
            ip_pw_label = ""
        in_person_action_lb = _in_person_submission_leaderboard(
            in_person_event_id,
            ip_ac_city,
            10,
            prompt_war_on=ip_pw_date,
            session_label=ip_pw_label,
        )
    else:
        in_person_action_lb = _in_person_submission_leaderboard(in_person_event_id, None, 10)
    in_person_action_pw_options = _in_person_pw_options(in_person_event_id)
    ip_ac_session_display = (
        _ipcsr_pw_session_display(city=ip_ac_city, prompt_war_on=ip_pw_date, session_label=ip_pw_label)
        if ip_ac_city and ip_pw_date
        else None
    )
    return render_template(
        "in_person.html",
        in_person_event_id=in_person_event_id,
        virtual_event_id=virtual_event_id,
        challenge_id=challenge_id,
        data_center=data_center,
        in_person_action_lb=in_person_action_lb,
        in_person_action_pw_options=in_person_action_pw_options,
        ip_action_center_city=ip_ac_city,
        ip_ac_pw_date_iso=ip_pw_date.isoformat() if ip_ac_city and ip_pw_date else None,
        ip_ac_session_label=ip_pw_label if ip_ac_city else "",
        ip_ac_session_display=ip_ac_session_display,
    )


@app.get("/in-person/leaderboard")
def in_person_leaderboard():
    """Paginated main-challenge standings (all ranks) for the selected Prompt War session."""
    in_person_event_id = request.args.get("inPersonEventId", type=int) or DEFAULT_IN_PERSON_EVENT_ID
    virtual_event_id = request.args.get("virtualEventId", type=int) or DEFAULT_VIRTUAL_EVENT_ID
    challenge_id = request.args.get("challengeId", type=int) or DEFAULT_CHALLENGE_ID
    page = request.args.get("page", type=int) or 1
    per_page = request.args.get("per_page", type=int) or 50
    ip_ac_city = (request.args.get("ipActionCenterCity") or "").strip() or None
    ip_pw_date = _parse_ipcsr_prompt_war_date_from_form(request.args.get("ipPromptWarDate"))
    ip_pw_label = _normalize_ipcsr_session_label(request.args.get("ipPromptWarLabel"))
    if ip_ac_city:
        if ip_pw_date is None:
            ip_pw_date = IPCSR_LEGACY_PROMPT_WAR_DATE
            ip_pw_label = ""
        in_person_action_lb = _in_person_submission_leaderboard(
            in_person_event_id,
            ip_ac_city,
            10,
            prompt_war_on=ip_pw_date,
            session_label=ip_pw_label,
            page=page,
            per_page=per_page,
        )
    else:
        in_person_action_lb = _in_person_submission_leaderboard(
            in_person_event_id,
            None,
            10,
            page=page,
            per_page=per_page,
        )
    in_person_action_pw_options = _in_person_pw_options(in_person_event_id)
    ip_ac_session_display = (
        _ipcsr_pw_session_display(city=ip_ac_city, prompt_war_on=ip_pw_date, session_label=ip_pw_label)
        if ip_ac_city and ip_pw_date
        else None
    )
    return render_template(
        "in_person_leaderboard.html",
        title="In-person · Leaderboard",
        in_person_event_id=in_person_event_id,
        virtual_event_id=virtual_event_id,
        challenge_id=challenge_id,
        in_person_action_lb=in_person_action_lb,
        in_person_action_pw_options=in_person_action_pw_options,
        ip_action_center_city=ip_ac_city,
        ip_ac_pw_date_iso=ip_pw_date.isoformat() if ip_ac_city and ip_pw_date else None,
        ip_ac_session_label=ip_pw_label if ip_ac_city else "",
        ip_ac_session_display=ip_ac_session_display,
    )


@app.get("/virtual")
def virtual_page():
    in_person_event_id = request.args.get("inPersonEventId", type=int) or DEFAULT_IN_PERSON_EVENT_ID
    virtual_event_id = request.args.get("virtualEventId", type=int) or DEFAULT_VIRTUAL_EVENT_ID
    challenges = _load_virtual_challenges_brief(virtual_event_id)
    id_set = {int(c["id"]) for c in challenges}
    eligibility_requested = request.args.get("challengeId", type=int)
    arena_requested = request.args.get("arenaChallengeId", type=int)
    if eligibility_requested is not None and id_set and eligibility_requested not in id_set:
        q = request.args.to_dict(flat=True)
        q.pop("challengeId", None)
        return redirect(f"{request.path}?{urlencode(q)}")
    if arena_requested is not None and id_set and arena_requested not in id_set:
        q = request.args.to_dict(flat=True)
        q["arenaChallengeId"] = str(int(challenges[0]["id"]))
        return redirect(f"{request.path}?{urlencode(q)}")
    seed = _effective_arena_challenge_seed(
        arena_challenge_id=arena_requested,
        eligibility_challenge_id=eligibility_requested,
        valid_ids=id_set,
    )
    challenge_id, challenges = _resolve_virtual_arena_challenge_id(
        virtual_event_id, requested=seed, challenges=challenges
    )
    if challenge_id is not None:
        leaderboard, distribution, dist_bins = _load_virtual_bundle(challenge_id)
    else:
        leaderboard, distribution, dist_bins = (
            {"rows": [], "error": None},
            {"bins": [], "error": None},
            [],
        )
    mdc_df = _parse_mdc_dashboard_iso_date(request.args.get("mdc_date_from"))
    mdc_dt = _parse_mdc_dashboard_iso_date(request.args.get("mdc_date_to"))
    data_center = _load_mdc_stats(
        virtual_event_id,
        mode="virtual",
        date_from=mdc_df,
        date_to=mdc_dt,
    )
    eligibility = None
    active_challenge_id = eligibility_requested
    if active_challenge_id:
        eligibility = _load_virtual_eligibility_summary(virtual_event_id, active_challenge_id)
    arena_stats = (
        _virtual_arena_challenge_stats(event_id=virtual_event_id, challenge_id=challenge_id)
        if challenge_id is not None
        else None
    )
    arena_challenge_title = None
    if challenge_id is not None:
        for ch in challenges:
            if int(ch["id"]) == int(challenge_id):
                arena_challenge_title = ch.get("title")
                break
    raw_standings = (request.args.get("standingsView") or "").strip()
    id_set_int = id_set
    if not raw_standings:
        if challenge_id is not None and (not id_set_int or int(challenge_id) in id_set_int):
            standings_is_global = False
            standings_challenge_id = int(challenge_id)
            standings_view_value = str(int(challenge_id))
        else:
            standings_is_global = True
            standings_challenge_id = None
            standings_view_value = "global"
    elif raw_standings.lower() == "global":
        standings_is_global = True
        standings_challenge_id = None
        standings_view_value = "global"
    else:
        try:
            sv_cid = int(raw_standings)
        except ValueError:
            sv_cid = 0
        if id_set_int and sv_cid in id_set_int:
            standings_is_global = False
            standings_challenge_id = sv_cid
            standings_view_value = str(sv_cid)
        elif challenge_id is not None and (not id_set_int or int(challenge_id) in id_set_int):
            standings_is_global = False
            standings_challenge_id = int(challenge_id)
            standings_view_value = str(int(challenge_id))
        else:
            standings_is_global = True
            standings_challenge_id = None
            standings_view_value = "global"
    if standings_is_global:
        standings_payload = _virtual_global_submission_leaderboard(
            event_id=virtual_event_id, limit=400, offset=0
        )
    elif standings_challenge_id is not None:
        standings_payload = _submission_leaderboard_payload(
            event_id=virtual_event_id,
            challenge_id=int(standings_challenge_id),
            limit=400,
            offset=0,
        )
    else:
        standings_payload = {"rows": [], "total": 0, "error": None, "challenge": None}
    standings_leaderboard = {
        "is_global": standings_is_global,
        "rows": standings_payload.get("rows") or [],
        "total": int(standings_payload.get("total") or 0),
        "error": standings_payload.get("error"),
        "challenge": standings_payload.get("challenge"),
    }
    return render_template(
        "virtual.html",
        in_person_event_id=in_person_event_id,
        virtual_event_id=virtual_event_id,
        challenge_id=challenge_id,
        leaderboard=leaderboard,
        distribution=distribution,
        dist_bins=dist_bins,
        data_center=data_center,
        virtual_challenges=challenges,
        active_challenge_id=active_challenge_id,
        eligibility=eligibility,
        arena_stats=arena_stats,
        standings_leaderboard=standings_leaderboard,
        standings_view_value=standings_view_value,
        arena_challenge_title=arena_challenge_title,
    )


@app.get("/virtual/leaderboard")
def virtual_submission_leaderboard():
    in_person_event_id = request.args.get("inPersonEventId", type=int) or DEFAULT_IN_PERSON_EVENT_ID
    virtual_event_id = request.args.get("virtualEventId", type=int) or DEFAULT_VIRTUAL_EVENT_ID
    challenges = _load_virtual_challenges_brief(virtual_event_id)
    id_set = {int(c["id"]) for c in challenges}
    url_challenge = request.args.get("challengeId", type=int)
    arena_requested = request.args.get("arenaChallengeId", type=int)
    is_global = (request.args.get("global", type=int) or 0) == 1
    if not is_global:
        if url_challenge is not None and id_set and url_challenge not in id_set and arena_requested is None:
            q = request.args.to_dict(flat=True)
            q.pop("challengeId", None)
            return redirect(f"{request.path}?{urlencode(q)}")
        if arena_requested is not None and id_set and arena_requested not in id_set:
            q = request.args.to_dict(flat=True)
            q["arenaChallengeId"] = str(int(challenges[0]["id"]))
            return redirect(f"{request.path}?{urlencode(q)}")
    seed = _effective_arena_challenge_seed(
        arena_challenge_id=arena_requested,
        eligibility_challenge_id=url_challenge,
        valid_ids=id_set,
    )
    challenge_id, challenges = _resolve_virtual_arena_challenge_id(
        virtual_event_id, requested=seed, challenges=challenges
    )
    page = request.args.get("page", default=1, type=int) or 1
    per_page = request.args.get("per_page", default=25, type=int) or 25
    per_page = min(max(int(per_page), 10), 100)
    page = max(int(page), 1)
    offset = (page - 1) * per_page
    if is_global:
        payload = _virtual_global_submission_leaderboard(
            event_id=virtual_event_id,
            page=page,
            per_page=per_page,
        )
        challenge_id = None
    else:
        payload = (
            _submission_leaderboard_payload(
                event_id=virtual_event_id,
                challenge_id=challenge_id,
                limit=per_page,
                offset=offset,
            )
            if challenge_id is not None
            else {"rows": [], "total": 0, "error": None, "challenge": None}
        )
    total = int(payload.get("total") or 0)
    total_pages = max(1, (total + per_page - 1) // per_page) if total else 1
    return render_template(
        "virtual_leaderboard.html",
        title="Virtual · Submission leaderboard",
        in_person_event_id=in_person_event_id,
        virtual_event_id=virtual_event_id,
        challenge_id=challenge_id,
        submission_leaderboard=payload,
        virtual_challenges=challenges,
        page=page,
        per_page=per_page,
        total_pages=total_pages,
        is_global_leaderboard=is_global,
    )


@app.get("/overview/settings")
def overview_settings():
    return render_template(
        "module_sheet.html",
        title="Overview · Settings",
        description="Workspace defaults, API keys, and operational tools for the whole program.",
        show_admin_link=True,
    )


@app.get("/overview/logs")
@audit_view(entity="overview_logs", action="VIEW", module="overview")
def overview_logs():
    """Browse HTTP/SQL activity and row-level data-change audit (search + time window)."""
    kind = (request.args.get("kind") or "activity").strip().lower()
    if kind not in ("activity", "data"):
        kind = "activity"
    page = max(1, request.args.get("page", default=1, type=int) or 1)
    per_page = request.args.get("per_page", default=25, type=int) or 25
    per_page = min(100, max(5, per_page))
    q = (request.args.get("q") or "").strip()[:300]
    window_hours = _parse_logs_window_hours(request.args.get("window"))
    since: datetime | None = None
    if window_hours is not None:
        since = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    action_eq = (request.args.get("action") or "").strip()[:120]

    activity: dict[str, object] | None = None
    data: dict[str, object] | None = None
    audit_ready = True
    schema_error: str | None = None

    try:
        with engine.connect() as conn:
            if not _audit_logs_schema_ok(conn):
                audit_ready = False
                schema_error = (
                    "Audit tables are not present. Apply database/audit.sql to this database, "
                    "then reload this page."
                )
            elif kind == "activity":
                activity = _load_overview_activity_logs(
                    conn,
                    q=q,
                    page=page,
                    per_page=per_page,
                    since=since,
                    action_eq=action_eq,
                )
            else:
                data = _load_overview_data_logs(
                    conn,
                    q=q,
                    page=page,
                    per_page=per_page,
                    since=since,
                )
    except Exception as exc:  # noqa: BLE001
        err_block: dict[str, object] = {
            "error": str(exc),
            "rows": [],
            "total": 0,
            "page": page,
            "per_page": per_page,
            "total_pages": 1,
            "search": q,
        }
        if kind == "activity":
            activity = {
                **err_block,
                "action": action_eq,
                "action_options": [],
            }
        else:
            data = err_block

    return render_template(
        "overview_logs.html",
        title="Overview · Logs",
        kind=kind,
        activity=activity,
        data=data,
        audit_ready=audit_ready,
        schema_error=schema_error,
        window_hours=window_hours,
        q=q,
        action_eq=action_eq,
    )


@app.get("/in-person/users")
def in_person_users():
    page = request.args.get("page", default=1, type=int) or 1
    per_page = request.args.get("per_page", default=25, type=int) or 25
    q = (request.args.get("q") or "").strip()[:200]
    ac_raw = request.args.get("attendance_city")
    ac = (ac_raw or "").strip()[:200] or None
    advanced = _parse_mdc_users_advanced_from_request(request.args)
    users = _load_mdc_users_page(
        DEFAULT_IN_PERSON_EVENT_ID, page, per_page, q, ac, advanced=advanced
    )
    return render_template(
        "in_person_users.html",
        title="In-person · Users",
        users=users,
    )


@app.get("/in-person/users/export.csv")
@audit_view(
    entity="in_person_main_data_center_registrations",
    action="EXPORT",
    module="in_person",
    extra_fn=lambda *a, **kw: {
        "q": (request.args.get("q") or "").strip()[:200] or None,
        "attendance_city": (request.args.get("attendance_city") or "").strip()[:200] or None,
        "advanced_keys": sorted(
            k
            for k in request.args
            if k.startswith("af_")
            or k
            in (
                "form_ts_from",
                "form_ts_to",
                "dob_from",
                "dob_to",
                "designation_years_min",
                "designation_years_max",
            )
        )[:40],
    },
)
def in_person_users_export_csv():
    q = (request.args.get("q") or "").strip()[:200]
    ac_raw = request.args.get("attendance_city")
    ac = (ac_raw or "").strip()[:200] or None
    advanced = _parse_mdc_users_advanced_from_request(request.args)
    rows, err = _fetch_mdc_users_export_rows(
        DEFAULT_IN_PERSON_EVENT_ID, q, ac, advanced=advanced
    )
    if err:
        return Response(err, status=500, mimetype="text/plain; charset=utf-8")
    payload = _mdc_users_rows_to_csv(rows, mode="in_person")
    return Response(
        payload,
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="in-person-mdc-registrations.csv"'},
    )


@app.get("/in-person/settings")
def in_person_settings():
    return render_template(
        "module_sheet.html",
        title="In-person · Settings",
        description="Funnel thresholds, import schedules, and city configuration.",
    )


@app.get("/virtual/users")
def virtual_users():
    page = request.args.get("page", default=1, type=int) or 1
    per_page = request.args.get("per_page", default=25, type=int) or 25
    q = (request.args.get("q") or "").strip()[:200]
    cid = request.args.get("challengeId", type=int)
    advanced = _parse_mdc_users_advanced_from_request(request.args)
    users = _load_mdc_users_page(
        DEFAULT_VIRTUAL_EVENT_ID, page, per_page, q, None,
        mode="virtual", challenge_id=cid, advanced=advanced,
    )
    challenges = _load_virtual_challenges_brief(DEFAULT_VIRTUAL_EVENT_ID)
    return render_template(
        "virtual_users.html",
        title="Virtual · Users",
        users=users,
        virtual_challenges=challenges,
        active_challenge_id=cid,
    )


@app.get("/virtual/users/export.csv")
@audit_view(
    entity="virtual_main_data_center_registrations",
    action="EXPORT",
    module="virtual",
    extra_fn=lambda *a, **kw: {
        "q": (request.args.get("q") or "").strip()[:200] or None,
        "advanced_keys": sorted(
            k
            for k in request.args
            if k.startswith("af_")
            or k
            in (
                "form_ts_from",
                "form_ts_to",
                "dob_from",
                "dob_to",
                "designation_years_min",
                "designation_years_max",
            )
        )[:40],
    },
)
def virtual_users_export_csv():
    q = (request.args.get("q") or "").strip()[:200]
    cid = request.args.get("challengeId", type=int)
    advanced = _parse_mdc_users_advanced_from_request(request.args)
    rows, err = _fetch_mdc_users_export_rows(
        DEFAULT_VIRTUAL_EVENT_ID, q, None, mode="virtual", challenge_id=cid, advanced=advanced
    )
    if err:
        return Response(err, status=500, mimetype="text/plain; charset=utf-8")
    payload = _mdc_users_rows_to_csv(rows, mode="virtual")
    return Response(
        payload,
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="virtual-mdc-registrations.csv"'},
    )


@app.get("/virtual/settings")
def virtual_settings():
    return render_template(
        "module_sheet.html",
        title="Virtual · Settings",
        description="Leaderboard refresh interval, challenge lifecycle, and reward rules.",
    )


# ---------- Virtual challenge management -----------------------------------


_CHALLENGE_STATUSES = ("draft", "live", "closed")


def _parse_challenge_form(form) -> tuple[dict | None, str | None]:
    """Validate the challenge admin form. Returns (payload, error_message)."""
    title = (form.get("title") or "").strip()
    description = (form.get("description") or "").strip() or None
    slug = (form.get("slug") or "").strip() or None
    import_sheet_suffix = (form.get("import_sheet_suffix") or "").strip() or None
    if import_sheet_suffix and len(import_sheet_suffix) > 200:
        return None, "import_sheet_suffix must be at most 200 characters."
    status = (form.get("status") or "draft").strip().lower()
    opens_raw = (form.get("opens_at") or "").strip()
    closes_raw = (form.get("closes_at") or "").strip()

    if not title:
        return None, "Title is required."
    if status not in _CHALLENGE_STATUSES:
        return None, f"Status must be one of {', '.join(_CHALLENGE_STATUSES)}."

    def _parse_dt(s: str) -> datetime | None:
        if not s:
            return None
        s2 = s.replace("T", " ")
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(s2, fmt)
            except ValueError:
                continue
        return None

    opens_at = _parse_dt(opens_raw)
    closes_at = _parse_dt(closes_raw)
    if opens_raw and opens_at is None:
        return None, "opens_at is not a valid date/time."
    if closes_raw and closes_at is None:
        return None, "closes_at is not a valid date/time."
    if opens_at and closes_at and not (opens_at < closes_at):
        return None, "opens_at must be earlier than closes_at."
    if not closes_at:
        return None, "closes_at is required (defines eligibility cutoff)."

    return (
        {
            "title": title[:200],
            "description": description,
            "slug": slug[:200] if slug else None,
            "import_sheet_suffix": import_sheet_suffix[:200] if import_sheet_suffix else None,
            "opens_at": opens_at,
            "closes_at": closes_at,
            "status": status,
        },
        None,
    )


@app.get("/virtual/challenges")
def virtual_challenges():
    event_id = request.args.get("virtualEventId", type=int) or DEFAULT_VIRTUAL_EVENT_ID
    challenges = _load_virtual_challenges(event_id)
    err = (request.args.get("error") or "").strip()[:200] or None
    ok = (request.args.get("ok") or "").strip()[:200] or None
    edit_id = request.args.get("edit", type=int)
    return render_template(
        "virtual_challenges.html",
        title="Virtual · Challenges",
        virtual_event_id=event_id,
        challenges=challenges,
        error=err,
        ok=ok,
        edit_id=edit_id,
        statuses=_CHALLENGE_STATUSES,
    )


@app.post("/virtual/challenges")
@audit_view(
    entity="challenges",
    action="INSERT",
    module="virtual",
    extra_fn=lambda *a, **kw: {"title": (request.form.get("title") or "").strip()[:200] or None},
)
def virtual_challenges_create():
    event_id = request.args.get("virtualEventId", type=int) or DEFAULT_VIRTUAL_EVENT_ID
    payload, err = _parse_challenge_form(request.form)
    if err:
        return redirect(
            url_for("virtual_challenges", virtualEventId=event_id, error=err, open_create="1")
        ), 303
    try:
        with engine.begin() as conn:
            ev = conn.execute(
                text("SELECT kind FROM events WHERE id = :eid"),
                {"eid": event_id},
            ).fetchone()
            if not ev or str(ev[0]) != "virtual":
                return redirect(url_for(
                    "virtual_challenges",
                    virtualEventId=event_id,
                    error="Event is not a virtual event.",
                    open_create="1",
                )), 303
            conn.execute(
                text(
                    """
                    INSERT INTO challenges (event_id, title, description, slug, import_sheet_suffix,
                                            opens_at, closes_at, status)
                    VALUES (:eid, :title, :description, :slug, :import_sheet_suffix, :opens_at, :closes_at, :status)
                    """
                ),
                {"eid": event_id, **payload},
            )
    except Exception as exc:  # noqa: BLE001
        return redirect(url_for(
            "virtual_challenges",
            virtualEventId=event_id,
            error=f"Could not create challenge: {exc}",
            open_create="1",
        )), 303
    return redirect(url_for(
        "virtual_challenges", virtualEventId=event_id, ok="Challenge created.",
    )), 303


@app.post("/virtual/challenges/<int:cid>")
@audit_view(
    entity="challenges",
    action="UPDATE",
    module="virtual",
    extra_fn=lambda cid, *a, **kw: {"challenge_id": int(cid)},
)
def virtual_challenges_update(cid: int):
    event_id = request.args.get("virtualEventId", type=int) or DEFAULT_VIRTUAL_EVENT_ID
    existing = _get_virtual_challenge(cid)
    if not existing:
        return redirect(url_for(
            "virtual_challenges", virtualEventId=event_id,
            error="Challenge not found or not a virtual challenge.",
        )), 303
    payload, err = _parse_challenge_form(request.form)
    if err:
        return redirect(url_for(
            "virtual_challenges", virtualEventId=event_id, edit=cid, error=err,
        )), 303
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE challenges
                    SET title = :title,
                        description = :description,
                        slug = :slug,
                        import_sheet_suffix = :import_sheet_suffix,
                        opens_at = :opens_at,
                        closes_at = :closes_at,
                        status = :status
                    WHERE id = :cid
                    """
                ),
                {"cid": cid, **payload},
            )
    except Exception as exc:  # noqa: BLE001
        return redirect(url_for(
            "virtual_challenges", virtualEventId=event_id, edit=cid,
            error=f"Could not update challenge: {exc}",
        )), 303
    return redirect(url_for(
        "virtual_challenges", virtualEventId=event_id, ok="Challenge updated.",
    )), 303


@app.post("/virtual/challenges/<int:cid>/delete")
@audit_view(
    entity="challenges",
    action="DELETE",
    module="virtual",
    extra_fn=lambda cid, *a, **kw: {"challenge_id": int(cid)},
)
def virtual_challenges_delete(cid: int):
    event_id = request.args.get("virtualEventId", type=int) or DEFAULT_VIRTUAL_EVENT_ID
    existing = _get_virtual_challenge(cid)
    if not existing:
        return redirect(url_for(
            "virtual_challenges", virtualEventId=event_id,
            error="Challenge not found or not a virtual challenge.",
        )), 303
    try:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM challenges WHERE id = :cid"), {"cid": cid})
    except Exception as exc:  # noqa: BLE001
        return redirect(url_for(
            "virtual_challenges", virtualEventId=event_id,
            error=f"Could not delete challenge: {exc}",
        )), 303
    return redirect(url_for(
        "virtual_challenges", virtualEventId=event_id, ok="Challenge deleted.",
    )), 303


@app.get("/api/virtual/challenges")
def api_virtual_challenges():
    event_id = request.args.get("virtualEventId", type=int) or DEFAULT_VIRTUAL_EVENT_ID
    rows = _load_virtual_challenges_brief(event_id)
    return jsonify({
        "event_id": event_id,
        "challenges": [
            {
                "id": int(r["id"]),
                "title": r["title"],
                "import_sheet_suffix": r.get("import_sheet_suffix"),
                "status": r["status"],
                "opens_at": _format_dt_display(r["opens_at"]) or None,
                "closes_at": _format_dt_display(r["closes_at"]) or None,
            }
            for r in rows
        ],
    })


@app.get("/api/virtual/challenges/<int:cid>/eligibility")
def api_virtual_challenge_eligibility(cid: int):
    event_id = request.args.get("virtualEventId", type=int) or DEFAULT_VIRTUAL_EVENT_ID
    summary = _load_virtual_eligibility_summary(event_id, cid)
    if summary.get("error") == "challenge not found for event":
        return jsonify(summary), 404
    return jsonify(summary)


@app.get("/in-person/import")
def in_person_import():
    eid = request.args.get("inPersonEventId", type=int) or DEFAULT_IN_PERSON_EVENT_ID
    try:
        with engine.connect() as conn:
            ac_opts = _load_mdc_attendance_city_options(conn, eid, mode="in_person")
    except Exception:  # noqa: BLE001
        ac_opts = []
    return render_template(
        "import_in_person.html",
        in_person_event_id=eid,
        action_center_attendance_cities=ac_opts,
    )


@app.get("/virtual/import")
def virtual_import():
    virtual_event_id = request.args.get("virtualEventId", type=int) or DEFAULT_VIRTUAL_EVENT_ID
    return render_template(
        "import_virtual.html",
        virtual_event_id=virtual_event_id,
    )


@app.get("/login")
@app.get("/admin/login")
def portal_login():
    """Public: send users to the CDI portal for authentication."""
    return redirect(f"{get_portal_url().rstrip('/')}/login")


@app.get("/logout")
@app.post("/logout")
def logout():
    from audit.admin_hooks import log_logout

    try:
        log_logout()
    except Exception:  # noqa: BLE001
        pass
    session.clear()
    resp = make_response(redirect(f"{get_portal_url().rstrip('/')}/dashboard"))
    resp.delete_cookie("h2s_cdi_session", path="/")
    return resp


@app.get("/admin")
def admin_page():
    health_payload: dict = {"ok": False}
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        health_payload = {"ok": True, "database": "up"}
    except Exception as exc:  # noqa: BLE001
        health_payload = {"ok": False, "database": "down", "detail": str(exc)}

    latest_job = None
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT id, module, status, started_at, finished_at, error_message, row_counts, created_at
                    FROM import_jobs
                    ORDER BY id DESC
                    LIMIT 1
                    """
                ),
            ).mappings().fetchone()
        if row:
            latest_job = dict(row)
            for k in ("started_at", "finished_at", "created_at"):
                if latest_job.get(k) is not None and hasattr(latest_job[k], "isoformat"):
                    latest_job[k] = latest_job[k].isoformat()
    except Exception:  # noqa: BLE001
        latest_job = None

    return render_template(
        "admin.html",
        title="Admin — Prompt Wars",
        health=health_payload,
        latestJob=latest_job,
    )


def _register_cdi_page_id_redirects() -> None:
    """Map ``/<pageId>`` (portal-style) to canonical routes under SCRIPT_NAME."""

    def _make_redirect(target_path: str):
        def _go() -> Response:
            sn = (request.environ.get("SCRIPT_NAME") or "").rstrip("/")
            loc = f"{sn}{target_path}" if sn else target_path
            return redirect(loc)

        return _go

    for _p in MODULE_PAGES:
        pid = _p["pageId"]
        tp = _p["path"]
        fn = _make_redirect(tp)
        fn.__name__ = f"cdi_page_redirect_{pid}"
        app.add_url_rule(f"/{pid}", endpoint=f"cdi_page_redirect_{pid}", view_func=fn, methods=["GET"])


_register_cdi_page_id_redirects()


def main():
    mode = "DEBUG" if DEBUG_MODE else "PROD"
    register_with_portal(MODULE_PAGES, module_name=MODULE_DISPLAY_NAME, base_url=MODULE_BASE_URL)
    print(
        f"Prompt Wars [{mode}] listening on http://{APP_HOST}:{APP_PORT}"
        f" (reloader={'on' if USE_RELOADER else 'off'})",
        file=sys.stderr,
    )
    app.run(host=APP_HOST, port=APP_PORT, debug=DEBUG_MODE, use_reloader=USE_RELOADER)


if __name__ == "__main__":
    main()
