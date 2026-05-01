"""
Prompt Wars — Flask-only dashboard and data API.

Run: python app.py
"""

from __future__ import annotations

import base64
import csv
import json
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from decimal import Decimal
from io import StringIO
from pathlib import Path
from urllib.parse import urlencode

from dotenv import load_dotenv
from flask import Flask, g, jsonify, make_response, redirect, render_template, request, Response, send_from_directory, session, url_for
from logging.handlers import RotatingFileHandler
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.middleware.proxy_fix import ProxyFix
from sqlalchemy import bindparam, text
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.pool import QueuePool

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
from services import hawkeye as hawkeye_service  # noqa: E402
from services import submission_analytics as submission_analytics_svc  # noqa: E402
from services import in_person_rsvp_list_import as ip_rsvp_list_svc  # noqa: E402
from services import virtual_challenge_attempts_sheet as vcsr_attempts_sheet_svc  # noqa: E402
from services.hawkeye import HawkeyeError, HawkeyeNotConfiguredError  # noqa: E402
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

# Cross-arena / all-PW global leaderboard UI and routes (set PW_GLOBAL_LEADERBOARDS_ENABLED=1 to show again).
PW_GLOBAL_LEADERBOARDS_ENABLED = _env_bool("PW_GLOBAL_LEADERBOARDS_ENABLED", False)

# Main Data Center registration exports: separate physical tables per track.
TABLE_IN_PERSON_MDC = "in_person_main_data_center_registrations"
TABLE_VIRTUAL_MDC = "virtual_main_data_center_registrations"
TABLE_IN_PERSON_CHALLENGE_SUBMISSIONS = "in_person_challenge_submission_rows"
TABLE_IN_PERSON_PW_SESSIONS = "in_person_pw_sessions"
TABLE_IN_PERSON_RSVP_LIST_EMAILS = "in_person_pw_session_rsvp_list_emails"

# Attendance-city dropdowns (MDC import, PW sessions, Action Center) are built from distinct MDC values;
# add cities here so they appear before any registration uses them (case-insensitive dedupe vs MDC).
IN_PERSON_PW_EXTRA_ATTENDANCE_CITIES: tuple[str, ...] = ("Gurugram",)

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

# Virtual roster: chart-linked filters (must match ``_virtual_arena_challenge_stats`` segment SQL).
_MDC_ARENA_TEAM_SEGMENTS = frozenset({"student", "professional", "other", "unknown"})

# MDC roster table sorting (query params ``sort`` / ``sort_dir``); values map to fixed SQL only.
_ROSTER_SORT_KEYS_VIRTUAL = frozenset(
    {"name", "email", "location", "occupation", "designation", "yrs_exp", "registered", "score"}
)
_ROSTER_SORT_KEYS_IN_PERSON = frozenset(
    {
        "name",
        "email",
        "location",
        "attendance_city",
        "occupation",
        "designation",
        "yrs_exp",
        "registered",
    }
)


def _parse_mdc_users_roster_sort(args, *, mode: str) -> tuple[str | None, str]:
    raw = (args.get("sort") or "").strip().lower()[:24]
    dr = (args.get("sort_dir") or "").strip().lower()[:4]
    dir_ok = "asc" if dr == "asc" else "desc"
    allowed = _ROSTER_SORT_KEYS_VIRTUAL if mode == "virtual" else _ROSTER_SORT_KEYS_IN_PERSON
    if raw not in allowed:
        return None, dir_ok
    return raw, dir_ok


def _mdc_users_roster_order_clause(
    sort_key: str | None,
    sort_dir: str,
    *,
    mode: str,
    challenge_id: int | None,
) -> str:
    """Default: newest registration first. ``sort_key`` must already be allowlisted."""
    default = "ORDER BY form_timestamp DESC NULLS LAST, id DESC"
    if not sort_key:
        return default
    if sort_key == "score" and mode != "virtual":
        return default
    dir_sql = "ASC" if (sort_dir or "").lower() == "asc" else "DESC"
    if sort_key == "name":
        expr = "lower(btrim(COALESCE(full_name, '')))"
    elif sort_key == "email":
        expr = "lower(btrim(COALESCE(email, '')))"
    elif sort_key == "location":
        expr = "lower(btrim(COALESCE(city, '') || ' ' || COALESCE(state, '')))"
    elif sort_key == "attendance_city":
        expr = "lower(btrim(COALESCE(attendance_city, '')))"
    elif sort_key == "occupation":
        expr = "lower(btrim(COALESCE(occupation, '')))"
    elif sort_key == "designation":
        expr = "lower(btrim(COALESCE(designation, '')))"
    elif sort_key == "yrs_exp":
        expr = "designation_years_experience"
    elif sort_key == "registered":
        expr = "form_timestamp"
    elif sort_key == "score":
        expr = "mdc_submission_score"
    else:
        return default
    return f"ORDER BY {expr} {dir_sql} NULLS LAST, id DESC"


def _mdc_users_roster_sort_href_query(
    preserve: dict[str, str],
    per_page: int,
    column: str,
    current_key: str | None,
    current_dir: str,
) -> str:
    body = dict(preserve)
    body["per_page"] = str(per_page)
    next_dir = "desc" if (current_key == column and (current_dir or "").lower() == "asc") else "asc"
    body["sort"] = column
    body["sort_dir"] = next_dir
    return urlencode(body)


def _mdc_users_virtual_submission_score_select_sql(table: str, challenge_id: int | None) -> str:
    """Scalar ``total_score`` from ``virtual_challenge_submission_rows`` for roster rows.

    With ``challenge_id``: that challenge only. Without: best non-null ``total_score`` for the
    workspace (``event_id``), then newest ``updated_at`` / ``id`` as tie-breakers so scores show
    even when **Eligible for challenge** is left on “All registrants”.
    """
    if challenge_id:
        return f"""(
            SELECT s.total_score FROM virtual_challenge_submission_rows s
            WHERE s.event_id = :eid AND s.challenge_id = :cid
              AND (
                s.virtual_mdc_registration_id = {table}.id
                OR s.leader_email_normalized = {table}.email_normalized
              )
            LIMIT 1
        ) AS mdc_submission_score"""
    return f"""(
        SELECT s.total_score FROM virtual_challenge_submission_rows s
        WHERE s.event_id = :eid
          AND (
            s.virtual_mdc_registration_id = {table}.id
            OR s.leader_email_normalized = {table}.email_normalized
          )
        ORDER BY s.total_score DESC NULLS LAST, s.updated_at DESC NULLS LAST, s.id DESC
        LIMIT 1
    ) AS mdc_submission_score"""


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


def _ipcsr_is_legacy_unassigned_pw(
    prompt_war_on: date | datetime | None, session_label: str | None
) -> bool:
    """Sentinel MDC row: 1970-01-01 with no session label (pre–PW session assignment)."""
    if prompt_war_on is None:
        return False
    if isinstance(prompt_war_on, datetime):
        prompt_war_on = prompt_war_on.date()
    if prompt_war_on != IPCSR_LEGACY_PROMPT_WAR_DATE:
        return False
    return not (session_label or "").strip()


def _is_missing_in_person_pw_sessions_table(exc: BaseException) -> bool:
    """True when PostgreSQL reports ``in_person_pw_sessions`` has not been created yet."""
    raw = str(getattr(exc, "orig", exc) or exc).lower()
    return "in_person_pw_sessions" in raw and "does not exist" in raw


def _reject_legacy_prompt_war_on_date(
    prompt_war_on: date | datetime | str | None,
) -> tuple[Response, int] | None:
    """Reject sentinel legacy date for any user-supplied Prompt War session date."""
    if prompt_war_on is None:
        return None
    if isinstance(prompt_war_on, datetime):
        prompt_war_on = prompt_war_on.date()
    if isinstance(prompt_war_on, date):
        iso = prompt_war_on.isoformat()[:10]
    else:
        iso = str(prompt_war_on).strip()[:10]
    if iso == "1970-01-01":
        return jsonify({"error": "1970-01-01 is not a valid session date"}), 400
    return None


def _ipcsr_pw_session_display(*, city: str, prompt_war_on: date, session_label: str) -> str:
    if _ipcsr_is_legacy_unassigned_pw(prompt_war_on, session_label):
        return f"{city} · no PW session date"
    d = prompt_war_on.strftime("%d %b %Y")
    if (session_label or "").strip():
        return f"{city} · {d} · {session_label.strip()}"
    return f"{city} · {d}"


def _pw_session_rsvp_row_key(city: str, prompt_war_on: date | str, session_label: str) -> tuple[str, str, str]:
    """Stable dedupe key for MDC + challenge-submission PW session rows."""
    if isinstance(prompt_war_on, str):
        prompt_war_on = date.fromisoformat(prompt_war_on[:10])
    return (
        city.strip().lower(),
        prompt_war_on.isoformat(),
        (session_label or "").strip(),
    )


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


engine: Engine = create_engine(
    DATABASE_URL,
    future=True,
    poolclass=QueuePool,
    pool_size=int(os.environ.get("DB_POOL_SIZE", "32")),
    max_overflow=int(os.environ.get("DB_POOL_OVERFLOW", "32")),
    pool_pre_ping=True,
    pool_recycle=int(os.environ.get("DB_POOL_RECYCLE", "1800")),
    pool_timeout=int(os.environ.get("DB_POOL_TIMEOUT", "10")),
)

from services.cache import TTLStore  # noqa: E402

_PW_CACHE_HOT = TTLStore(
    maxsize=int(os.environ.get("PW_CACHE_HOT_MAX", "2000")),
    ttl=float(os.environ.get("PW_CACHE_HOT_TTL_SEC", "5")),
)
_PW_CACHE_WARM = TTLStore(
    maxsize=int(os.environ.get("PW_CACHE_WARM_MAX", "500")),
    ttl=float(os.environ.get("PW_CACHE_WARM_TTL_SEC", "60")),
)


def pw_invalidate_read_caches() -> None:
    """Call after imports, credit grants, or challenge mutations."""
    _PW_CACHE_HOT.clear()
    _PW_CACHE_WARM.clear()


import concurrent.futures  # noqa: E402

_IMPORT_THREAD_POOL = concurrent.futures.ThreadPoolExecutor(
    max_workers=max(1, int(os.environ.get("PW_IMPORT_THREAD_POOL", "2"))),
    thread_name_prefix="pw_import",
)


def _pw_shutdown_import_pool() -> None:
    try:
        _IMPORT_THREAD_POOL.shutdown(wait=False, cancel_futures=True)
    except Exception:  # noqa: BLE001
        pass


import atexit  # noqa: E402

atexit.register(_pw_shutdown_import_pool)


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

# Optional Flask-side cap (bytes). Leave unset for no limit at the app layer; reverse proxies
# (nginx client_max_body_size, etc.) may still enforce their own limits — see deploy/nginx_large_uploads.conf.example
try:
    _pw_max_upload_mb = int((os.environ.get("PW_MAX_UPLOAD_MB") or "").strip())
    if _pw_max_upload_mb > 0:
        app.config["MAX_CONTENT_LENGTH"] = _pw_max_upload_mb * 1024 * 1024
except ValueError:
    pass


@app.errorhandler(RequestEntityTooLarge)
def _pw_request_entity_too_large(_e: RequestEntityTooLarge):
    if (request.path or "").startswith("/api/"):
        return jsonify(
            {
                "error": (
                    "Uploaded file exceeds a server-side size limit (Flask MAX_CONTENT_LENGTH / "
                    "Waitress max_request_body_size). Most 413 errors in production are from nginx: "
                    "set client_max_body_size 0; (or a large value) — see deploy/nginx_large_uploads.conf.example"
                ),
            }
        ), 413
    return (
        "Uploaded file too large. If using nginx, set client_max_body_size (see deploy/nginx_large_uploads.conf.example). "
        "Optional Flask cap: PW_MAX_UPLOAD_MB.",
        413,
        {"Content-Type": "text/plain; charset=utf-8"},
    )


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
    {
        "pageId": "overview_submission_analytics",
        "label": "Overview · Submission crossover",
        "path": "/overview/submission-analytics",
    },
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
    ("/admin/import/virtual/challenge-attempts", "overview_settings"),
    ("/admin/import/virtual/main-data-center", "overview_settings"),
    ("/admin/import/in-person/main-data-center", "overview_settings"),
    ("/admin/import/in-person/action-center", "overview_settings"),
    ("/admin/import/in-person/challenge-attempts", "overview_settings"),
    ("/admin/import", "overview_settings"),
    ("/admin", "overview_settings"),
    ("/api/import/latest", "overview_settings"),
    ("/api/credits/grant", "overview_settings"),
    ("/overview/settings", "overview_settings"),
    ("/overview/submission-analytics", "overview_submission_analytics"),
    ("/api/overview/submission-crossover", "overview_submission_analytics"),
    ("/overview/logs", "overview_logs"),
    ("/api/import/virtual/challenge-submissions", "virtual_import"),
    ("/api/import/virtual/main-data-center", "virtual_import"),
    ("/virtual/import", "virtual_import"),
    ("/api/import/in-person/main-data-center", "in_person_import"),
    ("/api/import/in-person/action-center", "in_person_import"),
    ("/api/import/in-person/challenge-attempts/preview", "in_person_import"),
    ("/api/import/in-person/challenge-attempts", "in_person_import"),
    ("/api/import/in-person/rsvp-lists/preview", "in_person_import"),
    ("/api/import/in-person/rsvp-lists", "in_person_import"),
    ("/api/in-person/attendance-cities", "in_person_import"),
    ("/api/in-person/sessions", "in_person_settings"),
    ("/api/in-person/hawkeye/mapping", "in_person_settings"),
    ("/api/in-person/hawkeye/sync", "in_person_settings"),
    ("/api/in-person/hawkeye/fetch", "in_person_settings"),
    ("/api/in-person/hawkeye/events", "in_person_dashboard"),
    ("/api/in-person/hawkeye/stats", "in_person_dashboard"),
    ("/api/in-person/action-center/leaderboard", "in_person_dashboard"),
    ("/api/import/in-person", "in_person_import"),
    ("/in-person/settings", "in_person_settings"),
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
    ("/api/import/virtual/challenge-attempts/preview", "virtual_import"),
    ("/api/import/virtual/challenge-attempts", "virtual_import"),
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
# SQL writes (SELECT/TXN optional via AUDIT_SQL_SELECTS), and every auth event
# is captured into audit.audit_events asynchronously, and DB row triggers
# (database/audit.sql) write field-level diffs into audit.audit_data_changes synchronously.
import audit  # noqa: E402
from audit.decorators import audit_view  # noqa: E402

audit.install(app, engine)

# Response compression (gzip/br for JSON/HTML when client accepts).
from flask_compress import Compress  # noqa: E402

_compress = Compress()
_compress.init_app(app)
app.config["COMPRESS_MIMETYPES"] = [
    "text/html",
    "text/css",
    "application/json",
    "application/javascript",
    "image/svg+xml",
]
app.config["COMPRESS_LEVEL"] = int(os.environ.get("COMPRESS_LEVEL", "6"))
app.config["COMPRESS_MIN_SIZE"] = int(os.environ.get("COMPRESS_MIN_SIZE", "1024"))

if not DEBUG_MODE:
    app.config["TEMPLATES_AUTO_RELOAD"] = False
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = int(
        os.environ.get("SEND_FILE_MAX_AGE_DEFAULT", "31536000")
    )
    app.jinja_env.auto_reload = False
    app.jinja_env.cache_size = int(os.environ.get("JINJA_CACHE_SIZE", "400"))
app.config["JSON_SORT_KEYS"] = False

_slow_log = (os.environ.get("PW_SLOW_REQUEST_LOG") or "").strip()
if _slow_log:
    _slow_handler = RotatingFileHandler(
        _slow_log,
        maxBytes=int(os.environ.get("PW_SLOW_LOG_MAX_BYTES", str(5 * 1024 * 1024))),
        backupCount=int(os.environ.get("PW_SLOW_LOG_BACKUPS", "3")),
        encoding="utf-8",
    )
    _slow_handler.setLevel(logging.WARNING)
    _slow_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    )
    app.logger.addHandler(_slow_handler)
    app.logger.setLevel(logging.INFO)


@app.before_request
def _pw_request_timing_start() -> None:
    g.pw_req_t0 = time.perf_counter()


@app.after_request
def _pw_request_timing_end(response: Response):
    t0 = getattr(g, "pw_req_t0", None)
    if t0 is not None:
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        response.headers["X-Response-Time-ms"] = str(elapsed_ms)
        if elapsed_ms >= int(os.environ.get("PW_SLOW_REQUEST_MS", "500")):
            msg = f"{request.method} {request.path} {elapsed_ms}ms endpoint={request.endpoint!r}"
            app.logger.warning("slow_request %s", msg)
    path = request.path or ""
    if path.startswith("/static/"):
        response.headers.setdefault(
            "Cache-Control",
            "public, max-age=31536000, immutable",
        )
    ep = request.endpoint or ""
    if ep in (
        "api_in_person_mdc_stats",
        "api_virtual_mdc_stats",
        "api_virtual_submission_leaderboard",
        "api_virtual_global_submission_leaderboard",
        "api_import_in_person_challenge_attempts",
        "api_import_virtual_challenge_attempts",
        "api_overview_submission_crossover",
        "credits_distribution",
        "leaderboard",
    ):
        response.headers.setdefault("Cache-Control", "private, max-age=5")
    return response


# Map Flask endpoint → (module id, sub-page key) for sidebar + module selector.
_PW_ENDPOINT_NAV: dict[str, tuple[str, str]] = {
    "main_dashboard": ("overview", "dashboard"),
    "overview_logs": ("overview", "logs"),
    "overview_settings": ("overview", "settings"),
    "overview_submission_analytics": ("overview", "submission_analytics"),
    "api_overview_submission_crossover": ("overview", "submission_analytics"),
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
    "admin_import_in_person_challenge_attempts": ("overview", "settings"),
    "admin_import_virtual_challenge_attempts": ("overview", "settings"),
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
    "api_import_in_person_rsvp_lists_preview": ("in_person", "import"),
    "api_import_in_person_rsvp_lists": ("in_person", "import"),
    "api_in_person_attendance_cities": ("in_person", "import"),
    "api_in_person_action_center_leaderboard": ("in_person", "dashboard"),
    "api_in_person_mdc_stats": ("in_person", "dashboard"),
    "api_virtual_mdc_stats": ("virtual", "dashboard"),
    "api_import_in_person_challenge_attempts_preview": ("in_person", "import"),
    "api_import_in_person_challenge_attempts": ("in_person", "import"),
    "api_import_virtual_challenge_attempts_preview": ("virtual", "import"),
    "api_import_virtual_challenge_attempts": ("virtual", "import"),
    "admin_result": ("overview", "settings"),
}


def _pw_vendor_script_url(filename: str, cdn_fallback: str) -> str:
    """Prefer ``static/vendor/<filename>`` when present (offline / LAN), else CDN."""
    vp = ROOT / "static" / "vendor" / filename
    if vp.is_file():
        return url_for("static", filename=f"vendor/{filename}")
    return cdn_fallback


def _pw_subnav_rows(module: str) -> list[dict[str, str]]:
    if module == "overview":
        spec = (
            ("dashboard", "Dashboard", "main_dashboard", "dashboard"),
            ("submission_analytics", "Crossover", "overview_submission_analytics", "compare_arrows"),
            ("logs", "Logs", "overview_logs", "receipt_long"),
            ("settings", "Settings", "overview_settings", "settings"),
        )
    elif module == "in_person":
        spec = (
            ("dashboard", "Dashboard", "in_person_page", "dashboard"),
            ("leaderboard", "Leaderboard", "in_person_leaderboard", "leaderboard"),
            ("users", "Users", "in_person_users", "group"),
            ("import", "Import", "in_person_import", "upload_file"),
            ("settings", "Settings", "in_person_settings", "tune"),
        )
    else:
        spec = (
            ("dashboard", "Dashboard", "virtual_page", "dashboard"),
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
        "pw_vendor_chart_js": _pw_vendor_script_url(
            "chart.umd.min.js",
            "https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js",
        ),
        "pw_vendor_echarts_js": _pw_vendor_script_url(
            "echarts.min.js",
            "https://cdn.jsdelivr.net/npm/echarts@5.5.1/dist/echarts.min.js",
        ),
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
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "database": "down", "detail": str(exc)}), 503
    out: dict[str, object] = {"ok": True, "database": "up"}
    pool = engine.pool
    try:
        out["db_pool_size"] = pool.size()
        out["db_pool_checked_out"] = pool.checkedout()
    except Exception:  # noqa: BLE001
        pass
    try:
        from audit import get_sink

        sk = get_sink()
        if sk is not None and hasattr(sk, "queue_depth"):
            qd = sk.queue_depth()
            if qd is not None and int(qd) >= 0:
                out["audit_queue_depth"] = int(qd)
    except Exception:  # noqa: BLE001
        pass
    return jsonify(out)


@app.get("/api/overview/submission-crossover")
def api_overview_submission_crossover():
    """Cross-track submission cohort counts (in-person Action Center vs virtual arena), matched by leader email."""
    p = submission_analytics_svc.parse_submission_crossover_params(
        request.args,
        default_ip_event_id=DEFAULT_IN_PERSON_EVENT_ID,
        default_v_event_id=DEFAULT_VIRTUAL_EVENT_ID,
    )
    if p is None:
        return jsonify({"error": "Invalid or missing event identifiers."}), 400
    key = submission_analytics_svc.submission_crossover_cache_key(p)
    data = _PW_CACHE_HOT.get_or_set(
        key,
        lambda: submission_analytics_svc.load_submission_crossover_uncached(engine, p),
    )
    return jsonify(data)


@app.get("/favicon.ico")
def favicon():
    """PW logo (PNG); served at ``/favicon.ico`` for browsers and auth public_paths."""
    return send_from_directory(app.static_folder, "favicon.png", mimetype="image/png")


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
        pw_invalidate_read_caches()
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
_MDC_UPSERT_SQL_VIRTUAL = """
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

_MDC_UPSERT_SQL_IN_PERSON = """
    INSERT INTO {table} (
      event_id, email, form_timestamp, utm_source, utm_medium, utm_campaign, utm_term, utm_content,
      org_name, org_state, org_city, class_stream, portfolio, domain, designation, designation_years_experience,
      founded_info, degree,
      profile_name, full_name, mobile, whatsapp, country, state, city, dob, gender, occupation,
      github_url, linkedin_url, attendance_city, prompt_war_on, session_label
    ) VALUES (
      :event_id, :email, :form_timestamp, :utm_source, :utm_medium, :utm_campaign, :utm_term, :utm_content,
      :org_name, :org_state, :org_city, :class_stream, :portfolio, :domain, :designation, :designation_years_experience,
      :founded_info, :degree,
      :profile_name, :full_name, :mobile, :whatsapp, :country, :state, :city, :dob, :gender, :occupation,
      :github_url, :linkedin_url, :attendance_city, :prompt_war_on, :session_label
    )
    ON CONFLICT ON CONSTRAINT uq_ip_mdc_event_email_pw_session DO UPDATE SET
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
      prompt_war_on = EXCLUDED.prompt_war_on,
      session_label = EXCLUDED.session_label,
      updated_at = now()
    RETURNING (xmax = 0) AS was_insert
    """

_IN_PERSON_MDC_UPSERT = text(_MDC_UPSERT_SQL_IN_PERSON.format(table=TABLE_IN_PERSON_MDC))
_VIRTUAL_MDC_UPSERT = text(_MDC_UPSERT_SQL_VIRTUAL.format(table=TABLE_VIRTUAL_MDC))

_VCSR_UPSERT = text(
    """
    INSERT INTO virtual_challenge_submission_rows (
      event_id, challenge_id, import_job_id, virtual_mdc_registration_id, source_sheet_name,
      team_name, leader_name, leader_email, leader_phone, team_size, attempts_completed, problem_statements,
      total_score, deployed_link, linkedin_post, github_repository_link,
      export_created_at, export_created_by_name, export_created_by_email,
      export_updated_at, export_updated_by_name, export_updated_by_email
    ) VALUES (
      :event_id, :challenge_id, :import_job_id, :virtual_mdc_registration_id, :source_sheet_name,
      :team_name, :leader_name, :leader_email, :leader_phone, :team_size, :attempts_completed, :problem_statements,
      :total_score, :deployed_link, :linkedin_post, :github_repository_link,
      :export_created_at, :export_created_by_name, :export_created_by_email,
      :export_updated_at, :export_updated_by_name, :export_updated_by_email
    )
    ON CONFLICT (challenge_id, leader_email_normalized) DO UPDATE SET
      import_job_id = EXCLUDED.import_job_id,
      virtual_mdc_registration_id = EXCLUDED.virtual_mdc_registration_id,
      source_sheet_name = EXCLUDED.source_sheet_name,
      team_name = EXCLUDED.team_name,
      leader_name = EXCLUDED.leader_name,
      leader_email = EXCLUDED.leader_email,
      leader_phone = EXCLUDED.leader_phone,
      team_size = EXCLUDED.team_size,
      attempts_completed = COALESCE(EXCLUDED.attempts_completed, virtual_challenge_submission_rows.attempts_completed),
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
      event_id, attendance_city, prompt_war_on, session_label, pw_session_id, import_job_id, in_person_mdc_registration_id,
      sheet_kind, source_sheet_name,
      team_name, leader_name, leader_email, leader_phone, team_size, attempts_completed, problem_statements,
      total_score, deployed_link, deployed_changes_notes, github_repository_link,
      export_created_at, export_created_by_name, export_created_by_email,
      export_updated_at, export_updated_by_name, export_updated_by_email
    ) VALUES (
      :event_id, :attendance_city, :prompt_war_on, :session_label, :pw_session_id, :import_job_id, :in_person_mdc_registration_id,
      :sheet_kind, :source_sheet_name,
      :team_name, :leader_name, :leader_email, :leader_phone, :team_size, :attempts_completed, :problem_statements,
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
      pw_session_id = EXCLUDED.pw_session_id,
      source_sheet_name = EXCLUDED.source_sheet_name,
      leader_name = EXCLUDED.leader_name,
      leader_email = EXCLUDED.leader_email,
      leader_phone = EXCLUDED.leader_phone,
      team_size = EXCLUDED.team_size,
      attempts_completed = COALESCE(EXCLUDED.attempts_completed, in_person_challenge_submission_rows.attempts_completed),
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

# Sparse Main Data Center rows created from Action Center import when leader emails are missing.
ACTION_CENTER_AUTO_MDC_UTM_CAMPAIGN = "action_center_auto_registration"


def _form_truthy_auto_create_missing_registrations() -> bool:
    v = (request.form.get("auto_create_missing_registrations") or "").strip().lower()
    return v in ("1", "true", "yes", "on")


def _leader_source_by_normalized_email(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """First occurrence per normalized leader email: casing-preserved email, name, phone."""
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        raw = (r.get("leader_email") or "").strip()
        if not raw:
            continue
        key = raw.lower()
        if key not in out:
            out[key] = {
                "leader_email": raw,
                "leader_name": r.get("leader_name"),
                "leader_phone": r.get("leader_phone"),
            }
    return out


def _synthetic_mdc_row_virtual(
    event_id: int, leader_email: str, leader_name: Any, leader_phone: Any
) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "email": leader_email,
        "form_timestamp": None,
        "utm_source": None,
        "utm_medium": None,
        "utm_campaign": ACTION_CENTER_AUTO_MDC_UTM_CAMPAIGN,
        "utm_term": None,
        "utm_content": None,
        "org_name": None,
        "org_state": None,
        "org_city": None,
        "class_stream": None,
        "portfolio": None,
        "domain": None,
        "designation": None,
        "designation_years_experience": None,
        "founded_info": None,
        "degree": None,
        "profile_name": None,
        "full_name": leader_name,
        "mobile": leader_phone,
        "whatsapp": None,
        "country": None,
        "state": None,
        "city": None,
        "dob": None,
        "gender": None,
        "occupation": None,
        "github_url": None,
        "linkedin_url": None,
        "attendance_city": None,
    }


def _synthetic_mdc_row_in_person(
    event_id: int,
    leader_email: str,
    leader_name: Any,
    leader_phone: Any,
    attendance_city: str,
    prompt_war_on: date,
    session_label: str,
) -> dict[str, Any]:
    d = _synthetic_mdc_row_virtual(event_id, leader_email, leader_name, leader_phone)
    d["attendance_city"] = attendance_city
    d["prompt_war_on"] = prompt_war_on
    d["session_label"] = session_label
    return d


def _ipcsr_mdc_maps_from_mrows(
    mrows: list[Any],
    pw_on: date,
    session_label_imp: str,
) -> tuple[dict[str, int], dict[str, str]]:
    """Pick best in-person MDC registration id per normalized email (same rules as Action Center import)."""
    sl_norm = session_label_imp.strip().lower()

    def _match_priority(pw_on_row: date, sln_row: str) -> int:
        if pw_on_row == pw_on and sln_row == sl_norm:
            return 0
        if pw_on_row == IPCSR_LEGACY_PROMPT_WAR_DATE and not sln_row:
            return 1
        return 2

    per_email: dict[str, dict[str, Any]] = {}
    for r in mrows:
        em = str(r["email_normalized"])
        pwd_cell = r["prompt_war_on"]
        if isinstance(pwd_cell, datetime):
            pwd_cell = pwd_cell.date()
        sln_cell = str(r["sln"] or "")
        cand = {
            "id": int(r["id"]),
            "ac": str(r["ac"]) or "",
            "rank": _match_priority(pwd_cell, sln_cell),
        }
        cur = per_email.get(em)
        if cur is None or cand["rank"] < cur["rank"]:
            per_email[em] = cand
    mdc_by_email = {em: int(v["id"]) for em, v in per_email.items()}
    mdc_ac_by_email = {em: str(v["ac"]) for em, v in per_email.items()}
    return mdc_by_email, mdc_ac_by_email


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

    for r in rows:
        pwo = r.get("prompt_war_on")
        if pwo is None:
            continue
        if isinstance(pwo, datetime):
            pd = pwo.date()
        elif isinstance(pwo, date):
            pd = pwo
        elif isinstance(pwo, str):
            try:
                pd = date.fromisoformat(str(pwo)[:10])
            except ValueError:
                continue
        else:
            continue
        rej = _reject_legacy_prompt_war_on_date(pd)
        if rej:
            mark_archive_status(archived.id, "failed", engine=engine, error="invalid prompt_war_on")
            return rej

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
    pw_invalidate_read_caches()
    payload = {
        "status": "success",
        "rows_created": rows_created,
        "rows_updated": rows_updated,
        "rows_written": rows_written,
        "rows_skipped": int(parse_stats.get("rows_skipped_no_email") or 0),
        "rows_read": int(parse_stats.get("rows_read") or 0),
        "rows_after_dedupe": int(parse_stats.get("rows_after_dedupe") or 0),
        "duplicate_registration_keys_collapsed": int(
            parse_stats.get("duplicate_registration_keys_collapsed") or 0
        ),
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
            batch = []
            for r in rows:
                d = {**r, "event_id": event_id}
                d.pop("prompt_war_on", None)
                d.pop("session_label", None)
                batch.append(d)
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
    pw_invalidate_read_caches()
    payload = {
        "status": "success",
        "rows_created": rows_created,
        "rows_updated": rows_updated,
        "rows_written": rows_written,
        "rows_skipped": int(parse_stats.get("rows_skipped_no_email") or 0),
        "rows_read": int(parse_stats.get("rows_read") or 0),
        "rows_after_dedupe": int(parse_stats.get("rows_after_dedupe") or 0),
        "duplicate_registration_keys_collapsed": int(
            parse_stats.get("duplicate_registration_keys_collapsed") or 0
        ),
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

    auto_create = _form_truthy_auto_create_missing_registrations()
    leader_src = _leader_source_by_normalized_email(rows)

    m_stmt = text(
        f"""
        SELECT id, email_normalized
        FROM {TABLE_VIRTUAL_MDC}
        WHERE event_id = :eid AND email_normalized IN :emails
        """
    ).bindparams(bindparam("emails", expanding=True))

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

        mrows = conn.execute(m_stmt, {"eid": event_id, "emails": emails_set}).fetchall()
        mdc_by_email = {str(r[1]): int(r[0]) for r in mrows}

    missing = [e for e in emails_set if e not in mdc_by_email]
    if missing and not auto_create:
        msg = (
            "Leader email(s) not found in Virtual Main Data Center for this event "
            f"(showing up to 20 of {len(missing)}): {', '.join(missing[:20])}"
        )
        mark_archive_status(archived.id, "parsed", engine=engine, error=msg)
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
                "needs_confirmation": True,
                "nothing_written": True,
                "target_table": "virtual_challenge_submission_rows",
                "virtual_event_id": event_id,
                "parse_stats": parse_for_ui,
                "rows_ready_to_import": len(rows),
                "archive_path": archived.stored_path,
            }
        ), 409

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

            if missing and auto_create:
                for em in missing:
                    src = leader_src[em]
                    conn.execute(
                        _VIRTUAL_MDC_UPSERT,
                        _synthetic_mdc_row_virtual(
                            event_id,
                            str(src["leader_email"]),
                            src.get("leader_name"),
                            src.get("leader_phone"),
                        ),
                    )
                mrows2 = conn.execute(m_stmt, {"eid": event_id, "emails": emails_set}).fetchall()
                mdc_by_email = {str(r[1]): int(r[0]) for r in mrows2}
                still_missing = [e for e in emails_set if e not in mdc_by_email]
                if still_missing:
                    raise ValueError(
                        "Could not link leader email(s) to Main Data Center after auto-create: "
                        + ", ".join(still_missing[:20])
                    )

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
                    "attempts_completed": r.get("attempts_completed"),
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
        pw_invalidate_read_caches()
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
                    "Each database row is keyed by (challenge_id, leader email). "
                    "If the workbook has more than one data row for the same leader email on the same challenge, "
                    "only the last row is kept before upsert. Re-importing updates that row (including team name)."
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


def _challenge_attempt_counts_column_mapping_from_form() -> dict[str, str | None] | None:
    """Parse optional ``column_mapping`` JSON (``email``, ``attempts`` keys). Empty/absent → auto-detect."""
    raw = (request.form.get("column_mapping") or "").strip()
    if not raw:
        return None
    try:
        d = json.loads(raw)
    except json.JSONDecodeError:
        raise ValueError("column_mapping must be valid JSON") from None
    if not isinstance(d, dict):
        raise ValueError("column_mapping must be a JSON object")
    out = {str(k): (None if v in (None, "") else str(v)) for k, v in d.items()}
    return out if out else None


def _preview_challenge_attempt_counts_core(upload_field: str):
    upload = request.files.get(upload_field)
    if not upload or not upload.filename:
        return jsonify({"error": "A .csv or .xlsx file is required"}), 400
    fn = (upload.filename or "").lower()
    if not (fn.endswith(".csv") or fn.endswith(".xlsx")):
        return jsonify({"error": "File must be .csv or .xlsx"}), 400
    raw = upload.read()
    if not raw:
        return jsonify({"error": "File is empty"}), 400
    try:
        payload = vcsr_attempts_sheet_svc.preview_attempts_sheet(raw, upload.filename or "")
    except ValueError as ve:
        return jsonify({"error": str(ve)}), 400
    return jsonify(payload)


def _import_virtual_challenge_attempts_core():
    """CSV/XLSX: Leader Email + Attempts Completed; updates ``attempts_completed`` for one challenge."""
    event_id = request.form.get("virtualEventId", type=int) or request.args.get("virtualEventId", type=int)
    if event_id is None:
        event_id = DEFAULT_VIRTUAL_EVENT_ID
    challenge_id = request.form.get("challenge_id", type=int) or request.args.get("challenge_id", type=int)
    if challenge_id is None or int(challenge_id) < 1:
        return jsonify({"error": "challenge_id is required (arena challenge for this sheet)."}), 400

    upload = request.files.get("virtual_challenge_attempts")
    if not upload or not upload.filename:
        return jsonify({"error": "A .csv or .xlsx file is required (field virtual_challenge_attempts)."}), 400

    fn = (upload.filename or "").lower()
    if not (fn.endswith(".csv") or fn.endswith(".xlsx")):
        return jsonify({"error": "File must be .csv or .xlsx"}), 400

    archived = archive_upload(
        upload,
        engine=engine,
        module="virtual_challenge_attempts",
        source_route=request.path,
        event_id=int(event_id),
    )
    raw = archived.fresh_stream().read()
    try:
        cm = _challenge_attempt_counts_column_mapping_from_form()
    except ValueError as ve:
        mark_archive_status(archived.id, "failed", engine=engine, error=str(ve))
        return jsonify({"error": str(ve), "nothing_written": True, "archive_path": archived.stored_path}), 400
    rows, parse_err = vcsr_attempts_sheet_svc.parse_challenge_attempts_sheet(raw, upload.filename or "", cm)
    if parse_err:
        mark_archive_status(archived.id, "failed", engine=engine, error=parse_err)
        return jsonify({"error": parse_err, "nothing_written": True, "archive_path": archived.stored_path}), 400

    _UPDATE_ATTEMPTS = text(
        """
        UPDATE virtual_challenge_submission_rows
        SET attempts_completed = :ac, updated_at = now()
        WHERE event_id = :eid AND challenge_id = :cid
          AND leader_email_normalized = lower(trim(:em))
        """
    )

    job_id: int | None = None
    try:
        with engine.begin() as conn:
            ch = conn.execute(
                text(
                    """
                    SELECT c.id, c.event_id
                    FROM challenges c
                    JOIN events e ON e.id = c.event_id AND e.kind = 'virtual'
                    WHERE c.id = :cid AND c.event_id = :eid
                    """
                ),
                {"cid": int(challenge_id), "eid": int(event_id)},
            ).mappings().fetchone()
            if not ch:
                raise ValueError("Challenge not found for this virtual event.")

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

            updated = 0
            not_found: list[str] = []
            for row in rows:
                em = (row.get("leader_email") or "").strip()
                ac = int(row["attempts_completed"])
                r2 = conn.execute(
                    _UPDATE_ATTEMPTS,
                    {"ac": ac, "eid": int(event_id), "cid": int(challenge_id), "em": em},
                )
                n = int(r2.rowcount or 0)
                if n:
                    updated += n
                else:
                    not_found.append(em)

            not_found_unique = sorted({e for e in not_found if e})
            rc = {
                "kind": "attempts_completed_patch",
                "rows_in_file": len(rows),
                "rows_updated": updated,
                "leader_emails_not_matched_rows": len(not_found),
                "leader_emails_not_matched_unique": len(not_found_unique),
                "unmatched_leader_emails_sample": not_found_unique[:80],
            }
            conn.execute(
                text(
                    """
                    UPDATE import_jobs
                    SET status = 'success', finished_at = now(), row_counts = CAST(:rc AS jsonb), error_message = NULL
                    WHERE id = :jid
                    """
                ),
                {"rc": json.dumps(rc), "jid": job_id},
            )

        mark_archive_status(archived.id, "success", engine=engine, rows_written=updated)
        pw_invalidate_read_caches()
        return jsonify(
            {
                "status": "success",
                "import_job_id": job_id,
                "rows_written": updated,
                "rows_in_file": len(rows),
                "leader_emails_not_matched": not_found_unique[:500],
                "leader_emails_not_matched_count": len(not_found),
                "leader_emails_not_matched_unique_count": len(not_found_unique),
                "archive_path": archived.stored_path,
                "target_table": "virtual_challenge_submission_rows.attempts_completed",
                "virtual_event_id": int(event_id),
                "challenge_id": int(challenge_id),
                "merge_policy": (
                    "Each sheet row updates teams where leader email matches (normalized) for the selected challenge. "
                    "Unmatched sheet rows are counted in leader_emails_not_matched_count; distinct addresses are in "
                    "leader_emails_not_matched (sorted, capped at 500 for the response)."
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
        return jsonify({"error": str(exc), "nothing_written": True, "archive_path": archived.stored_path}), 500


def _import_in_person_challenge_attempts_core():
    """CSV/XLSX: Leader Email + Attempts Completed; updates ``attempts_completed`` for one PW session + sheet kind."""
    event_id = request.form.get("inPersonEventId", type=int) or request.args.get("inPersonEventId", type=int)
    if event_id is None:
        event_id = DEFAULT_IN_PERSON_EVENT_ID

    raw_sid = (request.form.get("pw_session_id") or "").strip()
    if not raw_sid:
        return jsonify({"error": "pw_session_id is required (select a PW session)."}), 400
    try:
        pw_sid = int(raw_sid)
    except ValueError:
        return jsonify({"error": "pw_session_id must be an integer"}), 400

    sheet_kind = (request.form.get("sheet_kind") or "").strip().lower()
    if sheet_kind not in ("main", "warmup"):
        return jsonify({"error": "sheet_kind must be main or warmup."}), 400

    upload = request.files.get("in_person_challenge_attempts")
    if not upload or not upload.filename:
        return jsonify({"error": "A .csv or .xlsx file is required (field in_person_challenge_attempts)."}), 400

    fn = (upload.filename or "").lower()
    if not (fn.endswith(".csv") or fn.endswith(".xlsx")):
        return jsonify({"error": "File must be .csv or .xlsx"}), 400

    with engine.connect() as conn:
        ev = conn.execute(
            text("SELECT id, kind FROM events WHERE id = :id"),
            {"id": int(event_id)},
        ).fetchone()
        if not ev:
            return jsonify({"error": "event not found"}), 404
        if str(ev[1]) != "in_person":
            return jsonify({"error": "event must be in_person kind"}), 400
        try:
            srow = conn.execute(
                text(
                    f"""
                    SELECT id, city, prompt_war_on, session_label
                    FROM {TABLE_IN_PERSON_PW_SESSIONS}
                    WHERE id = :sid AND event_id = :eid
                    """
                ),
                {"sid": pw_sid, "eid": int(event_id)},
            ).mappings().first()
        except Exception as exc:  # noqa: BLE001
            if _is_missing_in_person_pw_sessions_table(exc):
                return (
                    jsonify(
                        {
                            "error": (
                                "in_person_pw_sessions is missing — apply database/migrate_sessions.sql "
                                "before importing attempt counts."
                            ),
                            "nothing_written": True,
                        }
                    ),
                    400,
                )
            raise
        if not srow:
            return jsonify({"error": "pw_session_id not found for this event"}), 404

    s_pwo = srow["prompt_war_on"]
    if isinstance(s_pwo, datetime):
        s_pwo = s_pwo.date()
    sess_city = str(srow.get("city") or "").strip()
    sess_lab = str(srow.get("session_label") or "")

    archived = archive_upload(
        upload,
        engine=engine,
        module="in_person_challenge_attempts",
        source_route=request.path,
        event_id=int(event_id),
    )
    raw = archived.fresh_stream().read()
    try:
        cm = _challenge_attempt_counts_column_mapping_from_form()
    except ValueError as ve:
        mark_archive_status(archived.id, "failed", engine=engine, error=str(ve))
        return jsonify({"error": str(ve), "nothing_written": True, "archive_path": archived.stored_path}), 400
    rows, parse_err = vcsr_attempts_sheet_svc.parse_challenge_attempts_sheet(raw, upload.filename or "", cm)
    if parse_err:
        mark_archive_status(archived.id, "failed", engine=engine, error=parse_err)
        return jsonify({"error": parse_err, "nothing_written": True, "archive_path": archived.stored_path}), 400

    _UPDATE_ATTEMPTS = text(
        f"""
        UPDATE {TABLE_IN_PERSON_CHALLENGE_SUBMISSIONS}
        SET attempts_completed = :ac, updated_at = now()
        WHERE event_id = :eid
          AND sheet_kind = :sk
          AND leader_email_normalized = lower(trim(:em))
          AND (
            pw_session_id = :sid
            OR (
              pw_session_id IS NULL
              AND attendance_city_normalized = lower(btrim(:city))
              AND prompt_war_on = :pwo
              AND session_label_normalized = lower(btrim(:slab))
            )
          )
        """
    )

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

            updated = 0
            not_found: list[str] = []
            for row in rows:
                em = (row.get("leader_email") or "").strip()
                ac = int(row["attempts_completed"])
                r2 = conn.execute(
                    _UPDATE_ATTEMPTS,
                    {
                        "ac": ac,
                        "eid": int(event_id),
                        "sk": sheet_kind,
                        "em": em,
                        "sid": pw_sid,
                        "city": sess_city,
                        "pwo": s_pwo,
                        "slab": sess_lab,
                    },
                )
                n = int(r2.rowcount or 0)
                if n:
                    updated += n
                else:
                    not_found.append(em)

            not_found_unique = sorted({e for e in not_found if e})
            rc = {
                "kind": "attempts_completed_patch",
                "rows_in_file": len(rows),
                "rows_updated": updated,
                "leader_emails_not_matched_rows": len(not_found),
                "leader_emails_not_matched_unique": len(not_found_unique),
                "unmatched_leader_emails_sample": not_found_unique[:80],
                "pw_session_id": pw_sid,
                "sheet_kind": sheet_kind,
            }
            conn.execute(
                text(
                    """
                    UPDATE import_jobs
                    SET status = 'success', finished_at = now(), row_counts = CAST(:rc AS jsonb), error_message = NULL
                    WHERE id = :jid
                    """
                ),
                {"rc": json.dumps(rc), "jid": job_id},
            )

        mark_archive_status(archived.id, "success", engine=engine, rows_written=updated)
        pw_invalidate_read_caches()
        return jsonify(
            {
                "status": "success",
                "import_job_id": job_id,
                "rows_written": updated,
                "rows_in_file": len(rows),
                "leader_emails_not_matched": not_found_unique[:500],
                "leader_emails_not_matched_count": len(not_found),
                "leader_emails_not_matched_unique_count": len(not_found_unique),
                "archive_path": archived.stored_path,
                "target_table": "in_person_challenge_submission_rows.attempts_completed",
                "in_person_event_id": int(event_id),
                "pw_session_id": pw_sid,
                "sheet_kind": sheet_kind,
                "merge_policy": (
                    "Each sheet row updates submission rows where leader email matches (normalized) for the "
                    "selected PW session and segment (main vs warm-up). Rows linked by pw_session_id are updated "
                    "first; legacy rows without pw_session_id must match the same city, Prompt War date, and "
                    "session label. Unmatched sheet rows are counted in leader_emails_not_matched_count."
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
        return jsonify({"error": str(exc), "nothing_written": True, "archive_path": archived.stored_path}), 500


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
    rej_pw = _reject_legacy_prompt_war_on_date(pw_on)
    if rej_pw:
        return rej_pw
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
                        "attendance_city is not in the in-person registration roster "
                        "(no registrations with that attendance city). Import registrations first or pick another city."
                    ),
                    "nothing_written": True,
                }
            ),
            400,
        )

    pw_sid: int | None = None
    raw_sid = (request.form.get("pw_session_id") or "").strip()
    if raw_sid:
        try:
            pw_sid = int(raw_sid)
        except ValueError:
            return jsonify({"error": "pw_session_id must be an integer"}), 400
        with engine.connect() as conn:
            srow = conn.execute(
                text(
                    f"""
                    SELECT id, city, prompt_war_on, session_label, event_id
                    FROM {TABLE_IN_PERSON_PW_SESSIONS}
                    WHERE id = :sid AND event_id = :eid
                    """
                ),
                {"sid": pw_sid, "eid": event_id},
            ).mappings().first()
        if not srow:
            return jsonify({"error": "pw_session_id not found for this event"}), 404
        s_pwo = srow["prompt_war_on"]
        if isinstance(s_pwo, datetime):
            s_pwo = s_pwo.date()
        if (
            str(srow["city"]).strip().lower() != city_canonical.strip().lower()
            or s_pwo != pw_on
            or str(srow.get("session_label") or "") != session_label_imp
        ):
            return jsonify({"error": "PW session does not match selected city, date, and label"}), 400

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

    auto_create = _form_truthy_auto_create_missing_registrations()
    leader_src = _leader_source_by_normalized_email(rows)

    # MDC link is a participant lookup, not a PW assignment gate. Vision MDC exports do not
    # carry per-PW dates, so most rows live under the legacy date / empty session label.
    # Prefer the row tagged for this exact PW; otherwise legacy; otherwise any row for that email.
    m_stmt = text(
        f"""
        SELECT id, email_normalized, btrim(COALESCE(attendance_city, '')) AS ac,
               prompt_war_on, lower(btrim(COALESCE(session_label, ''))) AS sln
        FROM {TABLE_IN_PERSON_MDC}
        WHERE event_id = :eid
          AND email_normalized IN :emails
        """
    ).bindparams(bindparam("emails", expanding=True))

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

        mrows = conn.execute(
            m_stmt,
            {"eid": event_id, "emails": emails_set},
        ).mappings().all()
        mdc_by_email, mdc_ac_by_email = _ipcsr_mdc_maps_from_mrows(list(mrows), pw_on, session_label_imp)

    missing = [e for e in emails_set if e not in mdc_by_email]
    if missing and not auto_create:
        msg = (
            "Leader email(s) not found in In-person Main Data Center for this event "
            "(any PW assignment) "
            f"(showing up to 20 of {len(missing)}): {', '.join(missing[:20])}"
        )
        mark_archive_status(archived.id, "parsed", engine=engine, error=msg)
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
                "needs_confirmation": True,
                "nothing_written": True,
                "target_table": TABLE_IN_PERSON_CHALLENGE_SUBMISSIONS,
                "in_person_event_id": event_id,
                "parse_stats": parse_for_ui,
                "rows_ready_to_import": len(rows),
                "archive_path": archived.stored_path,
            }
        ), 409

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

            if missing and auto_create:
                for em in missing:
                    src = leader_src[em]
                    conn.execute(
                        _IN_PERSON_MDC_UPSERT,
                        _synthetic_mdc_row_in_person(
                            event_id,
                            str(src["leader_email"]),
                            src.get("leader_name"),
                            src.get("leader_phone"),
                            city_canonical,
                            pw_on,
                            session_label_imp,
                        ),
                    )
                mrows2 = conn.execute(
                    m_stmt,
                    {"eid": event_id, "emails": emails_set},
                ).mappings().all()
                mdc_by_email, mdc_ac_by_email = _ipcsr_mdc_maps_from_mrows(list(mrows2), pw_on, session_label_imp)
                still_missing = [e for e in emails_set if e not in mdc_by_email]
                if still_missing:
                    raise ValueError(
                        "Could not link leader email(s) to Main Data Center after auto-create: "
                        + ", ".join(still_missing[:20])
                    )

            sel_city_norm = city_canonical.strip().lower()
            mismatched = 0
            for r in rows:
                em = (r.get("leader_email") or "").strip().lower()
                ac = (mdc_ac_by_email.get(em) or "").strip().lower()
                if ac and ac != sel_city_norm:
                    mismatched += 1
            parse_stats["mismatched_attendance_city"] = mismatched

            for r in rows:
                email_n = (r.get("leader_email") or "").strip().lower()
                mid = mdc_by_email[email_n]
                params = {
                    "event_id": event_id,
                    "attendance_city": city_canonical,
                    "prompt_war_on": pw_on,
                    "session_label": session_label_imp,
                    "pw_session_id": pw_sid,
                    "import_job_id": job_id,
                    "in_person_mdc_registration_id": mid,
                    "sheet_kind": str(r["sheet_kind"]),
                    "source_sheet_name": r["source_sheet_name"],
                    "team_name": (r.get("team_name") or "").strip(),
                    "leader_name": r.get("leader_name"),
                    "leader_email": (r.get("leader_email") or "").strip(),
                    "leader_phone": r.get("leader_phone"),
                    "team_size": r.get("team_size"),
                    "attempts_completed": r.get("attempts_completed"),
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
        pw_invalidate_read_caches()
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


def _import_in_person_rsvp_lists_preview_core():
    upload = request.files.get("rsvp_list_file")
    if not upload or not upload.filename:
        return jsonify({"error": "A file is required (field rsvp_list_file)"}), 400
    fn = (upload.filename or "").lower()
    if not (fn.endswith(".csv") or fn.endswith(".xlsx") or fn.endswith(".xls")):
        return jsonify({"error": "File must be .csv, .xlsx, or .xls"}), 400
    raw = upload.read()
    if not raw:
        return jsonify({"error": "File is empty"}), 400
    try:
        payload = ip_rsvp_list_svc.preview_file(raw, upload.filename or "")
    except ValueError as ve:
        return jsonify({"error": str(ve)}), 400
    return jsonify(payload)


def _import_in_person_rsvp_lists_core():
    event_id = DEFAULT_IN_PERSON_EVENT_ID
    upload = request.files.get("rsvp_list_file")
    if not upload or not upload.filename:
        return jsonify({"error": "A CSV or XLSX file is required (field rsvp_list_file)"}), 400

    raw_sid = (request.form.get("pw_session_id") or "").strip()
    if not raw_sid:
        return jsonify({"error": "pw_session_id is required"}), 400
    try:
        pw_sid = int(raw_sid)
    except ValueError:
        return jsonify({"error": "pw_session_id must be an integer"}), 400

    list_kind = (request.form.get("list_kind") or "").strip()
    if list_kind not in ip_rsvp_list_svc.LIST_KINDS:
        return jsonify({"error": "list_kind must be invite_sent or accepted"}), 400

    mapping_raw = (request.form.get("column_mapping") or "").strip() or "{}"
    try:
        column_mapping = json.loads(mapping_raw)
    except json.JSONDecodeError:
        return jsonify({"error": "column_mapping must be valid JSON"}), 400
    if not isinstance(column_mapping, dict):
        return jsonify({"error": "column_mapping must be a JSON object"}), 400
    column_mapping = {str(k): (None if v in (None, "") else str(v)) for k, v in column_mapping.items()}

    fn = (upload.filename or "").lower()
    if not (fn.endswith(".csv") or fn.endswith(".xlsx") or fn.endswith(".xls")):
        return jsonify({"error": "File must be .csv, .xlsx, or .xls"}), 400

    with engine.connect() as conn:
        srow = conn.execute(
            text(
                f"""
                SELECT id, event_id FROM {TABLE_IN_PERSON_PW_SESSIONS}
                WHERE id = :sid AND event_id = :eid
                """
            ),
            {"sid": pw_sid, "eid": event_id},
        ).mappings().first()
    if not srow:
        return jsonify({"error": "pw_session_id not found for this event"}), 404

    archived = archive_upload(
        upload,
        engine=engine,
        module="in_person_rsvp_lists",
        source_route=request.path,
        event_id=event_id,
    )

    try:
        raw_bytes = archived.fresh_stream().read()
        emails, stats = ip_rsvp_list_svc.parse_emails_with_mapping(
            raw_bytes, upload.filename or "", column_mapping
        )
    except ValueError as ve:
        mark_archive_status(archived.id, "failed", engine=engine, error=str(ve))
        return jsonify({"error": str(ve)}), 400

    if not emails:
        mark_archive_status(archived.id, "failed", engine=engine, error="No valid emails after parsing")
        return jsonify({"error": "No valid emails after parsing", "parse_stats": stats}), 400

    mark_archive_status(archived.id, "parsed", engine=engine)

    job_id: int | None = None
    try:
        with engine.begin() as conn:
            res = conn.execute(
                text(
                    """
                    INSERT INTO import_jobs (module, status, started_at, row_counts)
                    VALUES ('in_person_rsvp_lists', 'running', now(), '{}'::jsonb)
                    RETURNING id
                    """
                ),
            )
            job_id = int(res.scalar_one())
            mark_archive_status(archived.id, "parsed", engine=engine, import_job_id=job_id)

            ev = conn.execute(
                text("SELECT id, kind FROM events WHERE id = :id"),
                {"id": event_id},
            ).fetchone()
            if not ev:
                raise ValueError("event not found")
            if str(ev[1]) != "in_person":
                raise ValueError("event must be in_person kind")

            conn.execute(
                text(
                    f"""
                    DELETE FROM {TABLE_IN_PERSON_RSVP_LIST_EMAILS}
                    WHERE pw_session_id = :sid AND list_kind = :kind
                    """
                ),
                {"sid": pw_sid, "kind": list_kind},
            )

            ins = text(
                f"""
                INSERT INTO {TABLE_IN_PERSON_RSVP_LIST_EMAILS}
                  (event_id, pw_session_id, list_kind, email_normalized, import_job_id)
                VALUES (:eid, :sid, :kind, :em, :jid)
                """
            )
            for em in emails:
                conn.execute(
                    ins,
                    {"eid": event_id, "sid": pw_sid, "kind": list_kind, "em": em, "jid": job_id},
                )

            counts = {**stats, "emails_written": len(emails)}
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

        mark_archive_status(archived.id, "success", engine=engine, rows_written=len(emails))
        pw_invalidate_read_caches()
        return jsonify(
            {
                "status": "success",
                "import_job_id": job_id,
                "parse_stats": {**stats, "emails_written": len(emails)},
                "archive_path": archived.stored_path,
                "in_person_event_id": event_id,
                "pw_session_id": pw_sid,
                "list_kind": list_kind,
            }
        )
    except ProgrammingError as pe:
        mark_archive_status(archived.id, "failed", engine=engine, error=str(pe))
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
                    {"msg": str(pe), "jid": job_id},
                )
        return jsonify(
            {
                "error": (
                    f"{pe} Apply database/migrate_in_person_rsvp_list_imports.sql if the RSVP list table is missing."
                ),
                "nothing_written": True,
            }
        ), 500
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
        return jsonify({"error": str(exc), "parse_stats": stats}), 500


@app.post("/api/import/in-person/main-data-center")
def api_import_in_person_main_data_center():
    return _import_in_person_main_data_center_core()


@app.post("/api/import/in-person/action-center")
def api_import_in_person_action_center():
    return _import_in_person_action_center_core()


@app.post("/api/import/in-person/rsvp-lists/preview")
def api_import_in_person_rsvp_lists_preview():
    return _import_in_person_rsvp_lists_preview_core()


@app.post("/api/import/in-person/rsvp-lists")
def api_import_in_person_rsvp_lists():
    return _import_in_person_rsvp_lists_core()


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
    if pw_d is not None:
        rej_lb = _reject_legacy_prompt_war_on_date(pw_d)
        if rej_lb:
            return rej_lb
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


def _arena_participation_counts_for_email_normalized(
    conn,
    *,
    in_person_event_id: int,
    virtual_event_id: int,
    email_normalized: str | None,
) -> dict[str, int]:
    """Distinct Prompt War sessions (in-person) and arena challenges (virtual) for one email."""
    en = (str(email_normalized).strip() if email_normalized else "") or ""
    if not en:
        return {
            "arena_in_person_pw_sessions_count": 0,
            "arena_virtual_challenges_count": 0,
        }
    ip_n = conn.execute(
        text(
            f"""
            SELECT COUNT(*)::int FROM (
              SELECT DISTINCT m.pw_session_id AS sid
              FROM {TABLE_IN_PERSON_MDC} m
              WHERE m.event_id = :ipeid
                AND m.email_normalized = :en
                AND m.pw_session_id IS NOT NULL
              UNION
              SELECT DISTINCT c.pw_session_id
              FROM {TABLE_IN_PERSON_CHALLENGE_SUBMISSIONS} c
              INNER JOIN {TABLE_IN_PERSON_MDC} m
                ON m.id = c.in_person_mdc_registration_id AND m.event_id = c.event_id
              WHERE c.event_id = :ipeid
                AND m.email_normalized = :en
                AND c.pw_session_id IS NOT NULL
            ) t
            """
        ),
        {"ipeid": in_person_event_id, "en": en},
    ).scalar_one()
    v_n = conn.execute(
        text(
            f"""
            SELECT COUNT(DISTINCT s.challenge_id)::int
            FROM virtual_challenge_submission_rows s
            WHERE s.event_id = :veid
              AND s.challenge_id IS NOT NULL
              AND (
                s.leader_email_normalized = :en
                OR s.virtual_mdc_registration_id IN (
                  SELECT id FROM {TABLE_VIRTUAL_MDC}
                  WHERE event_id = :veid AND email_normalized = :en
                )
              )
            """
        ),
        {"veid": virtual_event_id, "en": en},
    ).scalar_one()
    return {
        "arena_in_person_pw_sessions_count": int(ip_n or 0),
        "arena_virtual_challenges_count": int(v_n or 0),
    }


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
                    SELECT id, event_id, email, email_normalized, form_timestamp, utm_source, utm_medium, utm_campaign,
                           utm_term, utm_content, org_name, org_state, org_city, class_stream, portfolio,
                           domain, designation, designation_years_experience, founded_info, degree, profile_name, full_name, mobile,
                           whatsapp, country, state, city, dob, gender, occupation, github_url,
                           linkedin_url, attendance_city, prompt_war_on, session_label, pw_session_id,
                           created_at, updated_at
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
    row_d = dict(row)
    base = _serialize_mdc_row_json(row_d)
    psid = row_d.get("pw_session_id")
    if psid is not None:
        try:
            with engine.connect() as conn:
                dn = conn.execute(
                    text(
                        f"""
                        SELECT display_name
                        FROM {TABLE_IN_PERSON_PW_SESSIONS}
                        WHERE id = :sid AND event_id = :eid
                        """
                    ),
                    {"sid": int(psid), "eid": eid},
                ).scalar_one_or_none()
            if dn:
                base["pw_session_display"] = str(dn)
        except Exception:  # noqa: BLE001
            pass
    try:
        with engine.connect() as conn:
            subs = conn.execute(
                text(
                    f"""
                    SELECT c.id, c.sheet_kind, c.attendance_city, c.prompt_war_on, c.session_label,
                           c.team_name, c.total_score, c.deployed_link,
                           c.github_repository_link, c.source_sheet_name, c.export_created_at,
                           s.display_name AS pw_session_display_name
                    FROM {TABLE_IN_PERSON_CHALLENGE_SUBMISSIONS} c
                    INNER JOIN {TABLE_IN_PERSON_PW_SESSIONS} s
                      ON s.id = c.pw_session_id
                     AND s.event_id = c.event_id
                    WHERE c.event_id = :eid
                      AND c.in_person_mdc_registration_id = :rid
                    ORDER BY c.sheet_kind ASC, c.team_name ASC, c.prompt_war_on ASC, c.id ASC
                    """
                ),
                {"eid": eid, "rid": reg_id},
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
            dn = (d.pop("pw_session_display_name", None) or "").strip()
            pwo = d.get("prompt_war_on")
            if isinstance(pwo, datetime):
                pwo = pwo.date()
            city = d.get("attendance_city") or ""
            sl = d.get("session_label") or ""
            if isinstance(pwo, date):
                d["prompt_war_on_iso"] = pwo.isoformat()
                d["pw_session_display"] = dn or _ipcsr_pw_session_display(
                    city=city, prompt_war_on=pwo, session_label=sl
                )
            else:
                d["prompt_war_on_iso"] = None
                d["pw_session_display"] = dn or (city or "—")
            d.pop("prompt_war_on", None)
            team_submissions.append(d)
        base["team_submissions"] = team_submissions
    except Exception as exc:  # noqa: BLE001
        base["team_submissions"] = []
        raw = str(getattr(exc, "orig", exc) or exc).lower()
        if _is_missing_in_person_pw_sessions_table(exc) or "pw_session_id" in raw:
            pass
        else:
            base["team_submissions_error"] = str(exc)
    try:
        with engine.connect() as conn:
            sess_rows = conn.execute(
                text(
                    f"""
                    SELECT id AS registration_id, attendance_city, prompt_war_on, session_label, form_timestamp
                    FROM {TABLE_IN_PERSON_MDC}
                    WHERE event_id = :eid
                      AND email_normalized = (
                        SELECT email_normalized FROM {TABLE_IN_PERSON_MDC}
                        WHERE id = :rid AND event_id = :eid
                      )
                    ORDER BY prompt_war_on DESC NULLS LAST, id DESC
                    """
                ),
                {"eid": eid, "rid": reg_id},
            ).mappings().all()
        parsed_rows: list[tuple[dict, date | None]] = []
        for sr in sess_rows:
            rd = dict(sr)
            pwo = rd.get("prompt_war_on")
            if isinstance(pwo, datetime):
                pwo = pwo.date()
            elif not isinstance(pwo, date):
                pwo = None
            parsed_rows.append((rd, pwo))
        has_non_legacy = any(
            p is not None and not _ipcsr_is_legacy_unassigned_pw(p, rd.get("session_label"))
            for rd, p in parsed_rows
        )
        participated_pw: list[dict] = []
        for rd, pwo in parsed_rows:
            rid = int(rd["registration_id"])
            city = str(rd.get("attendance_city") or "").strip()
            sl = str(rd.get("session_label") or "")
            fts = rd.get("form_timestamp")
            fts_out = _format_dt_display(fts) if fts is not None else None
            if (
                has_non_legacy
                and pwo is not None
                and _ipcsr_is_legacy_unassigned_pw(pwo, sl)
                and rid != int(reg_id)
            ):
                continue
            if isinstance(pwo, date):
                city_disp = city or "(Unknown)"
                participated_pw.append(
                    {
                        "registration_id": rid,
                        "attendance_city": rd.get("attendance_city"),
                        "session_label": sl,
                        "prompt_war_on_iso": pwo.isoformat(),
                        "pw_session_display": _ipcsr_pw_session_display(
                            city=city_disp, prompt_war_on=pwo, session_label=sl
                        ),
                        "form_timestamp": fts_out,
                        "is_current": rid == int(reg_id),
                    }
                )
            else:
                participated_pw.append(
                    {
                        "registration_id": rid,
                        "attendance_city": rd.get("attendance_city"),
                        "session_label": sl,
                        "prompt_war_on_iso": None,
                        "pw_session_display": "—",
                        "form_timestamp": fts_out,
                        "is_current": rid == int(reg_id),
                    }
                )
        base["participated_pw_sessions"] = participated_pw
    except Exception as exc:  # noqa: BLE001
        base["participated_pw_sessions"] = []
        base["participated_pw_sessions_error"] = str(exc)
    try:
        with engine.connect() as conn:
            base["audit_log"] = _load_mdc_registration_audit_timeline(
                conn,
                table_name=TABLE_IN_PERSON_MDC,
                reg_id=reg_id,
                entity_name="in_person_main_data_center_registrations",
                module="in_person",
            )
    except Exception as exc:  # noqa: BLE001
        base["audit_log"] = {
            "available": True,
            "rows": [],
            "message": f"Could not load audit history: {exc}",
        }
    base["hawkeye_rsvp"] = _build_hawkeye_rsvp_for_registration(
        engine, eid, reg_id=int(reg_id), row_d=row_d, base=base
    )
    try:
        with engine.connect() as conn:
            base.update(
                _arena_participation_counts_for_email_normalized(
                    conn,
                    in_person_event_id=eid,
                    virtual_event_id=DEFAULT_VIRTUAL_EVENT_ID,
                    email_normalized=row_d.get("email_normalized"),
                )
            )
    except Exception as exc:  # noqa: BLE001
        base["arena_participation_counts_error"] = str(exc)
        base["arena_in_person_pw_sessions_count"] = None
        base["arena_virtual_challenges_count"] = None
    return jsonify(base)


def _build_hawkeye_rsvp_for_registration(
    eng: Engine, eid: int, *, reg_id: int, row_d: dict, base: dict
) -> dict:
    """Hawkeye RSVP block for the user-detail view; resolves all PW sessions linked to this email."""
    email = row_d.get("email")
    candidates: list[dict] = []
    seen_psids: set[int] = set()
    try:
        with eng.connect() as conn:
            rows = conn.execute(
                text(
                    f"""
                    SELECT DISTINCT m.pw_session_id, s.display_name, s.city, s.prompt_war_on, s.session_label
                    FROM {TABLE_IN_PERSON_MDC} m
                    JOIN {TABLE_IN_PERSON_PW_SESSIONS} s
                      ON s.id = m.pw_session_id AND s.event_id = m.event_id
                    WHERE m.event_id = :eid
                      AND m.email_normalized = (
                        SELECT email_normalized FROM {TABLE_IN_PERSON_MDC}
                        WHERE id = :rid AND event_id = :eid
                      )
                      AND m.pw_session_id IS NOT NULL
                    UNION
                    SELECT DISTINCT c.pw_session_id, s.display_name, s.city, s.prompt_war_on, s.session_label
                    FROM {TABLE_IN_PERSON_CHALLENGE_SUBMISSIONS} c
                    JOIN {TABLE_IN_PERSON_PW_SESSIONS} s
                      ON s.id = c.pw_session_id AND s.event_id = c.event_id
                    WHERE c.event_id = :eid
                      AND c.in_person_mdc_registration_id = :rid
                      AND c.pw_session_id IS NOT NULL
                    """
                ),
                {"eid": eid, "rid": reg_id},
            ).mappings().all()
        for r in rows:
            psid = r.get("pw_session_id")
            if psid is None or int(psid) in seen_psids:
                continue
            seen_psids.add(int(psid))
            pwo = r.get("prompt_war_on")
            if isinstance(pwo, datetime):
                pwo = pwo.date()
            disp = (r.get("display_name") or "").strip()
            if not disp and isinstance(pwo, date):
                disp = _ipcsr_pw_session_display(
                    city=str(r.get("city") or ""),
                    prompt_war_on=pwo,
                    session_label=str(r.get("session_label") or ""),
                )
            candidates.append(
                {
                    "pw_session_id": int(psid),
                    "pw_session_display": disp or "—",
                    "city": str(r.get("city") or ""),
                    "prompt_war_on_iso": pwo.isoformat() if isinstance(pwo, date) else None,
                    "session_label": str(r.get("session_label") or ""),
                }
            )
    except Exception as exc:  # noqa: BLE001
        raw = str(getattr(exc, "orig", exc) or exc).lower()
        if not (_is_missing_in_person_pw_sessions_table(exc) or "pw_session_id" in raw):
            return {
                "applicable": False,
                "error": str(exc),
                "summary": f"Hawkeye RSVP lookup failed: {exc}",
                "sessions": [],
            }

    if not candidates:
        pwo_raw = row_d.get("prompt_war_on")
        pwo_curr: date | None = None
        if isinstance(pwo_raw, datetime):
            pwo_curr = pwo_raw.date()
        elif isinstance(pwo_raw, date):
            pwo_curr = pwo_raw
        if pwo_raw is None:
            return {
                "applicable": False,
                "summary": "No Prompt War date on this row; Hawkeye RSVP is shown per PW session.",
                "sessions": [],
            }
        if pwo_curr is not None and _ipcsr_is_legacy_unassigned_pw(pwo_curr, row_d.get("session_label")):
            return {
                "applicable": False,
                "pw_session_display": base.get("pw_session_display"),
                "summary": (
                    "This row has no Prompt War session date yet, and no other registration / submission "
                    "for this email is linked to a PW session. Link a PW session in In-person settings to use "
                    "Hawkeye RSVP."
                ),
                "sessions": [],
            }
        scope_key = hawkeye_service.make_pw_session_scope_key(
            str(row_d.get("attendance_city") or ""), pwo_raw, row_d.get("session_label")
        )
        sess = _build_hawkeye_session_block(
            eng,
            eid,
            scope_key=scope_key,
            email=email,
            pw_session_display=base.get("pw_session_display") or "",
            pw_session_id=None,
        )
        return {"applicable": True, "registration_email": email, "sessions": [sess]}

    sessions_out: list[dict] = []
    for c in candidates:
        scope_key = hawkeye_service.make_pw_session_scope_key(
            c.get("city") or "",
            c.get("prompt_war_on_iso") or "",
            c.get("session_label") or "",
        )
        sess = _build_hawkeye_session_block(
            eng,
            eid,
            scope_key=scope_key,
            email=email,
            pw_session_display=c.get("pw_session_display") or "",
            pw_session_id=c.get("pw_session_id"),
        )
        sessions_out.append(sess)
    return {"applicable": True, "registration_email": email, "sessions": sessions_out}


def _build_hawkeye_session_block(
    eng: Engine, eid: int, *, scope_key: str, email: Any, pw_session_display: str,
    pw_session_id: int | None,
) -> dict:
    sess: dict = {
        "scope_key": scope_key,
        "pw_session_display": pw_session_display,
        "pw_session_id": pw_session_id,
    }
    try:
        snap = hawkeye_service.get_latest_snapshot_stats_emails(
            eng, eid, scope_key, pw_session_id=pw_session_id
        )
    except Exception as exc:  # noqa: BLE001
        sess["error"] = str(exc)
        sess["summary"] = f"Hawkeye RSVP lookup failed: {exc}"
        return sess
    if snap is None:
        sess["summary"] = (
            "No Hawkeye snapshot for this PW session yet. Map the Hawkeye event tag "
            "and fetch stats in In-person settings."
        )
        sess["snapshot_has_emails"] = False
        sess["email_matched"] = False
        sess["bucket"] = None
        return sess
    sess["hawkeye_event_name"] = snap.get("hawkeye_event_name")
    sess["fetched_at"] = snap.get("fetched_at")
    sess.update(
        hawkeye_service.summarize_hawkeye_rsvp_for_email(snap.get("stats_emails"), email)
    )
    return sess


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
                    SELECT id, event_id, email, email_normalized, form_timestamp, utm_source, utm_medium, utm_campaign,
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
    row_d = dict(row)
    base = _serialize_mdc_row_json(row_d)
    try:
        with engine.connect() as conn:
            ch_rows = conn.execute(
                text(
                    """
                    SELECT DISTINCT c.id AS challenge_id, c.title, c.slug, c.status, c.import_sheet_suffix,
                           c.opens_at, c.closes_at, c.created_at, c.updated_at
                    FROM virtual_challenge_submission_rows s
                    JOIN challenges c ON c.id = s.challenge_id AND c.event_id = s.event_id
                    WHERE s.event_id = :eid
                      AND (
                        s.virtual_mdc_registration_id = :rid
                        OR s.leader_email_normalized = (
                          SELECT email_normalized FROM virtual_main_data_center_registrations
                          WHERE id = :rid AND event_id = :eid
                        )
                      )
                    ORDER BY c.closes_at NULLS LAST, c.id ASC
                    """
                ),
                {"eid": eid, "rid": reg_id},
            ).mappings().all()
        participated: list[dict] = []
        for cr in ch_rows:
            d = dict(cr)
            for k in ("opens_at", "closes_at", "created_at", "updated_at"):
                ts = d.get(k)
                if ts is not None and hasattr(ts, "isoformat"):
                    d[k] = ts.isoformat()
                elif ts is not None:
                    d[k] = str(ts)
            participated.append(d)
        base["participated_challenges"] = participated
    except Exception as exc:  # noqa: BLE001
        base["participated_challenges"] = []
        base["participated_challenges_error"] = str(exc)
    try:
        with engine.connect() as conn:
            base.update(
                _arena_participation_counts_for_email_normalized(
                    conn,
                    in_person_event_id=DEFAULT_IN_PERSON_EVENT_ID,
                    virtual_event_id=eid,
                    email_normalized=row_d.get("email_normalized"),
                )
            )
    except Exception as exc:  # noqa: BLE001
        base["arena_participation_counts_error"] = str(exc)
        base["arena_in_person_pw_sessions_count"] = None
        base["arena_virtual_challenges_count"] = None
    try:
        with engine.connect() as conn:
            base["audit_log"] = _load_mdc_registration_audit_timeline(
                conn,
                table_name=TABLE_VIRTUAL_MDC,
                reg_id=reg_id,
                entity_name="virtual_main_data_center_registrations",
                module="virtual",
            )
    except Exception as exc:  # noqa: BLE001
        base["audit_log"] = {
            "available": True,
            "rows": [],
            "message": f"Could not load audit history: {exc}",
        }
    return jsonify(base)


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

        pw_invalidate_read_caches()
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


def _load_mdc_brief_uncached(event_id: int, *, mode: str, conn: Connection | None = None) -> dict:
    """Compact Main Data Center stats for the Overview dashboard.

    Keeps query count low (~5) per module so the overview stays cheap.
    Pass ``conn`` to reuse an open connection (e.g. overview dashboard).
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

    def _fill(c: Connection) -> None:
        out["total"] = int(
            c.execute(
                text(f"SELECT COUNT(*) FROM {table} WHERE event_id = :e"),
                {"e": event_id},
            ).scalar()
            or 0
        )
        out["last7"] = int(
            c.execute(
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
        row = c.execute(
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
        row = c.execute(
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
        avg = c.execute(
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

    try:
        if conn is not None:
            _fill(conn)
        else:
            with engine.connect() as c:
                _fill(c)
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)
    return out


def _load_mdc_brief(event_id: int, *, mode: str, conn: Connection | None = None) -> dict:
    if conn is not None:
        return _load_mdc_brief_uncached(event_id, mode=mode, conn=conn)
    key = ("mdc_brief", str(mode), int(event_id))
    return _PW_CACHE_HOT.get_or_set(
        key, lambda: _load_mdc_brief_uncached(event_id, mode=mode, conn=None)
    )


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


def _fetch_overview_stats_uncached(
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
            mdc_ip = _load_mdc_brief(in_person_event_id, mode="in_person", conn=conn)
            mdc_v = _load_mdc_brief(virtual_event_id, mode="virtual", conn=conn)
            ip_ac_global = _in_person_submission_leaderboard(
                in_person_event_id, None, 10, conn=conn
            )
            v_ac_global = _virtual_global_submission_leaderboard(
                event_id=virtual_event_id, limit=10, conn=conn
            )
            ip_ac_cities = _in_person_pw_options(in_person_event_id, conn=conn)
            if resolved_cid is not None:
                v_arena_top3 = _submission_leaderboard_payload(
                    event_id=int(virtual_event_id),
                    challenge_id=int(resolved_cid),
                    limit=10,
                    offset=0,
                    conn=conn,
                )
            else:
                v_arena_top3 = {"rows": [], "total": 0, "error": None, "challenge": None}
        total_reg = rsvp_d + reg_v
        conv = (100.0 * sub_d / rsvp_d) if rsvp_d else 0.0
        mdc_total = (mdc_ip.get("total") or 0) + (mdc_v.get("total") or 0)
        mdc_last7 = (mdc_ip.get("last7") or 0) + (mdc_v.get("last7") or 0)
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


def _fetch_overview_stats(
    in_person_event_id: int,
    virtual_event_id: int,
    *,
    arena_challenge_id: int | None = None,
) -> dict:
    key = (
        "overview",
        int(in_person_event_id),
        int(virtual_event_id),
        int(arena_challenge_id) if arena_challenge_id is not None else None,
    )
    return _PW_CACHE_HOT.get_or_set(
        key,
        lambda: _fetch_overview_stats_uncached(
            in_person_event_id,
            virtual_event_id,
            arena_challenge_id=arena_challenge_id,
        ),
    )


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


def _audit_sanitize_for_json(
    obj: object,
    *,
    depth: int = 0,
    max_depth: int = 10,
    max_str: int = 400,
    max_dict_items: int = 80,
    max_list_items: int = 120,
) -> object:
    """Make nested audit payloads JSON-safe (truncate long strings, cap depth/size)."""
    if depth > max_depth:
        return "…"
    if obj is None:
        return None
    if isinstance(obj, datetime):
        v = obj if obj.tzinfo else obj.replace(tzinfo=timezone.utc)
        return v.astimezone(timezone.utc).isoformat()
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, dict):
        out: dict[str, object] = {}
        for i, (k, v) in enumerate(obj.items()):
            if i >= max_dict_items:
                out["_truncated"] = True
                break
            out[str(k)] = _audit_sanitize_for_json(
                v, depth=depth + 1, max_depth=max_depth, max_str=max_str
            )
        return out
    if isinstance(obj, (list, tuple)):
        return [
            _audit_sanitize_for_json(x, depth=depth + 1, max_depth=max_depth, max_str=max_str)
            for x in obj[:max_list_items]
        ]
    if isinstance(obj, str):
        if len(obj) > max_str:
            return obj[:max_str] + "…"
        return obj
    if isinstance(obj, (int, float, bool)):
        return obj
    return str(obj)[:max_str]


def _audit_jsonb_as_dict(value: object) -> dict | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            out = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return out if isinstance(out, dict) else None
    return None


def _load_mdc_registration_audit_timeline(
    conn,
    *,
    table_name: str,
    reg_id: int,
    entity_name: str,
    module: str,
    data_limit: int = 200,
    event_limit: int = 100,
    combined_max: int = 250,
) -> dict[str, object]:
    """Row-level audit + VIEW events for one MDC registration (Master Audit Log)."""
    if not _audit_logs_schema_ok(conn):
        return {
            "available": False,
            "rows": [],
            "message": "Audit tables are not present. Apply database/audit.sql to enable history.",
        }
    pk = json.dumps({"id": int(reg_id)})
    rows_merge: list[tuple[datetime, dict[str, object]]] = []
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)

    def _coerce_ts(v: object) -> datetime:
        if isinstance(v, datetime):
            if v.tzinfo is None:
                return v.replace(tzinfo=timezone.utc)
            return v.astimezone(timezone.utc)
        return epoch

    try:
        drows = conn.execute(
            text(
                """
                SELECT occurred_at, op, changed_columns, changes, new_row,
                       principal, principal_email, request_id, source, statement_tag
                FROM audit.audit_data_changes
                WHERE schema_name = 'public'
                  AND table_name = :tbl
                  AND record_pk @> CAST(:pk AS jsonb)
                ORDER BY occurred_at ASC
                LIMIT :lim
                """
            ),
            {"tbl": table_name, "pk": pk, "lim": data_limit},
        ).mappings().all()
        for r in drows:
            oc = r["occurred_at"]
            t = _coerce_ts(oc)
            cols_raw = r.get("changed_columns")
            if isinstance(cols_raw, list):
                cols_list = [str(c) for c in cols_raw]
            elif cols_raw is None:
                cols_list = []
            else:
                cols_list = [str(cols_raw)]
            ch_d = _audit_jsonb_as_dict(r.get("changes"))
            op = (r.get("op") or "").strip()
            if op == "INSERT":
                summary = "Registration row inserted"
            elif op == "UPDATE":
                summary = "Updated: " + (", ".join(cols_list) if cols_list else "(no column list)")
            elif op == "DELETE":
                summary = "Registration row deleted"
            else:
                summary = op or "Data change"
            entry: dict[str, object] = {
                "kind": "data",
                "occurred_at": t.isoformat(),
                "occurred_at_fmt": _fmt_audit_ts(oc if isinstance(oc, datetime) else None),
                "op": op,
                "summary": summary[:500],
                "principal": r.get("principal") or "",
                "principal_email": r.get("principal_email") or "",
                "request_id": r.get("request_id") or "",
                "source": r.get("source") or "",
                "statement_tag": r.get("statement_tag") or "",
                "changed_columns": cols_list,
                "changes": _audit_sanitize_for_json(ch_d) if ch_d else None,
            }
            nr = _audit_jsonb_as_dict(r.get("new_row"))
            if op == "INSERT" and nr:
                entry["new_row_columns"] = [str(k) for k in list(nr.keys())[:40]]
            rows_merge.append((t, entry))

        evrows = conn.execute(
            text(
                """
                SELECT occurred_at, action, endpoint, module, entity,
                       principal, principal_email, request_id, source, extra
                FROM audit.audit_events
                WHERE record_pk @> CAST(:pk AS jsonb)
                  AND action = 'VIEW'
                  AND COALESCE(entity, '') = :ent
                  AND COALESCE(module, '') = :mod
                ORDER BY occurred_at ASC
                LIMIT :lim
                """
            ),
            {"pk": pk, "ent": entity_name, "mod": module, "lim": event_limit},
        ).mappings().all()
        for r in evrows:
            oc = r["occurred_at"]
            t = _coerce_ts(oc)
            ex_d = _audit_jsonb_as_dict(r.get("extra"))
            ep = r.get("endpoint") or ""
            summary = "Detail viewed" + (f" — {ep}" if ep else "")
            entry = {
                "kind": "event",
                "occurred_at": t.isoformat(),
                "occurred_at_fmt": _fmt_audit_ts(oc if isinstance(oc, datetime) else None),
                "op": str(r.get("action") or "VIEW"),
                "summary": summary[:500],
                "principal": r.get("principal") or "",
                "principal_email": r.get("principal_email") or "",
                "request_id": r.get("request_id") or "",
                "source": r.get("source") or "",
                "endpoint": ep,
                "module": r.get("module") or "",
                "entity": r.get("entity") or "",
                "extra": _audit_sanitize_for_json(ex_d) if ex_d else None,
            }
            rows_merge.append((t, entry))

        rows_merge.sort(key=lambda x: x[0])
        merged = [x[1] for x in rows_merge[:combined_max]]
        return {"available": True, "rows": merged, "message": None}
    except Exception as exc:  # noqa: BLE001
        return {
            "available": True,
            "rows": [],
            "message": f"Could not load audit history: {exc}",
        }


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


def _manual_rsvp_list_counts_by_session(conn: Connection, event_id: int) -> tuple[dict[int, int], dict[int, int]]:
    """Counts from ``in_person_pw_session_rsvp_list_emails`` keyed by ``pw_session_id``."""
    manual_sent: dict[int, int] = {}
    manual_acc: dict[int, int] = {}
    mrows = ()
    try:
        mrows = conn.execute(
            text(
                f"""
                SELECT pw_session_id, list_kind, COUNT(*)::BIGINT AS c
                FROM {TABLE_IN_PERSON_RSVP_LIST_EMAILS}
                WHERE event_id = :eid
                GROUP BY pw_session_id, list_kind
                """
            ),
            {"eid": event_id},
        ).mappings().all()
    except ProgrammingError:
        mrows = ()
    except Exception:  # noqa: BLE001
        mrows = ()
    for mr in mrows:
        sid = int(mr["pw_session_id"])
        kind = str(mr["list_kind"])
        c = int(mr["c"] or 0)
        if kind == ip_rsvp_list_svc.LIST_KIND_INVITE_SENT:
            manual_sent[sid] = c
        elif kind == ip_rsvp_list_svc.LIST_KIND_ACCEPTED:
            manual_acc[sid] = c
    return manual_sent, manual_acc


def _overlay_manual_rsvp_on_hawkeye_event_rows(
    conn: Connection,
    event_id: int,
    rows: list[dict],
) -> list[dict]:
    """
    Hawkeye API rows for the in-person dashboard: same manual-first rule as MDC ``pw_session_rsvp``
    for ``rsvp_invite_sent`` / ``rsvp_accepted`` (``latest`` payload keys).
    """
    manual_sent, manual_acc = _manual_rsvp_list_counts_by_session(conn, event_id)
    out: list[dict] = []
    for row in rows:
        r = dict(row)
        pw_id = r.get("pw_session_id")
        if pw_id is None:
            out.append(r)
            continue
        sid = int(pw_id)
        ms = manual_sent.get(sid, 0)
        ma = manual_acc.get(sid, 0)
        if ms <= 0 and ma <= 0:
            out.append(r)
            continue
        latest_raw = r.get("latest")
        latest: dict
        if isinstance(latest_raw, dict):
            latest = dict(latest_raw)
        elif latest_raw is None:
            latest = {}
        else:
            latest = dict(latest_raw)
        h_sent = int(latest.get("rsvp_invite_sent") or 0)
        h_acc = int(latest.get("rsvp_accepted") or 0)
        latest["rsvp_invite_sent"] = ms if ms > 0 else h_sent
        latest["rsvp_accepted"] = ma if ma > 0 else h_acc
        r["latest"] = latest
        out.append(r)
    return out


def _enrich_pw_session_rsvp_rows(
    engine: Engine,
    conn: Connection,
    event_id: int,
    rows: list[dict],
) -> list[dict]:
    """Set ``rsvp_sent`` / ``rsvp_accepted``: manual import counts if any rows exist, else Hawkeye."""
    if not rows:
        return rows
    try:
        srows = conn.execute(
            text(
                f"""
                SELECT id, city, prompt_war_on, session_label
                FROM {TABLE_IN_PERSON_PW_SESSIONS}
                WHERE event_id = :eid
                """
            ),
            {"eid": event_id},
        ).mappings().all()
    except Exception:  # noqa: BLE001
        srows = ()

    sid_map: dict[tuple[str, str, str], int] = {}
    for sr in srows:
        pwo = sr["prompt_war_on"]
        if isinstance(pwo, datetime):
            pwo = pwo.date()
        elif not isinstance(pwo, date):
            continue
        k = _pw_session_rsvp_row_key(str(sr["city"]), pwo, str(sr.get("session_label") or ""))
        sid_map[k] = int(sr["id"])

    manual_sent, manual_acc = _manual_rsvp_list_counts_by_session(conn, event_id)

    hawk_sess: list[dict] = []
    for row in rows:
        hawk_sess.append(
            {
                "pw_session_id": sid_map.get(
                    _pw_session_rsvp_row_key(row["city"], row["prompt_war_on"], row["session_label"])
                ),
                "city": row["city"],
                "prompt_war_on_iso": row["prompt_war_on"],
                "session_label": row["session_label"],
                "display": row["session_display"],
                "team_count": 0,
            }
        )
    try:
        hawk_rows = hawkeye_service.list_pw_session_rows(engine, event_id, hawk_sess)
    except Exception:  # noqa: BLE001
        hawk_rows = [{} for _ in rows]

    out: list[dict] = []
    for row, hr in zip(rows, hawk_rows):
        k = _pw_session_rsvp_row_key(row["city"], row["prompt_war_on"], row["session_label"])
        sid = sid_map.get(k)
        ms = manual_sent.get(sid, 0) if sid else 0
        ma = manual_acc.get(sid, 0) if sid else 0
        latest = ((hr or {}).get("latest")) or {}
        h_sent = int(latest.get("rsvp_invite_sent") or 0)
        h_acc = int(latest.get("rsvp_accepted") or 0)
        out.append(
            {
                **row,
                "rsvp_sent": ms if ms > 0 else h_sent,
                "rsvp_accepted": ma if ma > 0 else h_acc,
            }
        )
    return out


def _load_mdc_stats_uncached(
    event_id: int,
    *,
    mode: str = "in_person",
    date_from: date | None = None,
    date_to: date | None = None,
    mdc_crossover_in_person_event_id: int | None = None,
    mdc_crossover_virtual_event_id: int | None = None,
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
        "pw_session_rsvp": [],
        "gender_breakdown": [],
        "top_occupations": [],
        # In-person ↔ virtual MDC overlap (distinct normalized emails); see end of try block.
        "mdc_crossover_both_tracks": None,
        "mdc_crossover_virtual_distinct": None,
        "mdc_crossover_in_person_distinct": None,
        "mdc_crossover_in_person_only": None,
        "mdc_crossover_virtual_only": None,
        # Virtual MDC emails (this event) that also appear as leaders in any in-person Action Center import.
        "mdc_crossover_virtual_reg_ip_action_center": None,
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

            if not is_virtual:
                # PW session list is all-time for the event (not scoped to MDC registration date range).
                pw_rsvp_sql = f"""
                    SELECT DISTINCT
                           COALESCE(NULLIF(btrim(attendance_city), ''), NULLIF(btrim(city), ''), '(Unknown)') AS city_label,
                           prompt_war_on,
                           btrim(COALESCE(session_label, '')) AS session_label_raw
                    FROM {table}
                    WHERE event_id = :eid
                      AND prompt_war_on <> DATE '1970-01-01'
                    ORDER BY prompt_war_on ASC, city_label ASC, session_label_raw ASC
                    LIMIT 80
                    """
                prsvp = conn.execute(text(pw_rsvp_sql), qp).mappings().all()
                rsvp_rows: list[dict] = []
                for r in prsvp:
                    city = str(r["city_label"])
                    pwo = r["prompt_war_on"]
                    if isinstance(pwo, datetime):
                        pwo = pwo.date()
                    elif not isinstance(pwo, date):
                        continue
                    sl = str(r["session_label_raw"] or "")
                    rsvp_rows.append(
                        {
                            "session_display": _ipcsr_pw_session_display(
                                city=city, prompt_war_on=pwo, session_label=sl
                            ),
                            "city": city,
                            "prompt_war_on": pwo.isoformat(),
                            "session_label": sl,
                            "rsvp_sent": 0,
                            "rsvp_accepted": 0,
                            "attended": 0,
                        }
                    )
                # Merge main-challenge sessions so RSVP rows match the sticky PW bar; neither branch
                # uses the MDC registration date filter.
                by_key: dict[tuple[str, str, str], dict] = {
                    _pw_session_rsvp_row_key(
                        row["city"], row["prompt_war_on"], row["session_label"]
                    ): row
                    for row in rsvp_rows
                }
                try:
                    sub_rows = conn.execute(
                        text(
                            f"""
                            SELECT DISTINCT
                                   btrim(attendance_city) AS city_raw,
                                   prompt_war_on,
                                   btrim(COALESCE(session_label, '')) AS session_label_raw
                            FROM {TABLE_IN_PERSON_CHALLENGE_SUBMISSIONS}
                            WHERE event_id = :eid
                              AND sheet_kind = 'main'
                              AND attendance_city IS NOT NULL
                              AND btrim(attendance_city) <> ''
                              AND prompt_war_on IS NOT NULL
                              AND prompt_war_on <> DATE '1970-01-01'
                            """
                        ),
                        {"eid": event_id},
                    ).mappings().all()
                except Exception:  # noqa: BLE001
                    sub_rows = ()
                for sr in sub_rows:
                    cr = sr.get("city_raw")
                    if not cr or not str(cr).strip():
                        continue
                    city_s = str(cr).strip()
                    pwo_s = sr["prompt_war_on"]
                    if isinstance(pwo_s, datetime):
                        pwo_s = pwo_s.date()
                    elif not isinstance(pwo_s, date):
                        continue
                    sl_s = str(sr.get("session_label_raw") or "")
                    k = _pw_session_rsvp_row_key(city_s, pwo_s, sl_s)
                    if k in by_key:
                        continue
                    by_key[k] = {
                        "session_display": _ipcsr_pw_session_display(
                            city=city_s, prompt_war_on=pwo_s, session_label=sl_s
                        ),
                        "city": city_s,
                        "prompt_war_on": pwo_s.isoformat(),
                        "session_label": sl_s,
                        "rsvp_sent": 0,
                        "rsvp_accepted": 0,
                        "attended": 0,
                    }
                merged_rsvp = sorted(
                    by_key.values(),
                    key=lambda row: (
                        row["prompt_war_on"],
                        str(row["city"]).lower(),
                        row["session_label"],
                    ),
                )
                merged_trim = merged_rsvp[:80]
                out["pw_session_rsvp"] = _enrich_pw_session_rsvp_rows(engine, conn, event_id, merged_trim)
            else:
                out["pw_session_rsvp"] = []

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

            c_ip = mdc_crossover_in_person_event_id
            c_v = mdc_crossover_virtual_event_id
            if (
                c_ip is not None
                and c_v is not None
                and int(c_ip) > 0
                and int(c_v) > 0
            ):
                cx = conn.execute(
                    text(
                        f"""
                        WITH ve AS (
                            SELECT email_normalized
                            FROM {TABLE_VIRTUAL_MDC}
                            WHERE event_id = :v_eid
                              AND email_normalized IS NOT NULL
                              AND btrim(email_normalized::text) <> ''
                        ),
                        ipe AS (
                            SELECT email_normalized
                            FROM {TABLE_IN_PERSON_MDC}
                            WHERE event_id = :ip_eid
                              AND email_normalized IS NOT NULL
                              AND btrim(email_normalized::text) <> ''
                        )
                        SELECT
                          (SELECT COUNT(DISTINCT email_normalized)::bigint FROM ve) AS v_n,
                          (SELECT COUNT(DISTINCT email_normalized)::bigint FROM ipe) AS ip_n,
                          (SELECT COUNT(DISTINCT ve.email_normalized)::bigint
                           FROM ve INNER JOIN ipe ON ipe.email_normalized = ve.email_normalized) AS both_n
                        """
                    ),
                    {"v_eid": int(c_v), "ip_eid": int(c_ip)},
                ).mappings().fetchone()
                if cx:
                    v_n = int(cx.get("v_n") or 0)
                    ip_n = int(cx.get("ip_n") or 0)
                    both_n = int(cx.get("both_n") or 0)
                    out["mdc_crossover_virtual_distinct"] = v_n
                    out["mdc_crossover_in_person_distinct"] = ip_n
                    out["mdc_crossover_both_tracks"] = both_n
                    out["mdc_crossover_in_person_only"] = max(0, ip_n - both_n)
                    out["mdc_crossover_virtual_only"] = max(0, v_n - both_n)

            if is_virtual:
                ac_n = conn.execute(
                    text(
                        f"""
                        SELECT COUNT(DISTINCT v.email_normalized)::bigint AS n
                        FROM {TABLE_VIRTUAL_MDC} v
                        JOIN events e_v ON e_v.id = v.event_id AND e_v.kind = 'virtual'
                        WHERE v.event_id = :eid
                          AND v.email_normalized IS NOT NULL
                          AND btrim(v.email_normalized::text) <> ''
                          AND EXISTS (
                            SELECT 1
                            FROM {TABLE_IN_PERSON_CHALLENGE_SUBMISSIONS} ip
                            JOIN events e_ip ON e_ip.id = ip.event_id AND e_ip.kind = 'in_person'
                            WHERE ip.leader_email_normalized = v.email_normalized
                              AND ip.leader_email_normalized IS NOT NULL
                              AND btrim(ip.leader_email_normalized::text) <> ''
                          )
                        """
                    ),
                    {"eid": event_id},
                ).scalar()
                out["mdc_crossover_virtual_reg_ip_action_center"] = int(ac_n or 0)
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)
    return out


def _load_mdc_stats(
    event_id: int,
    *,
    mode: str = "in_person",
    date_from: date | None = None,
    date_to: date | None = None,
    mdc_crossover_in_person_event_id: int | None = None,
    mdc_crossover_virtual_event_id: int | None = None,
) -> dict:
    key = (
        "mdc_stats",
        str(mode),
        int(event_id),
        date_from.isoformat() if date_from else None,
        date_to.isoformat() if date_to else None,
        int(mdc_crossover_in_person_event_id)
        if mdc_crossover_in_person_event_id is not None
        else None,
        int(mdc_crossover_virtual_event_id)
        if mdc_crossover_virtual_event_id is not None
        else None,
    )
    return _PW_CACHE_HOT.get_or_set(
        key,
        lambda: _load_mdc_stats_uncached(
            event_id,
            mode=mode,
            date_from=date_from,
            date_to=date_to,
            mdc_crossover_in_person_event_id=mdc_crossover_in_person_event_id,
            mdc_crossover_virtual_event_id=mdc_crossover_virtual_event_id,
        ),
    )


def _serialize_mdc_row_json(row: dict) -> dict:
    pw_cell = row.get("prompt_war_on")
    sl_cell = row.get("session_label") or ""
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
    if pw_cell is not None:
        pwo = pw_cell
        if isinstance(pwo, datetime):
            pwo = pwo.date()
        if isinstance(pwo, date):
            out["prompt_war_on_iso"] = pwo.isoformat()
            city_disp = (str(row.get("attendance_city") or row.get("city") or "").strip()) or "(Unknown)"
            out["pw_session_display"] = _ipcsr_pw_session_display(
                city=city_disp,
                prompt_war_on=pwo,
                session_label=str(sl_cell) if sl_cell is not None else "",
            )
    return out


_IP_SUBMISSION_SESSION_TOKEN_MAX = 600


def _encode_ip_submission_session_token(city: str, prompt_war_on: date, session_label: str) -> str:
    payload = json.dumps(
        {
            "c": (city or "").strip()[:500],
            "d": prompt_war_on.isoformat()
            if isinstance(prompt_war_on, date)
            else str(prompt_war_on or "")[:32],
            "l": (session_label or "").strip()[:IPCSR_SESSION_LABEL_MAX_LEN],
        },
        separators=(",", ":"),
        ensure_ascii=False,
    )
    raw = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")
    return raw[:_IP_SUBMISSION_SESSION_TOKEN_MAX]


def _decode_ip_submission_session_token(raw: str | None) -> tuple[str, date, str] | None:
    s = (raw or "").strip()
    if not s or len(s) > _IP_SUBMISSION_SESSION_TOKEN_MAX:
        return None
    try:
        pad = (-len(s)) % 4
        if pad:
            s += "=" * pad
        blob = base64.urlsafe_b64decode(s.encode("ascii"))
        obj = json.loads(blob.decode("utf-8"))
        if not isinstance(obj, dict):
            return None
        c = str(obj.get("c") or "").strip()[:500]
        d_raw = str(obj.get("d") or "").strip()[:32]
        l = str(obj.get("l") or "").strip()[:IPCSR_SESSION_LABEL_MAX_LEN]
        if not d_raw:
            return None
        pwo = date.fromisoformat(d_raw[:10])
        return c, pwo, l
    except (ValueError, TypeError, json.JSONDecodeError, OSError):
        return None


def _load_ip_submission_session_filter_options(conn: Connection, event_id: int) -> list[dict]:
    """Distinct PW sessions from in-person workbook imports (for roster filter)."""
    try:
        rows = conn.execute(
            text(
                f"""
                SELECT DISTINCT attendance_city, prompt_war_on, session_label
                FROM {TABLE_IN_PERSON_CHALLENGE_SUBMISSIONS}
                WHERE event_id = :eid
                ORDER BY prompt_war_on DESC NULLS LAST, attendance_city ASC NULLS LAST, session_label ASC
                """
            ),
            {"eid": int(event_id)},
        ).mappings().all()
    except Exception:  # noqa: BLE001
        return []
    out: list[dict] = []
    for r in rows:
        city = str(r.get("attendance_city") or "").strip()
        pwo = r.get("prompt_war_on")
        if isinstance(pwo, datetime):
            pwo = pwo.date()
        if not isinstance(pwo, date):
            continue
        sl = str(r.get("session_label") or "")
        token = _encode_ip_submission_session_token(city, pwo, sl)
        disp = _ipcsr_pw_session_display(city=city or "(Unknown)", prompt_war_on=pwo, session_label=sl)
        out.append({"token": token, "label": disp})
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
    raw_pc = (args.get("participated_challenge_id") or "").strip()[:12]
    participated_challenge_id: int | None = None
    if raw_pc.isdigit():
        participated_challenge_id = int(raw_pc)
    ss_raw = (args.get("submission_session") or "").strip()[:_IP_SUBMISSION_SESSION_TOKEN_MAX]
    ip_submission_session = _decode_ip_submission_session_token(ss_raw) if ss_raw else None
    submission_session_token = ss_raw if ip_submission_session else ""

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

    arena_raw_ch = (args.get("arenaChallengeId") or "").strip()[:12]
    arena_challenge_id: int | None = int(arena_raw_ch) if arena_raw_ch.isdigit() else None
    arena_seg_raw = (args.get("arenaTeamSegment") or "").strip().lower()[:24]
    arena_team_segment: str | None = arena_seg_raw if arena_seg_raw in _MDC_ARENA_TEAM_SEGMENTS else None
    arena_ac_raw = (args.get("arenaAttemptsCompleted") or "").strip()[:8]
    arena_attempts_completed: int | None = None
    if arena_ac_raw.isdigit():
        _ac = int(arena_ac_raw)
        if _ac == 0:
            arena_attempts_completed = 0
        elif 1 <= _ac <= 99:
            arena_attempts_completed = _ac
    if arena_team_segment not in ("student", "professional"):
        arena_attempts_completed = None
    has_virtual_arena = arena_challenge_id is not None and arena_team_segment is not None
    has_ip_arena = (
        ip_submission_session is not None
        and arena_team_segment is not None
        and arena_challenge_id is None
    )

    if (
        not text
        and form_ts_from is None
        and form_ts_to is None
        and dob_from is None
        and dob_to is None
        and designation_years_min is None
        and designation_years_max is None
        and participated_challenge_id is None
        and ip_submission_session is None
        and not has_virtual_arena
        and not has_ip_arena
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
        "participated_challenge_id": participated_challenge_id,
        "submission_session_token": submission_session_token,
        "ip_submission_session": ip_submission_session,
        "arena_challenge_id": arena_challenge_id if has_virtual_arena else None,
        "arena_team_segment": arena_team_segment if (has_virtual_arena or has_ip_arena) else None,
        "arena_attempts_completed": arena_attempts_completed
        if (has_virtual_arena or has_ip_arena)
        else None,
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
    *,
    mdc_pw_on_iso: str = "",
    mdc_session_label: str = "",
    virtual_event_id: int | None = None,
    roster_sort_key: str | None = None,
    roster_sort_dir: str | None = None,
) -> dict[str, str]:
    """Flat query-string parts (no page) for pagination, export, and per-page form."""
    out: dict[str, str] = {}
    if virtual_event_id is not None and int(virtual_event_id) != int(DEFAULT_VIRTUAL_EVENT_ID):
        out["virtualEventId"] = str(int(virtual_event_id))
    if search_s:
        out["q"] = search_s
    if attendance_city:
        out["attendance_city"] = attendance_city
    if challenge_id:
        out["challengeId"] = str(int(challenge_id))
    if mdc_pw_on_iso.strip():
        out["mdc_pw_on"] = mdc_pw_on_iso.strip()[:32]
    if (mdc_session_label or "").strip():
        out["mdc_session_label"] = mdc_session_label.strip()[:200]
    if not advanced:
        if roster_sort_key:
            out["sort"] = roster_sort_key
            d = (roster_sort_dir or "desc").lower()[:4]
            out["sort_dir"] = "asc" if d == "asc" else "desc"
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
    pcid = advanced.get("participated_challenge_id")
    if pcid is not None:
        try:
            out["participated_challenge_id"] = str(int(pcid))
        except (TypeError, ValueError):
            pass
    sst = (advanced.get("submission_session_token") or "").strip()
    if sst:
        out["submission_session"] = sst[:_IP_SUBMISSION_SESSION_TOKEN_MAX]
    ach = advanced.get("arena_challenge_id")
    aseg = advanced.get("arena_team_segment")
    seg_low = (str(aseg).strip().lower() if aseg is not None else "")
    if ach is not None and aseg:
        try:
            out["arenaChallengeId"] = str(int(ach))
        except (TypeError, ValueError):
            pass
        else:
            out["arenaTeamSegment"] = str(aseg)
            aac = advanced.get("arena_attempts_completed")
            if aac is not None:
                try:
                    out["arenaAttemptsCompleted"] = str(int(aac))
                except (TypeError, ValueError):
                    pass
    elif sst and aseg and seg_low in _MDC_ARENA_TEAM_SEGMENTS:
        out["arenaTeamSegment"] = str(aseg)
        aac = advanced.get("arena_attempts_completed")
        if aac is not None and seg_low in ("student", "professional"):
            try:
                out["arenaAttemptsCompleted"] = str(int(aac))
            except (TypeError, ValueError):
                pass
    if roster_sort_key:
        out["sort"] = roster_sort_key
        d = (roster_sort_dir or "desc").lower()[:4]
        out["sort_dir"] = "asc" if d == "asc" else "desc"
    return out


def _arena_roster_filter_applicable(
    advanced: dict[str, object] | None,
    *,
    event_id: int,
    mode: str,
) -> bool:
    if mode != "virtual" or not advanced:
        return False
    ach = advanced.get("arena_challenge_id")
    seg = (advanced.get("arena_team_segment") or "").strip().lower()
    if ach is None or not seg or seg not in _MDC_ARENA_TEAM_SEGMENTS:
        return False
    try:
        cid = int(ach)
    except (TypeError, ValueError):
        return False
    ch = _get_virtual_challenge(cid)
    return bool(ch and int(ch.get("event_id") or 0) == int(event_id))


def _append_virtual_arena_roster_filter_sql(
    advanced: dict[str, object] | None,
    *,
    event_id: int,
    table: str,
    conditions: list[str],
    params: dict,
) -> None:
    if not _arena_roster_filter_applicable(advanced, event_id=event_id, mode="virtual"):
        return
    assert advanced is not None
    arena_cid = int(advanced["arena_challenge_id"])
    seg = str(advanced.get("arena_team_segment") or "").strip().lower()
    params["arena_ch_id"] = arena_cid
    ac_extra = ""
    acn = advanced.get("arena_attempts_completed")
    if seg in ("student", "professional") and acn is not None:
        try:
            ac_int = int(acn)
        except (TypeError, ValueError):
            pass
        else:
            if ac_int == 0:
                ac_extra = " AND (s.attempts_completed IS NULL OR s.attempts_completed < 1)"
            elif 1 <= ac_int <= 99:
                ac_extra = (
                    " AND s.attempts_completed IS NOT NULL AND s.attempts_completed = :arena_ac_eq"
                )
                params["arena_ac_eq"] = ac_int
    if seg == "student":
        conditions.append(
            f"""
            EXISTS (
              SELECT 1 FROM virtual_challenge_submission_rows s
              INNER JOIN {table} m ON m.id = s.virtual_mdc_registration_id
              WHERE s.event_id = :eid AND s.challenge_id = :arena_ch_id
                AND m.id = {table}.id
                AND lower(btrim(m.occupation)) IN ('college_student', 'student')
                {ac_extra}
            )
            """.strip()
        )
    elif seg == "professional":
        conditions.append(
            f"""
            EXISTS (
              SELECT 1 FROM virtual_challenge_submission_rows s
              INNER JOIN {table} m ON m.id = s.virtual_mdc_registration_id
              WHERE s.event_id = :eid AND s.challenge_id = :arena_ch_id
                AND m.id = {table}.id
                AND lower(btrim(m.occupation)) IN (
                  'professional', 'startup', 'freelance', 'freelancer'
                )
                {ac_extra}
            )
            """.strip()
        )
    elif seg == "other":
        conditions.append(
            f"""
            EXISTS (
              SELECT 1 FROM virtual_challenge_submission_rows s
              INNER JOIN {table} m ON m.id = s.virtual_mdc_registration_id
              WHERE s.event_id = :eid AND s.challenge_id = :arena_ch_id
                AND m.id = {table}.id
                AND (
                  m.occupation IS NULL
                  OR btrim(m.occupation) = ''
                  OR lower(btrim(m.occupation)) NOT IN (
                    'college_student', 'student',
                    'professional', 'startup', 'freelance', 'freelancer'
                  )
                )
            )
            """.strip()
        )
    elif seg == "unknown":
        conditions.append(
            f"""
            EXISTS (
              SELECT 1 FROM virtual_challenge_submission_rows s
              WHERE s.event_id = :eid AND s.challenge_id = :arena_ch_id
                AND s.virtual_mdc_registration_id IS NULL
                AND s.leader_email_normalized = {table}.email_normalized
            )
            """.strip()
        )


def _ip_ac_arena_roster_filter_applicable(
    advanced: dict[str, object] | None,
    *,
    mode: str,
) -> bool:
    """In-person Action Center analytics → Users: ``submission_session`` + ``arenaTeamSegment`` (no challenge id)."""
    if mode != "in_person" or not advanced:
        return False
    if advanced.get("arena_challenge_id") is not None:
        return False
    if advanced.get("ip_submission_session") is None:
        return False
    seg = (advanced.get("arena_team_segment") or "").strip().lower()
    return seg in _MDC_ARENA_TEAM_SEGMENTS


def _append_in_person_arena_roster_filter_sql(
    advanced: dict[str, object] | None,
    *,
    table: str,
    conditions: list[str],
    params: dict,
) -> None:
    if not _ip_ac_arena_roster_filter_applicable(advanced, mode="in_person"):
        return
    assert advanced is not None
    sess = advanced["ip_submission_session"]
    city, pwo, slab = sess[0], sess[1], sess[2]
    params["ip_ac_city"] = (city or "").strip()[:500]
    params["ip_ac_pwo"] = pwo
    params["ip_ac_slab"] = (slab or "").strip()[:IPCSR_SESSION_LABEL_MAX_LEN]
    seg = str(advanced.get("arena_team_segment") or "").strip().lower()
    ac_extra = ""
    acn = advanced.get("arena_attempts_completed")
    if seg in ("student", "professional") and acn is not None:
        try:
            ac_int = int(acn)
        except (TypeError, ValueError):
            pass
        else:
            if ac_int == 0:
                ac_extra = " AND (s.attempts_completed IS NULL OR s.attempts_completed < 1)"
            elif 1 <= ac_int <= 99:
                ac_extra = (
                    " AND s.attempts_completed IS NOT NULL AND s.attempts_completed = :ip_ac_eq"
                )
                params["ip_ac_eq"] = ac_int
    if seg == "student":
        conditions.append(
            f"""
            EXISTS (
              SELECT 1 FROM {TABLE_IN_PERSON_CHALLENGE_SUBMISSIONS} s
              INNER JOIN {TABLE_IN_PERSON_MDC} m ON m.id = s.in_person_mdc_registration_id
              WHERE s.event_id = :eid AND s.sheet_kind = 'main'
                AND s.attendance_city_normalized = lower(btrim(:ip_ac_city))
                AND s.prompt_war_on = :ip_ac_pwo
                AND s.session_label_normalized = lower(btrim(:ip_ac_slab))
                AND m.id = {table}.id
                AND lower(btrim(m.occupation)) IN ('college_student', 'student')
                {ac_extra}
            )
            """.strip()
        )
    elif seg == "professional":
        conditions.append(
            f"""
            EXISTS (
              SELECT 1 FROM {TABLE_IN_PERSON_CHALLENGE_SUBMISSIONS} s
              INNER JOIN {TABLE_IN_PERSON_MDC} m ON m.id = s.in_person_mdc_registration_id
              WHERE s.event_id = :eid AND s.sheet_kind = 'main'
                AND s.attendance_city_normalized = lower(btrim(:ip_ac_city))
                AND s.prompt_war_on = :ip_ac_pwo
                AND s.session_label_normalized = lower(btrim(:ip_ac_slab))
                AND m.id = {table}.id
                AND lower(btrim(m.occupation)) IN (
                  'professional', 'startup', 'freelance', 'freelancer'
                )
                {ac_extra}
            )
            """.strip()
        )
    elif seg == "other":
        conditions.append(
            f"""
            EXISTS (
              SELECT 1 FROM {TABLE_IN_PERSON_CHALLENGE_SUBMISSIONS} s
              INNER JOIN {TABLE_IN_PERSON_MDC} m ON m.id = s.in_person_mdc_registration_id
              WHERE s.event_id = :eid AND s.sheet_kind = 'main'
                AND s.attendance_city_normalized = lower(btrim(:ip_ac_city))
                AND s.prompt_war_on = :ip_ac_pwo
                AND s.session_label_normalized = lower(btrim(:ip_ac_slab))
                AND m.id = {table}.id
                AND (
                  m.occupation IS NULL
                  OR btrim(m.occupation) = ''
                  OR lower(btrim(m.occupation)) NOT IN (
                    'college_student', 'student',
                    'professional', 'startup', 'freelance', 'freelancer'
                  )
                )
            )
            """.strip()
        )
    elif seg == "unknown":
        conditions.append(
            f"""
            EXISTS (
              SELECT 1 FROM {TABLE_IN_PERSON_CHALLENGE_SUBMISSIONS} s
              WHERE s.event_id = :eid AND s.sheet_kind = 'main'
                AND s.attendance_city_normalized = lower(btrim(:ip_ac_city))
                AND s.prompt_war_on = :ip_ac_pwo
                AND s.session_label_normalized = lower(btrim(:ip_ac_slab))
                AND s.in_person_mdc_registration_id IS NULL
                AND s.leader_email_normalized = {table}.email_normalized
            )
            """.strip()
        )


def _mdc_users_build_filter(
    event_id: int,
    search: str,
    attendance_city: str | None,
    *,
    mode: str = "in_person",
    challenge_id: int | None = None,
    advanced: dict[str, object] | None = None,
    mdc_pw_on: date | None = None,
    mdc_session_label: str | None = None,
) -> tuple[str, dict]:
    """Build WHERE clause; `search` must already be trimmed (empty means no text filter)."""
    table = _mdc_table_for_mode(mode)
    conditions = ["event_id = :eid"]
    params: dict = {"eid": event_id}
    if attendance_city:
        conditions.append("btrim(COALESCE(attendance_city, '')) = :acity")
        params["acity"] = attendance_city
    if mode == "in_person" and mdc_pw_on is not None:
        conditions.append("prompt_war_on = :mdc_pw_on")
        params["mdc_pw_on"] = mdc_pw_on
    if mode == "in_person" and (mdc_session_label or "").strip():
        conditions.append("lower(btrim(COALESCE(session_label, ''))) = :mdc_sln")
        params["mdc_sln"] = mdc_session_label.strip().lower()[:IPCSR_SESSION_LABEL_MAX_LEN]
    if search:
        conditions.append(
            "("
            "email ILIKE :q OR COALESCE(full_name, '') ILIKE :q OR "
            "COALESCE(profile_name, '') ILIKE :q OR COALESCE(mobile, '') ILIKE :q"
            ")"
        )
        params["q"] = f"%{search}%"
    _mdc_users_advanced_apply_sql(advanced, conditions, params)
    if advanced:
        pcid = advanced.get("participated_challenge_id")
        if mode == "virtual" and pcid is not None:
            try:
                pc_int = int(pcid)
            except (TypeError, ValueError):
                pc_int = 0
            if pc_int > 0:
                ch = _get_virtual_challenge(pc_int)
                if ch and int(ch.get("event_id") or 0) == int(event_id):
                    conditions.append(
                        "EXISTS (SELECT 1 FROM virtual_challenge_submission_rows s "
                        f"WHERE s.event_id = :eid AND s.challenge_id = :part_ch_id "
                        f"AND (s.virtual_mdc_registration_id = {table}.id "
                        f"OR s.leader_email_normalized = {table}.email_normalized))"
                    )
                    params["part_ch_id"] = pc_int
        ip_sess = advanced.get("ip_submission_session")
        if mode == "in_person" and ip_sess is not None:
            city, pwo, sl = ip_sess
            conditions.append(
                "EXISTS (SELECT 1 FROM in_person_challenge_submission_rows s "
                "WHERE s.event_id = :eid "
                "AND s.attendance_city_normalized = lower(btrim(:ss_city)) "
                "AND s.prompt_war_on = :ss_pwo "
                "AND lower(btrim(COALESCE(s.session_label, ''))) = lower(btrim(:ss_sl)) "
                f"AND s.leader_email_normalized = {table}.email_normalized)"
            )
            params["ss_city"] = (city or "").strip()[:500]
            params["ss_pwo"] = pwo
            params["ss_sl"] = (sl or "").strip()[:IPCSR_SESSION_LABEL_MAX_LEN]
        if mode == "virtual":
            _append_virtual_arena_roster_filter_sql(
                advanced,
                event_id=event_id,
                table=table,
                conditions=conditions,
                params=params,
            )
        elif mode == "in_person":
            _append_in_person_arena_roster_filter_sql(
                advanced,
                table=table,
                conditions=conditions,
                params=params,
            )
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
        cohort = _virtual_challenge_submission_cohort_stats(event_id)
        for d in out:
            cid = int(d["id"])
            c = cohort.get(cid) or {}
            d["submission_distinct_teams"] = int(c.get("submission_distinct_teams") or 0)
            d["submission_fresh_vs_prior_challenge"] = int(c.get("submission_fresh_vs_prior_challenge") or 0)
            d["submission_returning_from_prior_challenge"] = int(
                c.get("submission_returning_from_prior_challenge") or 0
            )
            d["submission_prior_challenge_id"] = c.get("submission_prior_challenge_id")
            d["submission_prior_challenge_title"] = c.get("submission_prior_challenge_title")
        return out
    except Exception:  # noqa: BLE001
        return []


def _load_virtual_challenges_brief_uncached(event_id: int) -> list[dict]:
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


def _load_virtual_challenges_brief(event_id: int) -> list[dict]:
    key = ("v_challenges_brief", int(event_id))
    return _PW_CACHE_WARM.get_or_set(key, lambda: _load_virtual_challenges_brief_uncached(event_id))


def _virtual_challenge_submission_cohort_stats(event_id: int) -> dict[int, dict]:
    """Per challenge: distinct leaders (normalized email) and split vs the **chronologically prior** arena round.

    Prior round = previous row when challenges are ordered by ``closes_at``, ``opens_at``, ``id``
    (earlier real-world round first). This is independent of admin UI ordering (e.g. live tab first).

    *Fresh* = no row on that prior round; *returning* = at least one row on both this round and the prior.
    """
    try:
        with engine.connect() as conn:
            ev = conn.execute(
                text("SELECT kind FROM events WHERE id = :eid"),
                {"eid": int(event_id)},
            ).fetchone()
            if not ev or str(ev[0]) != "virtual":
                return {}
            ch_ord = "ORDER BY c.closes_at ASC NULLS LAST, c.opens_at ASC NULLS LAST, c.id ASC"
            rows = conn.execute(
                text(
                    f"""
                    WITH ordered_ch AS (
                      SELECT c.id,
                             LAG(c.id) OVER ({ch_ord}) AS prev_challenge_id,
                             LAG(c.title) OVER ({ch_ord}) AS prev_challenge_title
                      FROM challenges c
                      WHERE c.event_id = :eid
                    ),
                    distinct_submitters AS (
                      SELECT r.challenge_id,
                             r.leader_email_normalized
                      FROM virtual_challenge_submission_rows r
                      WHERE r.event_id = :eid
                      GROUP BY r.challenge_id, r.leader_email_normalized
                    ),
                    labeled AS (
                      SELECT ds.challenge_id,
                             oc.prev_challenge_id,
                             oc.prev_challenge_title,
                             EXISTS (
                               SELECT 1
                               FROM virtual_challenge_submission_rows p
                               WHERE p.event_id = :eid
                                 AND p.challenge_id = oc.prev_challenge_id
                                 AND p.leader_email_normalized = ds.leader_email_normalized
                             ) AS in_prev
                      FROM distinct_submitters ds
                      JOIN ordered_ch oc ON oc.id = ds.challenge_id
                    ),
                    agg AS (
                      SELECT challenge_id,
                             COUNT(*)::BIGINT AS distinct_teams,
                             COUNT(*) FILTER (WHERE prev_challenge_id IS NOT NULL AND in_prev)::BIGINT
                               AS returning_from_prior,
                             COUNT(*) FILTER (WHERE prev_challenge_id IS NULL OR NOT in_prev)::BIGINT
                               AS fresh_vs_prior
                      FROM labeled
                      GROUP BY challenge_id
                    )
                    SELECT oc.id AS challenge_id,
                           oc.prev_challenge_id,
                           oc.prev_challenge_title,
                           COALESCE(a.distinct_teams, 0)::BIGINT AS distinct_teams,
                           COALESCE(a.returning_from_prior, 0)::BIGINT AS returning_from_prior,
                           COALESCE(a.fresh_vs_prior, 0)::BIGINT AS fresh_vs_prior
                    FROM ordered_ch oc
                    LEFT JOIN agg a ON a.challenge_id = oc.id
                    """
                ),
                {"eid": int(event_id)},
            ).mappings().all()
        out_map: dict[int, dict] = {}
        for r in rows:
            cid = int(r["challenge_id"])
            pid = r.get("prev_challenge_id")
            out_map[cid] = {
                "submission_distinct_teams": int(r["distinct_teams"] or 0),
                "submission_returning_from_prior_challenge": int(r["returning_from_prior"] or 0),
                "submission_fresh_vs_prior_challenge": int(r["fresh_vs_prior"] or 0),
                "submission_prior_challenge_id": int(pid) if pid is not None else None,
                "submission_prior_challenge_title": (r.get("prev_challenge_title") or "").strip() or None,
            }
        return out_map
    except Exception:  # noqa: BLE001
        return {}


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
    db_cities = [str(x) for x in rows if x]
    seen = {c.strip().lower() for c in db_cities}
    merged = list(db_cities)
    for extra in IN_PERSON_PW_EXTRA_ATTENDANCE_CITIES:
        label = extra.strip()
        if not label:
            continue
        key = label.lower()
        if key in seen:
            continue
        merged.append(label)
        seen.add(key)
    merged.sort(key=str.casefold)
    return merged


def _load_mdc_advanced_select_options(
    conn,
    event_id: int,
    *,
    mode: str,
    columns: tuple[str, ...] = MDC_USERS_ADVANCED_TEXT_COLUMNS,
    per_column_limit: int = MDC_USERS_ADVANCED_SELECT_LIMIT,
) -> dict[str, list[str]]:
    """Distinct non-empty values per advanced-filter column (dropdown options, cap per column).

    One round-trip: ``UNION ALL`` of per-column limited DISTINCT subqueries (columns are
    identifiers from ``MDC_USERS_ADVANCED_TEXT_COLUMNS`` only).
    """
    table = _mdc_table_for_mode(mode)
    out: dict[str, list[str]] = {c: [] for c in MDC_USERS_ADVANCED_TEXT_COLUMNS}
    lim = int(per_column_limit)
    valid_cols = [c for c in columns if c in MDC_USERS_ADVANCED_TEXT_COLUMNS]
    if not valid_cols:
        return out
    parts: list[str] = []
    for col in valid_cols:
        parts.append(
            f"""(SELECT '{col}'::text AS col_key, v FROM (
                SELECT DISTINCT btrim({col})::text AS v
                FROM {table}
                WHERE event_id = :eid AND {col} IS NOT NULL AND btrim({col}) <> ''
                ORDER BY 1 ASC
                LIMIT :lim
            ) sq)"""
        )
    sql = " UNION ALL ".join(parts)
    try:
        rows = conn.execute(text(sql), {"eid": event_id, "lim": lim}).mappings().all()
        for r in rows:
            ck = r.get("col_key")
            v = r.get("v")
            if ck in out and v:
                out[str(ck)].append(str(v))
    except Exception:  # noqa: BLE001
        for col in valid_cols:
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
    mdc_pw_on_iso: str = "",
    mdc_session_label: str = "",
    virtual_event_id: int | None = None,
    roster_sort_key: str | None = None,
    roster_sort_dir: str | None = None,
) -> str:
    """Querystring that drops every advanced-filter param while keeping search, dropdowns, per-page."""
    params: dict[str, str] = {"per_page": str(per_page)}
    if virtual_event_id is not None and int(virtual_event_id) != int(DEFAULT_VIRTUAL_EVENT_ID):
        params["virtualEventId"] = str(int(virtual_event_id))
    if roster_sort_key:
        params["sort"] = roster_sort_key
        d = (roster_sort_dir or "desc").lower()[:4]
        params["sort_dir"] = "asc" if d == "asc" else "desc"
    if search_s:
        params["q"] = search_s
    if attendance_city:
        params["attendance_city"] = attendance_city
    if challenge_id:
        params["challengeId"] = str(int(challenge_id))
    if mdc_pw_on_iso.strip():
        params["mdc_pw_on"] = mdc_pw_on_iso.strip()[:32]
    if (mdc_session_label or "").strip():
        params["mdc_session_label"] = mdc_session_label.strip()[:200]
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
    mdc_pw_on: date | None = None,
    mdc_session_label: str | None = None,
    roster_sort_key: str | None = None,
    roster_sort_dir: str | None = None,
) -> dict:
    """Paginated Main Data Center registrations for the Vision roster table."""
    table = _mdc_table_for_mode(mode)
    per_page = max(10, min(int(per_page or 25), 100))
    page = max(1, int(page or 1))
    offset = (page - 1) * per_page
    search_s = (search or "").strip()[:200]
    ac = None if mode == "virtual" else ((attendance_city or "").strip()[:200] or None)
    cid = int(challenge_id) if (mode == "virtual" and challenge_id) else None
    sk = roster_sort_key
    sd = (roster_sort_dir or "desc").lower()[:4]
    sd = "asc" if sd == "asc" else "desc"
    if sk == "score" and mode != "virtual":
        sk = None
    adv_active = bool(advanced)
    pw_iso = mdc_pw_on.isoformat() if mdc_pw_on is not None else ""
    sl_f = (mdc_session_label or "").strip()
    preserve = _mdc_users_preserve_query_dict(
        search_s,
        ac,
        cid,
        advanced,
        mdc_pw_on_iso=pw_iso,
        mdc_session_label=sl_f,
        virtual_event_id=event_id if mode == "virtual" else None,
        roster_sort_key=sk,
        roster_sort_dir=sd if sk else None,
    )
    export_query = urlencode(preserve)
    pagination_body = {"per_page": str(per_page), **preserve}
    preserve_query_str = urlencode(pagination_body)
    advanced_text = dict((advanced or {}).get("text") or {})
    advanced_raw = dict((advanced or {}).get("raw") or {})
    adv_part = 0
    if advanced:
        if advanced.get("participated_challenge_id"):
            adv_part += 1
        if (advanced.get("submission_session_token") or "").strip():
            adv_part += 1
        if _arena_roster_filter_applicable(advanced, event_id=event_id, mode=mode):
            adv_part += 1
            _seg_a = (advanced.get("arena_team_segment") or "").strip().lower()
            if (
                _seg_a in ("student", "professional")
                and advanced.get("arena_attempts_completed") is not None
            ):
                adv_part += 1
        if _ip_ac_arena_roster_filter_applicable(advanced, mode=mode):
            adv_part += 1
            _seg_ip = (advanced.get("arena_team_segment") or "").strip().lower()
            if (
                _seg_ip in ("student", "professional")
                and advanced.get("arena_attempts_completed") is not None
            ):
                adv_part += 1
    advanced_count = len(advanced_text) + sum(1 for v in advanced_raw.values() if v) + adv_part
    participation_challenge_options: list[dict] = []
    participation_submission_session_options: list[dict] = []
    if mode == "virtual":
        participation_challenge_options = [
            {"id": int(c["id"]), "title": str(c.get("title") or "")}
            for c in _load_virtual_challenges_brief(event_id)
        ]
    else:
        try:
            with engine.connect() as _conn_opts:
                participation_submission_session_options = _load_ip_submission_session_filter_options(
                    _conn_opts, event_id
                )
        except Exception:  # noqa: BLE001
            participation_submission_session_options = []
    advanced_chips = _mdc_users_active_chips(advanced, preserve=preserve, per_page=per_page)
    base_chip = dict(preserve)
    base_chip["per_page"] = str(per_page)
    if mode == "in_person":
        if pw_iso:
            rm = dict(base_chip)
            rm.pop("mdc_pw_on", None)
            advanced_chips.append(
                {
                    "key": "mdc_pw_on",
                    "label": "PW date",
                    "value": pw_iso,
                    "remove_qs": urlencode(rm),
                }
            )
        if sl_f:
            rm = dict(base_chip)
            rm.pop("mdc_session_label", None)
            advanced_chips.append(
                {
                    "key": "mdc_session_label",
                    "label": "PW session label",
                    "value": sl_f,
                    "remove_qs": urlencode(rm),
                }
            )
    if advanced:
        pcid = advanced.get("participated_challenge_id")
        if pcid is not None and mode == "virtual":
            try:
                pc_int = int(pcid)
            except (TypeError, ValueError):
                pc_int = None
            if pc_int:
                ch_title = next(
                    (
                        str(c.get("title") or "")
                        for c in participation_challenge_options
                        if int(c["id"]) == pc_int
                    ),
                    f"#{pc_int}",
                )
                rm = dict(base_chip)
                rm.pop("participated_challenge_id", None)
                advanced_chips.append(
                    {
                        "key": "participated_challenge_id",
                        "label": "Submitted in challenge",
                        "value": ch_title,
                        "remove_qs": urlencode(rm),
                    }
                )
        if _arena_roster_filter_applicable(advanced, event_id=event_id, mode=mode):
            try:
                arena_cid = int(advanced["arena_challenge_id"])
            except (TypeError, ValueError, KeyError):
                arena_cid = 0
            seg_low = (advanced.get("arena_team_segment") or "").strip().lower()
            seg_label = {
                "student": "Student",
                "professional": "Professional",
                "other": "Other",
                "unknown": "Unknown",
            }.get(seg_low, seg_low)
            arena_ch_title = next(
                (
                    str(c.get("title") or "")
                    for c in participation_challenge_options
                    if int(c["id"]) == arena_cid
                ),
                f"#{arena_cid}",
            )
            aac = advanced.get("arena_attempts_completed")
            if (
                aac is not None
                and seg_low in ("student", "professional")
                and str(aac).strip().isdigit()
            ):
                rm_ac = dict(base_chip)
                rm_ac.pop("arenaAttemptsCompleted", None)
                advanced_chips.append(
                    {
                        "key": "arenaAttemptsCompleted",
                        "label": "Arena attempts completed",
                        "value": str(int(aac)),
                        "remove_qs": urlencode(rm_ac),
                    }
                )
            rm_arena = dict(base_chip)
            rm_arena.pop("arenaChallengeId", None)
            rm_arena.pop("arenaTeamSegment", None)
            rm_arena.pop("arenaAttemptsCompleted", None)
            advanced_chips.append(
                {
                    "key": "arenaTeamSegment",
                    "label": "Arena cohort",
                    "value": f"{arena_ch_title} · {seg_label}",
                    "remove_qs": urlencode(rm_arena),
                }
            )
        if _ip_ac_arena_roster_filter_applicable(advanced, mode=mode):
            seg_low = (advanced.get("arena_team_segment") or "").strip().lower()
            seg_label = {
                "student": "Student",
                "professional": "Professional",
                "other": "Other",
                "unknown": "Unknown",
            }.get(seg_low, seg_low)
            aac = advanced.get("arena_attempts_completed")
            if (
                aac is not None
                and seg_low in ("student", "professional")
                and str(aac).strip().isdigit()
            ):
                rm_ac = dict(base_chip)
                rm_ac.pop("arenaAttemptsCompleted", None)
                advanced_chips.append(
                    {
                        "key": "arenaAttemptsCompleted",
                        "label": "Action Center attempts completed",
                        "value": str(int(aac)),
                        "remove_qs": urlencode(rm_ac),
                    }
                )
            rm_ip = dict(base_chip)
            rm_ip.pop("submission_session", None)
            rm_ip.pop("arenaTeamSegment", None)
            rm_ip.pop("arenaAttemptsCompleted", None)
            sst_ip = (advanced.get("submission_session_token") or "").strip()
            sess_lab_ip = next(
                (o["label"] for o in participation_submission_session_options if o["token"] == sst_ip),
                sst_ip[:48] + ("…" if len(sst_ip) > 48 else ""),
            )
            advanced_chips.append(
                {
                    "key": "ip_ac_arena",
                    "label": "Action Center cohort",
                    "value": f"{sess_lab_ip} · {seg_label}",
                    "remove_qs": urlencode(rm_ip),
                }
            )
        sst = (advanced.get("submission_session_token") or "").strip()
        if sst and mode == "in_person" and not _ip_ac_arena_roster_filter_applicable(advanced, mode=mode):
            sess_lab = next(
                (o["label"] for o in participation_submission_session_options if o["token"] == sst),
                sst[:48] + ("…" if len(sst) > 48 else ""),
            )
            rm = dict(base_chip)
            rm.pop("submission_session", None)
            advanced_chips.append(
                {
                    "key": "submission_session",
                    "label": "Submitted in PW session (workbook)",
                    "value": sess_lab,
                    "remove_qs": urlencode(rm),
                }
            )
    reset_advanced_qs = _mdc_users_reset_advanced_qs(
        search_s=search_s,
        attendance_city=ac,
        challenge_id=cid,
        per_page=per_page,
        mdc_pw_on_iso=pw_iso,
        mdc_session_label=sl_f,
        virtual_event_id=event_id if mode == "virtual" else None,
        roster_sort_key=sk,
        roster_sort_dir=sd if sk else None,
    )
    sort_cols = (
        ["name", "email", "location", "occupation", "designation", "yrs_exp", "registered", "score"]
        if mode == "virtual"
        else [
            "name",
            "email",
            "location",
            "attendance_city",
            "occupation",
            "designation",
            "yrs_exp",
            "registered",
        ]
    )
    sort_hrefs = {
        c: _mdc_users_roster_sort_href_query(dict(preserve), per_page, c, sk, sd) for c in sort_cols
    }
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
        "mdc_pw_on": pw_iso,
        "mdc_session_label": sl_f,
        "participation_challenge_options": participation_challenge_options,
        "participation_submission_session_options": participation_submission_session_options,
        "selected_participated_challenge_id": advanced.get("participated_challenge_id")
        if advanced
        else None,
        "selected_submission_session": (advanced.get("submission_session_token") or "").strip()
        if advanced
        else "",
        "arena_from_charts_active": (
            _arena_roster_filter_applicable(advanced, event_id=event_id, mode=mode)
            or _ip_ac_arena_roster_filter_applicable(advanced, mode=mode)
        ),
        "sort_key": sk,
        "sort_dir": sd,
        "sort_hrefs": sort_hrefs,
        "roster_has_score_column": bool(mode == "virtual"),
    }
    try:
        with engine.connect() as conn:
            out["attendance_city_options"] = _load_mdc_attendance_city_options(conn, event_id, mode=mode)
            out["advanced_select_options"] = _load_mdc_advanced_select_options(
                conn, event_id, mode=mode
            )
            where_sql, params = _mdc_users_build_filter(
                event_id,
                search_s,
                ac,
                mode=mode,
                challenge_id=cid,
                advanced=advanced,
                mdc_pw_on=mdc_pw_on,
                mdc_session_label=mdc_session_label,
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
            extra_cols = ", prompt_war_on, session_label" if mode == "in_person" else ""
            score_sel = "NULL::numeric AS mdc_submission_score"
            if mode == "virtual":
                score_sel = _mdc_users_virtual_submission_score_select_sql(table, cid)
            order_sql = _mdc_users_roster_order_clause(sk, sd, mode=mode, challenge_id=cid)
            rows = conn.execute(
                text(
                    f"""
                    SELECT id, full_name, email, city, state, country, attendance_city, occupation,
                           mobile, profile_name, form_timestamp, designation, designation_years_experience,
                           {score_sel}
                           {extra_cols}
                    FROM {table}
                    WHERE {where_sql}
                    {order_sql}
                    LIMIT :lim OFFSET :off
                    """
                ),
                params_page,
            ).mappings().all()
        out["total"] = total
        out["total_pages"] = max(1, (total + per_page - 1) // per_page) if total else 1
        row_dicts = [dict(r) for r in rows]
        if mode == "in_person":
            for d in row_dicts:
                pwo = d.get("prompt_war_on")
                if isinstance(pwo, datetime):
                    pwo = pwo.date()
                if isinstance(pwo, date):
                    city_disp = (str(d.get("attendance_city") or d.get("city") or "").strip()) or "(Unknown)"
                    d["pw_session_display"] = _ipcsr_pw_session_display(
                        city=city_disp,
                        prompt_war_on=pwo,
                        session_label=str(d.get("session_label") or ""),
                    )
                    d["prompt_war_on_iso"] = pwo.isoformat()
        out["rows"] = row_dicts
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
    mdc_pw_on: date | None = None,
    mdc_session_label: str | None = None,
    roster_sort_key: str | None = None,
    roster_sort_dir: str | None = None,
) -> tuple[list[dict], str | None]:
    table = _mdc_table_for_mode(mode)
    search_s = (search or "").strip()[:200]
    ac = None if mode == "virtual" else ((attendance_city or "").strip()[:200] or None)
    cid = int(challenge_id) if (mode == "virtual" and challenge_id) else None
    sk = roster_sort_key
    sd = (roster_sort_dir or "desc").lower()[:4]
    sd = "asc" if sd == "asc" else "desc"
    if sk == "score" and mode != "virtual":
        sk = None
    where_sql, params = _mdc_users_build_filter(
        event_id,
        search_s,
        ac,
        mode=mode,
        challenge_id=cid,
        advanced=advanced,
        mdc_pw_on=mdc_pw_on,
        mdc_session_label=mdc_session_label,
    )
    extra_cols = ", prompt_war_on, session_label" if mode == "in_person" else ""
    score_sel = "NULL::numeric AS mdc_submission_score"
    if mode == "virtual":
        score_sel = _mdc_users_virtual_submission_score_select_sql(table, cid)
    order_sql = _mdc_users_roster_order_clause(sk, sd, mode=mode, challenge_id=cid)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    f"""
                    SELECT id, full_name, email, city, state, country, attendance_city, occupation,
                           mobile, profile_name, form_timestamp, designation, designation_years_experience,
                           {score_sel}
                           {extra_cols}
                    FROM {table}
                    WHERE {where_sql}
                    {order_sql}
                    """
                ),
                params,
            ).mappings().all()
        row_dicts = [dict(r) for r in rows]
        if mode == "in_person":
            for d in row_dicts:
                pwo = d.get("prompt_war_on")
                if isinstance(pwo, datetime):
                    pwo = pwo.date()
                if isinstance(pwo, date):
                    city_disp = (str(d.get("attendance_city") or d.get("city") or "").strip()) or "(Unknown)"
                    d["pw_session_display"] = _ipcsr_pw_session_display(
                        city=city_disp,
                        prompt_war_on=pwo,
                        session_label=str(d.get("session_label") or ""),
                    )
                    d["prompt_war_on_iso"] = pwo.isoformat()
        return row_dicts, None
    except Exception as exc:  # noqa: BLE001
        return [], str(exc)


def _mdc_users_rows_to_csv(rows: list[dict], *, mode: str = "in_person") -> bytes:
    include_attendance = mode != "virtual"
    include_virt_score = mode == "virtual"
    headers = [
        "id",
        "full_name",
        "email",
        "city",
        "state",
        "country",
        *(
            []
            if not include_attendance
            else ["attendance_city", "pw_session_display", "prompt_war_on_iso", "session_label"]
        ),
        "occupation",
        "mobile",
        "profile_name",
        *([] if not include_virt_score else ["imported_total_score"]),
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
            row_vals.append(r.get("pw_session_display") or "")
            row_vals.append(r.get("prompt_war_on_iso") or "")
            row_vals.append(r.get("session_label") or "")
        row_vals.extend(
            [
                r.get("occupation") or "",
                r.get("mobile") or "",
                r.get("profile_name") or "",
            ]
        )
        if include_virt_score:
            sc = r.get("mdc_submission_score")
            row_vals.append("" if sc is None else str(sc))
        row_vals.append(fts_s)
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
    conn: Connection | None = None,
) -> dict:
    """
    Team rows from virtual_challenge_submission_rows for one challenge.
    Order: total_score DESC, export_created_at ASC (earlier submission wins ties), id ASC.
    """
    limit = min(max(int(limit or 50), 1), 500)
    offset = max(int(offset or 0), 0)
    out: dict = {"rows": [], "total": 0, "error": None, "challenge": None}

    def _fill(c: Connection) -> None:
        ch = _validate_virtual_submission_challenge(c, event_id=event_id, challenge_id=challenge_id)
        if not ch:
            out["error"] = "challenge not found"
            return
        out["challenge"] = {"id": int(ch["id"]), "title": ch.get("title") or "", "event_id": int(ch["event_id"])}
        total = c.execute(
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
        rows = c.execute(
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

    try:
        if conn is not None:
            _fill(conn)
        else:
            with engine.connect() as c:
                _fill(c)
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
    conn: Connection | None = None,
) -> dict:
    """
    Aggregate ``virtual_challenge_submission_rows`` across all virtual arena challenges
    for one event. One row per ``leader_email_normalized``: each row's
    ``average_score`` is the mean of that leader's per-arena scores (missing scores count as
    zero; denominator is the number of arenas they have a submission row in).
    ``submitted_at`` is the earliest export timestamp across their rows; display
    team/name/email come from the row with the highest single-arena score (then earliest
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
                        SELECT leader_email_normalized,
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
                        GROUP BY leader_email_normalized
                    )
    """

    def _fill(c: Connection) -> None:
        total = int(
            c.execute(
                text(_vcsr_global_base + " SELECT COUNT(*)::BIGINT FROM agg "),
                {"eid": int(event_id)},
            ).scalar()
            or 0
        )
        out["total"] = total
        rows = c.execute(
            text(
                _vcsr_global_base
                + """
                , ranked AS (
                    SELECT leader_email_normalized,
                           team_name,
                           leader_name,
                           leader_email,
                           average_score,
                           submitted_at,
                           arena_count,
                           ROW_NUMBER() OVER (
                               ORDER BY average_score DESC NULLS LAST,
                                        submitted_at ASC NULLS LAST,
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

    try:
        if conn is not None:
            _fill(conn)
        else:
            with engine.connect() as c:
                _fill(c)
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
    conn: Connection | None = None,
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

    def _fill(c: Connection) -> None:
        base_where = f"event_id = :eid AND sheet_kind = 'main'"
        params: dict = {"eid": int(event_id)}
        if attendance_city and str(attendance_city).strip():
            base_where += " AND lower(btrim(attendance_city)) = lower(btrim(:acity))"
            params["acity"] = str(attendance_city).strip()
            base_where += " AND prompt_war_on = :pwon AND session_label_normalized = lower(btrim(:slab))"
            params["pwon"] = eff_pw
            params["slab"] = eff_sl
        total = c.execute(
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
        rows = c.execute(
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

    try:
        if conn is not None:
            _fill(conn)
        else:
            with engine.connect() as c:
                _fill(c)
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)
    return out


def _in_person_pw_options(event_id: int, conn: Connection | None = None) -> list[dict]:
    """PW sessions from ``in_person_pw_sessions`` with main-challenge team counts per session."""

    def _query(c: Connection):
        return c.execute(
            text(
                f"""
                SELECT
                  s.id AS pw_session_id,
                  s.city,
                  s.prompt_war_on,
                  s.session_label,
                  s.scope_key,
                  s.display_name,
                  s.created_at AS session_created_at,
                  COALESCE(t.cnt, 0)::BIGINT AS team_count
                FROM {TABLE_IN_PERSON_PW_SESSIONS} s
                LEFT JOIN (
                  SELECT
                    event_id,
                    lower(trim(both FROM attendance_city)) AS city_n,
                    prompt_war_on,
                    COALESCE(session_label, '') AS sl,
                    COUNT(*)::BIGINT AS cnt
                  FROM {TABLE_IN_PERSON_CHALLENGE_SUBMISSIONS}
                  WHERE sheet_kind = 'main'
                  GROUP BY event_id, lower(trim(both FROM attendance_city)), prompt_war_on, COALESCE(session_label, '')
                ) t
                  ON t.event_id = s.event_id
                 AND t.city_n = s.city
                 AND t.prompt_war_on = s.prompt_war_on
                 AND t.sl = s.session_label
                WHERE s.event_id = :eid
                ORDER BY s.prompt_war_on DESC, s.city, s.session_label
                """
            ),
            {"eid": int(event_id)},
        ).mappings().all()

    try:
        if conn is not None:
            rows = _query(conn)
        else:
            with engine.connect() as c:
                rows = _query(c)
        out: list[dict] = []
        for r in rows:
            cty = str(r.get("city") or "").strip()
            if not cty:
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
            disp = str(r.get("display_name") or "").strip() or _ipcsr_pw_session_display(
                city=cty, prompt_war_on=pwd, session_label=sl
            )
            ca = r.get("session_created_at")
            out.append(
                {
                    "pw_session_id": int(r["pw_session_id"]),
                    "city": cty,
                    "prompt_war_on_iso": iso,
                    "session_label": sl,
                    "display": disp,
                    "team_count": int(r["team_count"] or 0),
                    "session_created_at": ca,
                }
            )
        return out
    except Exception:  # noqa: BLE001
        return []


def _in_person_pw_default_reference_date() -> date:
    """IST calendar date used to compare ``prompt_war_on`` when picking a default session."""
    return datetime.now(ZoneInfo("Asia/Kolkata")).date()


def _in_person_pw_option_prompt_war_date(d: dict) -> date:
    iso = str(d.get("prompt_war_on_iso") or "")[:10]
    try:
        return date.fromisoformat(iso)
    except ValueError:
        return date.min


def _default_in_person_pw_session_for_redirect(pws: list[dict]) -> dict | None:
    """Pick the PW session when redirecting without ``ipActionCenterCity``.

    Uses each row's ``prompt_war_on`` (``prompt_war_on_iso``): prefer the **latest
    initiative that has already occurred** — the greatest date on or before today
    (Asia/Kolkata). Among ties on the same date, prefer higher ``team_count`` then
    ``pw_session_id``.

    If every session is still in the future relative to today, use the **earliest
    upcoming** ``prompt_war_on`` (same tiebreakers via inverted sort for ``min``).
    """
    if not pws:
        return None
    ref = _in_person_pw_default_reference_date()
    past_or_today = [d for d in pws if _in_person_pw_option_prompt_war_date(d) <= ref]

    def _key_latest_past(d: dict) -> tuple[date, int, int]:
        return (
            _in_person_pw_option_prompt_war_date(d),
            int(d.get("team_count") or 0),
            int(d.get("pw_session_id") or 0),
        )

    if past_or_today:
        return max(past_or_today, key=_key_latest_past)

    future_only = [d for d in pws if _in_person_pw_option_prompt_war_date(d) > ref]
    if not future_only:
        return max(pws, key=_key_latest_past)

    def _key_earliest_future(d: dict) -> tuple[date, int, int]:
        dd = _in_person_pw_option_prompt_war_date(d)
        tc = int(d.get("team_count") or 0)
        sid = int(d.get("pw_session_id") or 0)
        return (dd, -tc, -sid)

    return min(future_only, key=_key_earliest_future)


def _arena_submission_crossover_summary(sc: dict) -> dict:
    """Compact leader counts for arena Submission Analytics (in-person + virtual pages)."""
    if sc.get("error"):
        return {"error": str(sc["error"])}
    c = sc.get("counts") or {}
    return {
        "error": None,
        "distinct_ip_leaders": int(c.get("distinct_ip_leaders") or 0),
        "distinct_v_leaders": int(c.get("distinct_v_leaders") or 0),
        "both_tracks": int(c.get("both_tracks") or 0),
        "ip_only": int(c.get("ip_only") or 0),
        "v_only": int(c.get("v_only") or 0),
    }


def _virtual_arena_challenge_stats(*, event_id: int, challenge_id: int) -> dict:
    """
    Per-arena-challenge: registration counts at opens_at / closes_at (MDC),
    submission totals, distinct MDC-linked rows, fresh vs returning submitter
    counts vs the chronologically prior challenge (``closes_at``, ``opens_at``, ``id``),
    aggregate ``total_score`` stats on imported submission rows (all teams), plus
    the same five score summaries (min / max / avg / median / std dev) per student
    vs professional registration segment (same MDC occupation filters as the attempt charts).
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
        "submission_distinct_teams": 0,
        "submission_fresh_vs_prior_challenge": 0,
        "submission_returning_from_prior_challenge": 0,
        "submission_prior_challenge_id": None,
        "submission_prior_challenge_title": None,
        "team_segment_student": 0,
        "team_segment_professional": 0,
        "team_segment_other": 0,
        "team_segment_unknown": 0,
        "attempt_buckets_student": [],
        "attempt_buckets_professional": [],
        "submission_score_student_n": 0,
        "submission_score_student_min": None,
        "submission_score_student_max": None,
        "submission_score_student_avg": None,
        "submission_score_student_median": None,
        "submission_score_student_stddev": None,
        "submission_score_professional_n": 0,
        "submission_score_professional_min": None,
        "submission_score_professional_max": None,
        "submission_score_professional_avg": None,
        "submission_score_professional_median": None,
        "submission_score_professional_stddev": None,
        "submission_score_agg_n": 0,
        "submission_score_min": None,
        "submission_score_max": None,
        "submission_score_avg": None,
        "submission_score_median": None,
        "submission_score_p25": None,
        "submission_score_p75": None,
        "submission_score_stddev": None,
        "submission_score_range": None,
        "submission_crossover": None,
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

            score_agg = conn.execute(
                text(
                    """
                    SELECT
                      COUNT(*) FILTER (WHERE total_score IS NOT NULL)::BIGINT AS n_scored,
                      MAX(total_score) AS sc_max,
                      MIN(total_score) AS sc_min,
                      AVG(total_score) AS sc_avg,
                      STDDEV_SAMP(total_score) AS sc_stddev_samp,
                      (
                        SELECT percentile_cont(0.25) WITHIN GROUP (ORDER BY s2.total_score)::double precision
                        FROM virtual_challenge_submission_rows s2
                        WHERE s2.event_id = :eid AND s2.challenge_id = :cid
                          AND s2.total_score IS NOT NULL
                      ) AS sc_p25,
                      (
                        SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY s3.total_score)::double precision
                        FROM virtual_challenge_submission_rows s3
                        WHERE s3.event_id = :eid AND s3.challenge_id = :cid
                          AND s3.total_score IS NOT NULL
                      ) AS sc_median,
                      (
                        SELECT percentile_cont(0.75) WITHIN GROUP (ORDER BY s4.total_score)::double precision
                        FROM virtual_challenge_submission_rows s4
                        WHERE s4.event_id = :eid AND s4.challenge_id = :cid
                          AND s4.total_score IS NOT NULL
                      ) AS sc_p75
                    FROM virtual_challenge_submission_rows s
                    WHERE s.event_id = :eid AND s.challenge_id = :cid
                    """
                ),
                {"eid": int(event_id), "cid": int(challenge_id)},
            ).mappings().fetchone()
            if score_agg:
                n_scored = int(score_agg["n_scored"] or 0)
                out["submission_score_agg_n"] = n_scored

                def _score_float(v):
                    if v is None:
                        return None
                    return float(v)

                if n_scored > 0:
                    sc_max = _score_float(score_agg["sc_max"])
                    sc_min = _score_float(score_agg["sc_min"])
                    out["submission_score_max"] = sc_max
                    out["submission_score_min"] = sc_min
                    out["submission_score_avg"] = _score_float(score_agg["sc_avg"])
                    out["submission_score_median"] = _score_float(score_agg["sc_median"])
                    out["submission_score_p25"] = _score_float(score_agg["sc_p25"])
                    out["submission_score_p75"] = _score_float(score_agg["sc_p75"])
                    out["submission_score_stddev"] = _score_float(score_agg["sc_stddev_samp"])
                    if sc_max is not None and sc_min is not None:
                        out["submission_score_range"] = sc_max - sc_min

            seg_row = conn.execute(
                text(
                    f"""
                    SELECT
                      COUNT(*) FILTER (WHERE s.virtual_mdc_registration_id IS NULL)::BIGINT
                        AS seg_unknown,
                      COUNT(*) FILTER (
                        WHERE s.virtual_mdc_registration_id IS NOT NULL
                          AND lower(btrim(m.occupation)) IN (
                            'college_student', 'student'
                          )
                      )::BIGINT AS seg_student,
                      COUNT(*) FILTER (
                        WHERE s.virtual_mdc_registration_id IS NOT NULL
                          AND lower(btrim(m.occupation)) IN (
                            'professional', 'startup', 'freelance', 'freelancer'
                          )
                      )::BIGINT AS seg_professional,
                      COUNT(*) FILTER (
                        WHERE s.virtual_mdc_registration_id IS NOT NULL
                          AND (
                            m.occupation IS NULL
                            OR btrim(m.occupation) = ''
                            OR lower(btrim(m.occupation)) NOT IN (
                              'college_student', 'student',
                              'professional', 'startup', 'freelance', 'freelancer'
                            )
                          )
                      )::BIGINT AS seg_other
                    FROM virtual_challenge_submission_rows s
                    LEFT JOIN {TABLE_VIRTUAL_MDC} m ON m.id = s.virtual_mdc_registration_id
                    WHERE s.event_id = :eid AND s.challenge_id = :cid
                    """
                ),
                {"eid": int(event_id), "cid": int(challenge_id)},
            ).mappings().fetchone()
            if seg_row:
                out["team_segment_unknown"] = int(seg_row["seg_unknown"] or 0)
                out["team_segment_student"] = int(seg_row["seg_student"] or 0)
                out["team_segment_professional"] = int(seg_row["seg_professional"] or 0)
                out["team_segment_other"] = int(seg_row["seg_other"] or 0)

            _ab_sql_student = f"""
                SELECT t.ac_bucket AS ac, COUNT(*)::BIGINT AS cnt
                FROM (
                  SELECT CASE
                    WHEN s.attempts_completed IS NULL OR s.attempts_completed < 1 THEN 0
                    ELSE s.attempts_completed::INTEGER
                  END AS ac_bucket
                  FROM virtual_challenge_submission_rows s
                  INNER JOIN {TABLE_VIRTUAL_MDC} m ON m.id = s.virtual_mdc_registration_id
                  WHERE s.event_id = :eid AND s.challenge_id = :cid
                    AND lower(btrim(m.occupation)) IN ('college_student', 'student')
                ) t
                GROUP BY t.ac_bucket
                ORDER BY t.ac_bucket
                """
            _ab_sql_professional = f"""
                SELECT t.ac_bucket AS ac, COUNT(*)::BIGINT AS cnt
                FROM (
                  SELECT CASE
                    WHEN s.attempts_completed IS NULL OR s.attempts_completed < 1 THEN 0
                    ELSE s.attempts_completed::INTEGER
                  END AS ac_bucket
                  FROM virtual_challenge_submission_rows s
                  INNER JOIN {TABLE_VIRTUAL_MDC} m ON m.id = s.virtual_mdc_registration_id
                  WHERE s.event_id = :eid AND s.challenge_id = :cid
                    AND lower(btrim(m.occupation)) IN (
                      'professional', 'startup', 'freelance', 'freelancer'
                    )
                ) t
                GROUP BY t.ac_bucket
                ORDER BY t.ac_bucket
                """
            st_ab = conn.execute(text(_ab_sql_student), {"eid": int(event_id), "cid": int(challenge_id)}).mappings().all()
            out["attempt_buckets_student"] = [
                {"label": str(int(r["ac"])), "count": int(r["cnt"] or 0)} for r in st_ab
            ]
            pr_ab = conn.execute(text(_ab_sql_professional), {"eid": int(event_id), "cid": int(challenge_id)}).mappings().all()
            out["attempt_buckets_professional"] = [
                {"label": str(int(r["ac"])), "count": int(r["cnt"] or 0)} for r in pr_ab
            ]

            _score_seg_agg_base = f"""
                SELECT
                  COUNT(*)::BIGINT AS n,
                  MIN(s.total_score) AS sc_min,
                  MAX(s.total_score) AS sc_max,
                  AVG(s.total_score::double precision) AS sc_avg,
                  STDDEV_SAMP(s.total_score::double precision) AS sc_stddev,
                  percentile_cont(0.5) WITHIN GROUP (ORDER BY s.total_score::double precision) AS sc_median
                FROM virtual_challenge_submission_rows s
                INNER JOIN {TABLE_VIRTUAL_MDC} m ON m.id = s.virtual_mdc_registration_id
                WHERE s.event_id = :eid AND s.challenge_id = :cid
                  AND {{occ_filter}}
                  AND s.total_score IS NOT NULL
                """

            def _apply_score_seg(row, prefix: str) -> None:
                if not row or int(row.get("n") or 0) < 1:
                    return
                out[f"{prefix}_n"] = int(row["n"] or 0)

                def _sf(v):
                    if v is None:
                        return None
                    return float(v)

                out[f"{prefix}_min"] = _sf(row.get("sc_min"))
                out[f"{prefix}_max"] = _sf(row.get("sc_max"))
                out[f"{prefix}_avg"] = _sf(row.get("sc_avg"))
                out[f"{prefix}_median"] = _sf(row.get("sc_median"))
                out[f"{prefix}_stddev"] = _sf(row.get("sc_stddev"))

            st_sc = conn.execute(
                text(
                    _score_seg_agg_base.format(
                        occ_filter="lower(btrim(m.occupation)) IN ('college_student', 'student')"
                    )
                ),
                {"eid": int(event_id), "cid": int(challenge_id)},
            ).mappings().fetchone()
            _apply_score_seg(st_sc, "submission_score_student")

            pr_sc = conn.execute(
                text(
                    _score_seg_agg_base.format(
                        occ_filter=(
                            "lower(btrim(m.occupation)) IN ("
                            "'professional', 'startup', 'freelance', 'freelancer')"
                        )
                    )
                ),
                {"eid": int(event_id), "cid": int(challenge_id)},
            ).mappings().fetchone()
            _apply_score_seg(pr_sc, "submission_score_professional")

            try:
                xp = submission_analytics_svc.SubmissionCrossoverParams(
                    in_person_event_id=DEFAULT_IN_PERSON_EVENT_ID,
                    virtual_event_id=int(event_id),
                    virtual_challenge_ids=(int(challenge_id),),
                )
                out["submission_crossover"] = _arena_submission_crossover_summary(
                    submission_analytics_svc.load_submission_crossover(conn, xp)
                )
            except Exception as exc:  # noqa: BLE001
                out["submission_crossover"] = {"error": str(exc)}

        cohort = _virtual_challenge_submission_cohort_stats(int(event_id))
        cc = cohort.get(int(challenge_id)) or {}
        out["submission_distinct_teams"] = int(cc.get("submission_distinct_teams") or 0)
        out["submission_fresh_vs_prior_challenge"] = int(cc.get("submission_fresh_vs_prior_challenge") or 0)
        out["submission_returning_from_prior_challenge"] = int(
            cc.get("submission_returning_from_prior_challenge") or 0
        )
        out["submission_prior_challenge_id"] = cc.get("submission_prior_challenge_id")
        out["submission_prior_challenge_title"] = cc.get("submission_prior_challenge_title")
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)
    return out


def _in_person_action_center_stats(
    *,
    event_id: int,
    attendance_city: str,
    prompt_war_on: date,
    session_label: str,
) -> dict:
    """
    Session-scoped Action Center submission metrics (same response keys as
    ``_virtual_arena_challenge_stats`` for template reuse). KPIs that used
    challenge ``opens_at`` / ``closes_at`` on virtual are replaced with
    MDC session registration counts and submission rows linked to MDC.
    """
    out: dict = {
        "error": None,
        "challenge_id": None,
        "kpi_profile": "in_person_session",
        "opens_at": None,
        "closes_at": None,
        "opens_at_display": None,
        "closes_at_display": None,
        "opens_at_set": True,
        "registrations_at_open": None,
        "registrations_at_close": None,
        "total_submissions": 0,
        "unique_mdc_submissions": 0,
        "submission_distinct_teams": 0,
        "submission_fresh_vs_prior_challenge": 0,
        "submission_returning_from_prior_challenge": 0,
        "submission_prior_challenge_id": None,
        "submission_prior_challenge_title": None,
        "team_segment_student": 0,
        "team_segment_professional": 0,
        "team_segment_other": 0,
        "team_segment_unknown": 0,
        "attempt_buckets_student": [],
        "attempt_buckets_professional": [],
        "submission_score_student_n": 0,
        "submission_score_student_min": None,
        "submission_score_student_max": None,
        "submission_score_student_avg": None,
        "submission_score_student_median": None,
        "submission_score_student_stddev": None,
        "submission_score_professional_n": 0,
        "submission_score_professional_min": None,
        "submission_score_professional_max": None,
        "submission_score_professional_avg": None,
        "submission_score_professional_median": None,
        "submission_score_professional_stddev": None,
        "submission_score_agg_n": 0,
        "submission_score_min": None,
        "submission_score_max": None,
        "submission_score_avg": None,
        "submission_score_median": None,
        "submission_score_p25": None,
        "submission_score_p75": None,
        "submission_score_stddev": None,
        "submission_score_range": None,
        "submission_crossover": None,
    }
    city = (attendance_city or "").strip()
    if not city:
        out["error"] = "no attendance city"
        return out
    slab = (session_label or "").strip()[:IPCSR_SESSION_LABEL_MAX_LEN]
    base_params = {
        "eid": int(event_id),
        "acity": city[:500],
        "pwon": prompt_war_on,
        "slab": slab,
    }

    def _ipcsr_sess_sql(alias: str) -> str:
        """Session scope on submission rows; qualify columns for JOINs to MDC (both have ``event_id``)."""
        return (
            f"{alias}.event_id = :eid AND {alias}.sheet_kind = 'main' "
            f"AND lower(btrim({alias}.attendance_city)) = lower(btrim(:acity)) "
            f"AND {alias}.prompt_war_on = :pwon "
            f"AND {alias}.session_label_normalized = lower(btrim(:slab))"
        )

    def _score_float(v):
        if v is None:
            return None
        return float(v)

    try:
        with engine.connect() as conn:
            mdc_row = conn.execute(
                text(
                    f"""
                    SELECT COUNT(*)::BIGINT AS n
                    FROM {TABLE_IN_PERSON_MDC} m
                    WHERE m.event_id = :eid
                      AND lower(btrim(COALESCE(m.attendance_city, ''))) = lower(btrim(:acity))
                      AND m.prompt_war_on = :pwon
                      AND m.session_label_normalized = lower(btrim(:slab))
                    """
                ),
                base_params,
            ).mappings().fetchone()
            if mdc_row:
                out["registrations_at_open"] = int(mdc_row["n"] or 0)

            linked_row = conn.execute(
                text(
                    f"""
                    SELECT COUNT(*)::BIGINT AS n
                    FROM {TABLE_IN_PERSON_CHALLENGE_SUBMISSIONS} s
                    WHERE {_ipcsr_sess_sql("s")}
                      AND s.in_person_mdc_registration_id IS NOT NULL
                    """
                ),
                base_params,
            ).mappings().fetchone()
            if linked_row:
                out["registrations_at_close"] = int(linked_row["n"] or 0)

            sub_row = conn.execute(
                text(
                    f"""
                    SELECT COUNT(*)::BIGINT AS total,
                           COUNT(DISTINCT s.in_person_mdc_registration_id)
                             FILTER (WHERE s.in_person_mdc_registration_id IS NOT NULL)::BIGINT AS uniq_mdc
                    FROM {TABLE_IN_PERSON_CHALLENGE_SUBMISSIONS} s
                    WHERE {_ipcsr_sess_sql("s")}
                    """
                ),
                base_params,
            ).mappings().fetchone()
            if sub_row:
                out["total_submissions"] = int(sub_row["total"] or 0)
                out["unique_mdc_submissions"] = int(sub_row["uniq_mdc"] or 0)

            score_agg = conn.execute(
                text(
                    f"""
                    SELECT
                      COUNT(*) FILTER (WHERE s.total_score IS NOT NULL)::BIGINT AS n_scored,
                      MAX(s.total_score) AS sc_max,
                      MIN(s.total_score) AS sc_min,
                      AVG(s.total_score) AS sc_avg,
                      STDDEV_SAMP(s.total_score) AS sc_stddev_samp,
                      (
                        SELECT percentile_cont(0.25) WITHIN GROUP (ORDER BY s2.total_score)::double precision
                        FROM {TABLE_IN_PERSON_CHALLENGE_SUBMISSIONS} s2
                        WHERE {_ipcsr_sess_sql("s2")}
                          AND s2.total_score IS NOT NULL
                      ) AS sc_p25,
                      (
                        SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY s3.total_score)::double precision
                        FROM {TABLE_IN_PERSON_CHALLENGE_SUBMISSIONS} s3
                        WHERE {_ipcsr_sess_sql("s3")}
                          AND s3.total_score IS NOT NULL
                      ) AS sc_median,
                      (
                        SELECT percentile_cont(0.75) WITHIN GROUP (ORDER BY s4.total_score)::double precision
                        FROM {TABLE_IN_PERSON_CHALLENGE_SUBMISSIONS} s4
                        WHERE {_ipcsr_sess_sql("s4")}
                          AND s4.total_score IS NOT NULL
                      ) AS sc_p75
                    FROM {TABLE_IN_PERSON_CHALLENGE_SUBMISSIONS} s
                    WHERE {_ipcsr_sess_sql("s")}
                    """
                ),
                base_params,
            ).mappings().fetchone()
            if score_agg:
                n_scored = int(score_agg["n_scored"] or 0)
                out["submission_score_agg_n"] = n_scored
                if n_scored > 0:
                    sc_max = _score_float(score_agg["sc_max"])
                    sc_min = _score_float(score_agg["sc_min"])
                    out["submission_score_max"] = sc_max
                    out["submission_score_min"] = sc_min
                    out["submission_score_avg"] = _score_float(score_agg["sc_avg"])
                    out["submission_score_median"] = _score_float(score_agg["sc_median"])
                    out["submission_score_p25"] = _score_float(score_agg["sc_p25"])
                    out["submission_score_p75"] = _score_float(score_agg["sc_p75"])
                    out["submission_score_stddev"] = _score_float(score_agg["sc_stddev_samp"])
                    if sc_max is not None and sc_min is not None:
                        out["submission_score_range"] = sc_max - sc_min

            seg_row = conn.execute(
                text(
                    f"""
                    SELECT
                      COUNT(*) FILTER (WHERE s.in_person_mdc_registration_id IS NULL)::BIGINT
                        AS seg_unknown,
                      COUNT(*) FILTER (
                        WHERE s.in_person_mdc_registration_id IS NOT NULL
                          AND lower(btrim(m.occupation)) IN ('college_student', 'student')
                      )::BIGINT AS seg_student,
                      COUNT(*) FILTER (
                        WHERE s.in_person_mdc_registration_id IS NOT NULL
                          AND lower(btrim(m.occupation)) IN (
                            'professional', 'startup', 'freelance', 'freelancer'
                          )
                      )::BIGINT AS seg_professional,
                      COUNT(*) FILTER (
                        WHERE s.in_person_mdc_registration_id IS NOT NULL
                          AND (
                            m.occupation IS NULL
                            OR btrim(m.occupation) = ''
                            OR lower(btrim(m.occupation)) NOT IN (
                              'college_student', 'student',
                              'professional', 'startup', 'freelance', 'freelancer'
                            )
                          )
                      )::BIGINT AS seg_other
                    FROM {TABLE_IN_PERSON_CHALLENGE_SUBMISSIONS} s
                    LEFT JOIN {TABLE_IN_PERSON_MDC} m ON m.id = s.in_person_mdc_registration_id
                    WHERE {_ipcsr_sess_sql("s")}
                    """
                ),
                base_params,
            ).mappings().fetchone()
            if seg_row:
                out["team_segment_unknown"] = int(seg_row["seg_unknown"] or 0)
                out["team_segment_student"] = int(seg_row["seg_student"] or 0)
                out["team_segment_professional"] = int(seg_row["seg_professional"] or 0)
                out["team_segment_other"] = int(seg_row["seg_other"] or 0)

            _ab_sql_student = f"""
                SELECT t.ac_bucket AS ac, COUNT(*)::BIGINT AS cnt
                FROM (
                  SELECT CASE
                    WHEN s.attempts_completed IS NULL OR s.attempts_completed < 1 THEN 0
                    ELSE s.attempts_completed::INTEGER
                  END AS ac_bucket
                  FROM {TABLE_IN_PERSON_CHALLENGE_SUBMISSIONS} s
                  INNER JOIN {TABLE_IN_PERSON_MDC} m ON m.id = s.in_person_mdc_registration_id
                  WHERE {_ipcsr_sess_sql("s")}
                    AND lower(btrim(m.occupation)) IN ('college_student', 'student')
                ) t
                GROUP BY t.ac_bucket
                ORDER BY t.ac_bucket
                """
            _ab_sql_professional = f"""
                SELECT t.ac_bucket AS ac, COUNT(*)::BIGINT AS cnt
                FROM (
                  SELECT CASE
                    WHEN s.attempts_completed IS NULL OR s.attempts_completed < 1 THEN 0
                    ELSE s.attempts_completed::INTEGER
                  END AS ac_bucket
                  FROM {TABLE_IN_PERSON_CHALLENGE_SUBMISSIONS} s
                  INNER JOIN {TABLE_IN_PERSON_MDC} m ON m.id = s.in_person_mdc_registration_id
                  WHERE {_ipcsr_sess_sql("s")}
                    AND lower(btrim(m.occupation)) IN (
                      'professional', 'startup', 'freelance', 'freelancer'
                    )
                ) t
                GROUP BY t.ac_bucket
                ORDER BY t.ac_bucket
                """
            st_ab = conn.execute(text(_ab_sql_student), base_params).mappings().all()
            out["attempt_buckets_student"] = [
                {"label": str(int(r["ac"])), "count": int(r["cnt"] or 0)} for r in st_ab
            ]
            pr_ab = conn.execute(text(_ab_sql_professional), base_params).mappings().all()
            out["attempt_buckets_professional"] = [
                {"label": str(int(r["ac"])), "count": int(r["cnt"] or 0)} for r in pr_ab
            ]

            _score_seg_agg_base = f"""
                SELECT
                  COUNT(*)::BIGINT AS n,
                  MIN(s.total_score) AS sc_min,
                  MAX(s.total_score) AS sc_max,
                  AVG(s.total_score::double precision) AS sc_avg,
                  STDDEV_SAMP(s.total_score::double precision) AS sc_stddev,
                  percentile_cont(0.5) WITHIN GROUP (ORDER BY s.total_score::double precision) AS sc_median
                FROM {TABLE_IN_PERSON_CHALLENGE_SUBMISSIONS} s
                INNER JOIN {TABLE_IN_PERSON_MDC} m ON m.id = s.in_person_mdc_registration_id
                WHERE {_ipcsr_sess_sql("s")}
                  AND {{occ_filter}}
                  AND s.total_score IS NOT NULL
                """

            def _apply_score_seg(row, prefix: str) -> None:
                if not row or int(row.get("n") or 0) < 1:
                    return
                out[f"{prefix}_n"] = int(row["n"] or 0)

                def _sf(v):
                    if v is None:
                        return None
                    return float(v)

                out[f"{prefix}_min"] = _sf(row.get("sc_min"))
                out[f"{prefix}_max"] = _sf(row.get("sc_max"))
                out[f"{prefix}_avg"] = _sf(row.get("sc_avg"))
                out[f"{prefix}_median"] = _sf(row.get("sc_median"))
                out[f"{prefix}_stddev"] = _sf(row.get("sc_stddev"))

            st_sc = conn.execute(
                text(
                    _score_seg_agg_base.format(
                        occ_filter="lower(btrim(m.occupation)) IN ('college_student', 'student')"
                    )
                ),
                base_params,
            ).mappings().fetchone()
            _apply_score_seg(st_sc, "submission_score_student")

            pr_sc = conn.execute(
                text(
                    _score_seg_agg_base.format(
                        occ_filter=(
                            "lower(btrim(m.occupation)) IN ("
                            "'professional', 'startup', 'freelance', 'freelancer')"
                        )
                    )
                ),
                base_params,
            ).mappings().fetchone()
            _apply_score_seg(pr_sc, "submission_score_professional")

            pick = conn.execute(
                text(
                    f"""
                    SELECT o.prev_id, o.prev_title
                    FROM (
                      SELECT id,
                             LAG(id) OVER (
                               PARTITION BY event_id
                               ORDER BY prompt_war_on ASC NULLS FIRST,
                                        lower(trim(COALESCE(session_label, ''))) ASC,
                                        id ASC
                             ) AS prev_id,
                             LAG(display_name) OVER (
                               PARTITION BY event_id
                               ORDER BY prompt_war_on ASC NULLS FIRST,
                                        lower(trim(COALESCE(session_label, ''))) ASC,
                               id ASC
                             ) AS prev_title,
                             city,
                             prompt_war_on,
                             session_label
                      FROM {TABLE_IN_PERSON_PW_SESSIONS}
                      WHERE event_id = :eid
                    ) o
                    WHERE lower(trim(o.city)) = lower(trim(:acity))
                      AND o.prompt_war_on = :pwon
                      AND lower(trim(COALESCE(o.session_label, ''))) = lower(trim(COALESCE(:slab, '')))
                    LIMIT 1
                    """
                ),
                base_params,
            ).mappings().fetchone()
            prev_id = int(pick["prev_id"]) if pick and pick.get("prev_id") is not None else None
            out["submission_prior_challenge_title"] = (
                str(pick["prev_title"]).strip() if pick and pick.get("prev_title") else None
            )
            out["submission_prior_challenge_id"] = prev_id

            cur_ct = conn.execute(
                text(
                    f"""
                    SELECT COUNT(DISTINCT s.leader_email_normalized)::BIGINT AS n
                    FROM {TABLE_IN_PERSON_CHALLENGE_SUBMISSIONS} s
                    WHERE {_ipcsr_sess_sql("s")}
                      AND s.leader_email_normalized IS NOT NULL
                    """
                ),
                base_params,
            ).scalar()
            distinct_teams = int(cur_ct or 0)
            out["submission_distinct_teams"] = distinct_teams

            if prev_id is None or distinct_teams == 0:
                out["submission_fresh_vs_prior_challenge"] = distinct_teams
                out["submission_returning_from_prior_challenge"] = 0
            else:
                cohort_row = conn.execute(
                    text(
                        f"""
                        WITH cur AS (
                          SELECT DISTINCT s.leader_email_normalized AS em
                          FROM {TABLE_IN_PERSON_CHALLENGE_SUBMISSIONS} s
                          WHERE {_ipcsr_sess_sql("s")}
                            AND s.leader_email_normalized IS NOT NULL
                        ),
                        prev AS (
                          SELECT DISTINCT p.leader_email_normalized AS em
                          FROM {TABLE_IN_PERSON_CHALLENGE_SUBMISSIONS} p
                          INNER JOIN {TABLE_IN_PERSON_PW_SESSIONS} ps ON ps.id = :prev_id
                          WHERE p.event_id = :eid AND p.sheet_kind = 'main'
                            AND p.attendance_city_normalized = lower(btrim(ps.city))
                            AND p.prompt_war_on = ps.prompt_war_on
                            AND p.session_label_normalized = lower(btrim(COALESCE(ps.session_label, '')))
                            AND p.leader_email_normalized IS NOT NULL
                        )
                        SELECT
                          (SELECT COUNT(*)::bigint FROM cur c
                             WHERE EXISTS (SELECT 1 FROM prev p WHERE p.em = c.em)) AS ret,
                          (SELECT COUNT(*)::bigint FROM cur c
                             WHERE NOT EXISTS (SELECT 1 FROM prev p WHERE p.em = c.em)) AS fresh
                        """
                    ),
                    {**base_params, "prev_id": prev_id},
                ).mappings().fetchone()
                if cohort_row:
                    out["submission_returning_from_prior_challenge"] = int(cohort_row["ret"] or 0)
                    out["submission_fresh_vs_prior_challenge"] = int(cohort_row["fresh"] or 0)

            try:
                xp = submission_analytics_svc.SubmissionCrossoverParams(
                    in_person_event_id=int(event_id),
                    virtual_event_id=DEFAULT_VIRTUAL_EVENT_ID,
                    ip_sheet_kind="main",
                    ip_attendance_city=city,
                    ip_prompt_war_on=prompt_war_on,
                    ip_session_label_normalized=slab,
                    virtual_challenge_ids=(),
                )
                out["submission_crossover"] = _arena_submission_crossover_summary(
                    submission_analytics_svc.load_submission_crossover(conn, xp)
                )
            except Exception as exc:  # noqa: BLE001
                out["submission_crossover"] = {"error": str(exc)}
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
        if not PW_GLOBAL_LEADERBOARDS_ENABLED and v_lb_scope == "global":
            v_lb_scope = "arena"
            v_ov_cid = None
        if v_lb_scope == "arena" and v_ov_cid is not None:
            challenge_id = int(v_ov_cid)
    else:
        _v_lb_default = "arena" if not PW_GLOBAL_LEADERBOARDS_ENABLED else "global"
        raw_v_lb = (request.args.get("vLbScope") or _v_lb_default).strip().lower()
        v_lb_scope = raw_v_lb if raw_v_lb in ("global", "arena") else _v_lb_default
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
    if not PW_GLOBAL_LEADERBOARDS_ENABLED and ip_lb_scope == "global" and ip_ac_opts:
        o0 = _default_in_person_pw_session_for_redirect(ip_ac_opts) or ip_ac_opts[0]
        focus_city_raw = str(o0.get("city") or "").strip() or None
        iso = str(o0.get("prompt_war_on_iso") or "").strip()
        lab = str(o0.get("session_label") or "")
        raw_sess = f"{iso}|{lab}" if lab else iso
        ip_lb_scope = "city"
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
    ip_overview_options: list[dict[str, str]] = []
    if PW_GLOBAL_LEADERBOARDS_ENABLED:
        ip_overview_options.append({"value": "global", "label": "Global (main challenge)"})
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
    virtual_overview_options: list[dict[str, str]] = []
    if PW_GLOBAL_LEADERBOARDS_ENABLED:
        virtual_overview_options.append({"value": "global", "label": "Global (all arenas)"})
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
    if v_lb_scope == "global" and PW_GLOBAL_LEADERBOARDS_ENABLED:
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
        global_leaderboards_enabled=PW_GLOBAL_LEADERBOARDS_ENABLED,
    )


@app.get("/api/in-person/sessions")
def api_in_person_sessions_list():
    eid = request.args.get("event_id", type=int)
    if not eid:
        return jsonify({"error": "event_id is required"}), 400
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    f"""
                    SELECT
                      s.id,
                      s.city,
                      s.prompt_war_on,
                      s.session_label,
                      s.scope_key,
                      s.display_name,
                      (SELECT COUNT(*)::bigint FROM {TABLE_IN_PERSON_MDC} r WHERE r.pw_session_id = s.id) AS n_mdc,
                      (SELECT COUNT(*)::bigint FROM {TABLE_IN_PERSON_CHALLENGE_SUBMISSIONS} c
                       WHERE c.pw_session_id = s.id) AS n_csr
                    FROM {TABLE_IN_PERSON_PW_SESSIONS} s
                    WHERE s.event_id = :eid
                    ORDER BY s.prompt_war_on DESC, s.city, s.session_label
                    """
                ),
                {"eid": int(eid)},
            ).mappings().all()
    except Exception as exc:  # noqa: BLE001
        if _is_missing_in_person_pw_sessions_table(exc):
            app.logger.warning(
                "sessions list: in_person_pw_sessions missing — apply database/migrate_sessions.sql"
            )
            return jsonify(
                {
                    "event_id": int(eid),
                    "sessions": [],
                    "migration_required": True,
                    "hint": "Run database/migrate_sessions.sql against this database (same DATABASE_URL as the app).",
                }
            )
        app.logger.warning("sessions list failed: %s", exc)
        return jsonify({"error": str(exc)}), 500
    out = []
    for r in rows:
        pwo = r["prompt_war_on"]
        if isinstance(pwo, datetime):
            pwo = pwo.date()
        n_mdc = int(r.get("n_mdc") or 0)
        n_csr = int(r.get("n_csr") or 0)
        out.append(
            {
                "id": int(r["id"]),
                "city": str(r.get("city") or ""),
                "prompt_war_on": pwo.isoformat() if isinstance(pwo, date) else str(pwo)[:10],
                "session_label": str(r.get("session_label") or ""),
                "scope_key": str(r.get("scope_key") or ""),
                "display_name": str(r.get("display_name") or ""),
                "has_data": (n_mdc + n_csr) > 0,
            }
        )
    return jsonify({"event_id": int(eid), "sessions": out})


@app.post("/api/in-person/sessions")
def api_in_person_sessions_create():
    body = request.get_json(silent=True) or {}
    eid = body.get("event_id")
    city_raw = (body.get("city") or "").strip()
    pwo_raw = body.get("prompt_war_on")
    slab = str(body.get("session_label") or "").strip()[:IPCSR_SESSION_LABEL_MAX_LEN]
    if eid is None or not city_raw or not pwo_raw:
        return jsonify({"error": "event_id, city, and prompt_war_on are required"}), 400
    try:
        pwo = date.fromisoformat(str(pwo_raw).strip()[:10])
    except ValueError:
        return jsonify({"error": "prompt_war_on must be YYYY-MM-DD"}), 400
    rej = _reject_legacy_prompt_war_on_date(pwo)
    if rej:
        return rej
    city_n = city_raw.strip().lower()
    try:
        with engine.connect() as conn:
            ev = conn.execute(
                text("SELECT id, kind FROM events WHERE id = :id"),
                {"id": int(eid)},
            ).fetchone()
            if not ev or str(ev[1]) != "in_person":
                return jsonify({"error": "event must be in_person"}), 400
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500
    ins_sql = text(
        f"""
        INSERT INTO {TABLE_IN_PERSON_PW_SESSIONS} (event_id, city, prompt_war_on, session_label)
        VALUES (:event_id, :city, :prompt_war_on, :session_label)
        ON CONFLICT (event_id, city, prompt_war_on, session_label) DO NOTHING
        RETURNING id, city, prompt_war_on, session_label, scope_key, display_name
        """
    )
    try:
        with engine.begin() as conn:
            row = conn.execute(
                ins_sql,
                {
                    "event_id": int(eid),
                    "city": city_n,
                    "prompt_war_on": pwo,
                    "session_label": slab,
                },
            ).mappings().first()
            if row:
                pw_invalidate_read_caches()
                rpwo = row["prompt_war_on"]
                if isinstance(rpwo, datetime):
                    rpwo = rpwo.date()
                return (
                    jsonify(
                        {
                            "id": int(row["id"]),
                            "city": str(row.get("city") or ""),
                            "prompt_war_on": rpwo.isoformat() if isinstance(rpwo, date) else str(rpwo)[:10],
                            "session_label": str(row.get("session_label") or ""),
                            "scope_key": str(row.get("scope_key") or ""),
                            "display_name": str(row.get("display_name") or ""),
                        }
                    ),
                    201,
                )
            ex = conn.execute(
                text(
                    f"""
                    SELECT id, display_name
                    FROM {TABLE_IN_PERSON_PW_SESSIONS}
                    WHERE event_id = :event_id AND city = :city
                      AND prompt_war_on = :prompt_war_on AND session_label = :session_label
                    """
                ),
                {
                    "event_id": int(eid),
                    "city": city_n,
                    "prompt_war_on": pwo,
                    "session_label": slab,
                },
            ).mappings().first()
            if not ex:
                return jsonify({"error": "Session already exists"}), 409
            return (
                jsonify(
                    {
                        "error": "Session already exists",
                        "existing": {"id": int(ex["id"]), "display_name": str(ex.get("display_name") or "")},
                    }
                ),
                409,
            )
    except IntegrityError as exc:
        app.logger.warning("sessions create conflict: %s", exc)
        return jsonify({"error": "Session already exists"}), 409
    except Exception as exc:  # noqa: BLE001
        if _is_missing_in_person_pw_sessions_table(exc):
            return jsonify(
                {
                    "error": "in_person_pw_sessions table is missing.",
                    "hint": "psql with your DATABASE_URL: \\i database/migrate_sessions.sql",
                }
            ), 503
        app.logger.warning("sessions create failed: %s", exc)
        return jsonify({"error": str(exc)}), 500


@app.delete("/api/in-person/sessions/<int:session_id>")
def api_in_person_sessions_delete(session_id: int):
    eid = request.args.get("event_id", type=int)
    if not eid:
        return jsonify({"error": "event_id is required"}), 400
    sid = int(session_id)
    try:
        with engine.begin() as conn:
            exists = conn.execute(
                text(
                    f"SELECT 1 FROM {TABLE_IN_PERSON_PW_SESSIONS} WHERE id = :sid AND event_id = :eid"
                ),
                {"sid": sid, "eid": int(eid)},
            ).scalar_one_or_none()
            if exists is None:
                return jsonify({"error": "Session not found"}), 404
            n_snap = conn.execute(
                text(
                    """
                    UPDATE hawkeye_rsvp_snapshots
                    SET pw_session_id = NULL
                    WHERE event_id = :eid AND pw_session_id = :sid
                    """
                ),
                {"eid": int(eid), "sid": sid},
            ).rowcount
            n_mdc = conn.execute(
                text(
                    f"""
                    UPDATE {TABLE_IN_PERSON_MDC}
                    SET pw_session_id = NULL
                    WHERE event_id = :eid AND pw_session_id = :sid
                    """
                ),
                {"eid": int(eid), "sid": sid},
            ).rowcount
            n_csr = conn.execute(
                text(
                    f"""
                    UPDATE {TABLE_IN_PERSON_CHALLENGE_SUBMISSIONS}
                    SET pw_session_id = NULL
                    WHERE event_id = :eid AND pw_session_id = :sid
                    """
                ),
                {"eid": int(eid), "sid": sid},
            ).rowcount
            n_map = conn.execute(
                text(
                    """
                    UPDATE event_external_mappings
                    SET pw_session_id = NULL
                    WHERE event_id = :eid AND pw_session_id = :sid
                    """
                ),
                {"eid": int(eid), "sid": sid},
            ).rowcount
            res = conn.execute(
                text(
                    f"DELETE FROM {TABLE_IN_PERSON_PW_SESSIONS} WHERE id = :sid AND event_id = :eid RETURNING id"
                ),
                {"sid": sid, "eid": int(eid)},
            ).scalar_one_or_none()
            if res is None:
                return jsonify({"error": "Session not found"}), 404
        pw_invalidate_read_caches()
        return (
            jsonify(
                {
                    "ok": True,
                    "id": sid,
                    "unlinked": {
                        "hawkeye_snapshots": int(n_snap or 0),
                        "mdc_registrations": int(n_mdc or 0),
                        "challenge_submissions": int(n_csr or 0),
                        "hawkeye_mappings": int(n_map or 0),
                    },
                }
            ),
            200,
        )
    except Exception as exc:  # noqa: BLE001
        if _is_missing_in_person_pw_sessions_table(exc):
            return jsonify(
                {
                    "error": "in_person_pw_sessions table is missing.",
                    "hint": "psql with your DATABASE_URL: \\i database/migrate_sessions.sql",
                }
            ), 503
        app.logger.warning("sessions delete failed: %s", exc)
        return jsonify({"error": str(exc)}), 500


@app.post("/api/in-person/hawkeye/mapping")
def api_in_person_hawkeye_mapping_save():
    body = request.get_json(silent=True) or {}
    eid = body.get("event_id")
    event_tag = (body.get("event_tag") or "").strip()
    if eid is None or not event_tag:
        return jsonify({"error": "event_id and event_tag are required"}), 400
    notes = body.get("notes")
    notes_s = notes.strip() if isinstance(notes, str) else None
    if notes_s == "":
        notes_s = None
    pw_sid = body.get("pw_session_id")
    try:
        pw_int = int(pw_sid) if pw_sid is not None and str(pw_sid).strip() != "" else None
    except (TypeError, ValueError):
        return jsonify({"error": "pw_session_id must be an integer"}), 400
    try:
        row = hawkeye_service.save_mapping(
            engine, int(eid), event_tag, notes=notes_s, pw_session_id=pw_int
        )
        pw_invalidate_read_caches()
        return jsonify(row)
    except HawkeyeError as exc:
        app.logger.warning("hawkeye mapping save failed: %s", exc)
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001
        app.logger.warning("hawkeye mapping save failed: %s", exc)
        return jsonify({"error": str(exc)}), 500


@app.get("/api/in-person/hawkeye/mapping")
def api_in_person_hawkeye_mapping_get():
    eid = request.args.get("event_id", type=int)
    if not eid:
        return jsonify({"error": "event_id is required"}), 400
    pw_sid = request.args.get("pw_session_id", type=int)
    try:
        if pw_sid:
            m = hawkeye_service.get_mapping(engine, int(eid), pw_session_id=int(pw_sid))
        else:
            m = hawkeye_service.get_mapping(engine, int(eid))
        if not m:
            return jsonify({"configured": False})
        return jsonify(m)
    except Exception as exc:  # noqa: BLE001
        app.logger.warning("hawkeye mapping get failed: %s", exc)
        return jsonify({"error": str(exc)}), 500


@app.post("/api/in-person/hawkeye/sync")
def api_in_person_hawkeye_sync():
    body = request.get_json(silent=True) or {}
    eid = body.get("event_id")
    if eid is None:
        return jsonify({"error": "event_id is required"}), 400
    pw_sid = body.get("pw_session_id")
    try:
        pw_int = int(pw_sid) if pw_sid is not None and str(pw_sid).strip() != "" else None
    except (TypeError, ValueError):
        return jsonify({"error": "pw_session_id must be an integer"}), 400
    try:
        out = hawkeye_service.sync_event(
            engine,
            int(eid),
            triggered_by="manual",
            pw_session_id=pw_int,
            invalidate_caches=pw_invalidate_read_caches,
        )
        return jsonify(out)
    except HawkeyeNotConfiguredError as exc:
        app.logger.warning("hawkeye sync not configured: %s", exc)
        return jsonify({"error": str(exc)}), 404
    except HawkeyeError as exc:
        app.logger.warning("hawkeye sync failed: %s", exc)
        return jsonify({"error": str(exc)}), 502
    except Exception as exc:  # noqa: BLE001
        app.logger.warning("hawkeye sync unexpected error: %s", exc)
        return jsonify({"error": str(exc)}), 500


@app.get("/api/in-person/hawkeye/stats")
def api_in_person_hawkeye_stats():
    eid = request.args.get("event_id", type=int)
    if not eid:
        return jsonify({"error": "event_id is required"}), 400
    pw_sid = request.args.get("pw_session_id", type=int)
    try:
        if pw_sid:
            if not hawkeye_service.get_mapping(engine, int(eid), pw_session_id=int(pw_sid)):
                return jsonify({"configured": False})
            snap = hawkeye_service.get_latest_snapshot(engine, int(eid), pw_session_id=int(pw_sid))
        else:
            if not hawkeye_service.get_mapping(engine, int(eid)):
                return jsonify({"configured": False})
            snap = hawkeye_service.get_latest_snapshot(engine, int(eid))
        if not snap:
            return jsonify({"configured": True, "has_snapshot": False})
        return jsonify({"configured": True, "has_snapshot": True, **snap})
    except Exception as exc:  # noqa: BLE001
        app.logger.warning("hawkeye stats failed: %s", exc)
        return jsonify({"error": str(exc)}), 500


@app.get("/api/in-person/hawkeye/events")
def api_in_person_hawkeye_events():
    """
    Per-PW-session Hawkeye rows for one in-person event (defaults to
    ``DEFAULT_IN_PERSON_EVENT_ID``). Each row carries the PW-session display
    label, current ``external_key`` (if mapped) and latest snapshot stats.
    """
    eid = request.args.get("event_id", type=int) or DEFAULT_IN_PERSON_EVENT_ID
    try:
        sessions = _in_person_pw_options(int(eid))
        with engine.connect() as conn:
            rows = hawkeye_service.list_pw_session_rows(engine, int(eid), sessions)
            rows = _overlay_manual_rsvp_on_hawkeye_event_rows(conn, int(eid), rows)
        return jsonify({"event_id": int(eid), "events": rows})
    except Exception as exc:  # noqa: BLE001
        app.logger.warning("hawkeye list events failed: %s", exc)
        return jsonify({"error": str(exc)}), 500


@app.post("/api/in-person/hawkeye/fetch")
def api_in_person_hawkeye_fetch():
    """Save the Hawkeye event tag for a PW session and immediately fetch its stats."""
    body = request.get_json(silent=True) or {}
    eid = body.get("event_id")
    event_tag = (body.get("event_tag") or "").strip()
    if eid is None or not event_tag:
        return jsonify({"error": "event_id and event_tag are required"}), 400
    scope_key = body.get("scope_key")
    scope_key = (scope_key or "").strip() if isinstance(scope_key, str) else ""
    scope_obj = body.get("scope")
    scope_dict = scope_obj if isinstance(scope_obj, dict) else None
    notes = body.get("notes")
    notes_s = notes.strip() if isinstance(notes, str) else None
    if notes_s == "":
        notes_s = None
    pw_sid = body.get("pw_session_id")
    try:
        pw_int = int(pw_sid) if pw_sid is not None and str(pw_sid).strip() != "" else None
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "pw_session_id must be an integer"}), 400
    try:
        out = hawkeye_service.save_mapping_and_sync(
            engine,
            int(eid),
            event_tag,
            triggered_by="manual",
            scope_key=scope_key,
            scope=scope_dict,
            notes=notes_s,
            pw_session_id=pw_int,
            invalidate_caches=pw_invalidate_read_caches,
        )
        return jsonify({"ok": True, **out})
    except HawkeyeNotConfiguredError as exc:
        app.logger.warning("hawkeye fetch not configured: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 404
    except HawkeyeError as exc:
        app.logger.warning("hawkeye fetch failed: %s", exc)
        return jsonify({"ok": False, "error": str(exc), "status_code": exc.status_code}), 502
    except Exception as exc:  # noqa: BLE001
        app.logger.warning("hawkeye fetch unexpected error: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.get("/api/in-person/main-data-center/stats")
def api_in_person_mdc_stats():
    """JSON payload for Main Data Center charts (optional ``mdc_date_from`` / ``mdc_date_to``, IST dates)."""
    eid = request.args.get("inPersonEventId", type=int) or DEFAULT_IN_PERSON_EVENT_ID
    v_cross = request.args.get("crossoverVirtualEventId", type=int)
    d0 = _parse_mdc_dashboard_iso_date(request.args.get("mdc_date_from"))
    d1 = _parse_mdc_dashboard_iso_date(request.args.get("mdc_date_to"))
    ip_cx = int(eid) if v_cross and int(v_cross) > 0 and int(eid) > 0 else None
    v_cx = int(v_cross) if v_cross and int(v_cross) > 0 and int(eid) > 0 else None
    payload = _load_mdc_stats(
        eid,
        mode="in_person",
        date_from=d0,
        date_to=d1,
        mdc_crossover_in_person_event_id=ip_cx,
        mdc_crossover_virtual_event_id=v_cx,
    )
    return jsonify(payload)


@app.get("/api/virtual/main-data-center/stats")
def api_virtual_mdc_stats():
    """JSON payload for Virtual Main Data Center charts (optional ``mdc_date_from`` / ``mdc_date_to``, IST dates)."""
    eid = request.args.get("virtualEventId", type=int) or DEFAULT_VIRTUAL_EVENT_ID
    ip_cross = request.args.get("crossoverInPersonEventId", type=int)
    d0 = _parse_mdc_dashboard_iso_date(request.args.get("mdc_date_from"))
    d1 = _parse_mdc_dashboard_iso_date(request.args.get("mdc_date_to"))
    v_cx = int(eid) if ip_cross and int(ip_cross) > 0 and int(eid) > 0 else None
    ip_cx = int(ip_cross) if ip_cross and int(ip_cross) > 0 and int(eid) > 0 else None
    payload = _load_mdc_stats(
        eid,
        mode="virtual",
        date_from=d0,
        date_to=d1,
        mdc_crossover_in_person_event_id=ip_cx,
        mdc_crossover_virtual_event_id=v_cx,
    )
    return jsonify(payload)


@app.post("/api/import/virtual/challenge-attempts/preview")
def api_import_virtual_challenge_attempts_preview():
    return _preview_challenge_attempt_counts_core("virtual_challenge_attempts")


@app.post("/api/import/virtual/challenge-attempts")
def api_import_virtual_challenge_attempts():
    return _import_virtual_challenge_attempts_core()


@app.post("/api/import/in-person/challenge-attempts/preview")
def api_import_in_person_challenge_attempts_preview():
    return _preview_challenge_attempt_counts_core("in_person_challenge_attempts")


@app.post("/api/import/in-person/challenge-attempts")
def api_import_in_person_challenge_attempts():
    return _import_in_person_challenge_attempts_core()


@app.post("/admin/import/in-person/challenge-attempts")
def admin_import_in_person_challenge_attempts():
    nav_eid = request.form.get("inPersonEventId", type=int) or DEFAULT_IN_PERSON_EVENT_ID
    back_import_url = url_for("in_person_import", inPersonEventId=nav_eid)
    back_dashboard_url = url_for("in_person_page", inPersonEventId=nav_eid)
    out = _import_in_person_challenge_attempts_core()
    resp, status = out if isinstance(out, tuple) else (out, 200)
    payload = resp.get_json(silent=True) if hasattr(resp, "get_json") else None
    if status != 200:
        msg = (payload or {}).get("error") if isinstance(payload, dict) else str(payload)
        return (
            render_template(
                "challenge_attempts_patch_import_result.html",
                title="In-person challenge attempts import",
                ok=False,
                message=msg or "Import failed",
                stats=None,
                error_detail=payload if isinstance(payload, dict) else None,
                back_import_url=back_import_url,
                back_dashboard_url=back_dashboard_url,
                back_import_label="Back to In-person import",
                back_dashboard_label="In-person dashboard",
            ),
            status,
        )
    stats = payload if isinstance(payload, dict) else {}
    ok_eid = int(stats.get("in_person_event_id") or nav_eid)
    return render_template(
        "challenge_attempts_patch_import_result.html",
        title="In-person challenge attempts import",
        ok=True,
        message="Attempts updated on in-person submission rows",
        stats=stats,
        error_detail=None,
        back_import_url=url_for("in_person_import", inPersonEventId=ok_eid),
        back_dashboard_url=url_for("in_person_page", inPersonEventId=ok_eid),
        back_import_label="Back to In-person import",
        back_dashboard_label="In-person dashboard",
    )


@app.post("/admin/import/virtual/challenge-attempts")
def admin_import_virtual_challenge_attempts():
    out = _import_virtual_challenge_attempts_core()
    resp, status = out if isinstance(out, tuple) else (out, 200)
    payload = resp.get_json(silent=True) if hasattr(resp, "get_json") else None
    if status != 200:
        msg = (payload or {}).get("error") if isinstance(payload, dict) else str(payload)
        return (
            render_template(
                "virtual_challenge_submissions_import_result.html",
                title="Virtual challenge attempts import",
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
        title="Virtual challenge attempts import",
        ok=True,
        message="Attempts updated on submission rows",
        stats=stats,
        error_detail=None,
    )


@app.get("/in-person")
def in_person_page():
    in_person_event_id = request.args.get("inPersonEventId", type=int) or DEFAULT_IN_PERSON_EVENT_ID
    virtual_event_id = request.args.get("virtualEventId", type=int) or DEFAULT_VIRTUAL_EVENT_ID
    challenge_id = request.args.get("challengeId", type=int) or DEFAULT_CHALLENGE_ID
    mdc_df = _parse_mdc_dashboard_iso_date(request.args.get("mdc_date_from"))
    mdc_dt = _parse_mdc_dashboard_iso_date(request.args.get("mdc_date_to"))
    _ip_cx, _v_cx = (
        (int(in_person_event_id), int(virtual_event_id))
        if int(in_person_event_id) > 0 and int(virtual_event_id) > 0
        else (None, None)
    )
    data_center = _load_mdc_stats(
        in_person_event_id,
        mode="in_person",
        date_from=mdc_df,
        date_to=mdc_dt,
        mdc_crossover_in_person_event_id=_ip_cx,
        mdc_crossover_virtual_event_id=_v_cx,
    )
    ip_ac_city = (request.args.get("ipActionCenterCity") or "").strip() or None
    ip_pw_date = _parse_ipcsr_prompt_war_date_from_form(request.args.get("ipPromptWarDate"))
    ip_pw_label = _normalize_ipcsr_session_label(request.args.get("ipPromptWarLabel"))
    _ip_pws = _in_person_pw_options(in_person_event_id)
    if not PW_GLOBAL_LEADERBOARDS_ENABLED and not ip_ac_city and _ip_pws:
        pw0 = _default_in_person_pw_session_for_redirect(_ip_pws) or _ip_pws[0]
        return redirect(
            url_for(
                "in_person_page",
                inPersonEventId=in_person_event_id,
                virtualEventId=virtual_event_id,
                challengeId=challenge_id,
                ipActionCenterCity=pw0["city"],
                ipPromptWarDate=pw0["prompt_war_on_iso"],
                ipPromptWarLabel=pw0.get("session_label") or "",
                mdc_date_from=request.args.get("mdc_date_from"),
                mdc_date_to=request.args.get("mdc_date_to"),
            )
        )
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
    ip_arena_stats = None
    ip_submission_session_token_analytics = ""
    if ip_ac_city and ip_pw_date:
        ip_arena_stats = _in_person_action_center_stats(
            event_id=in_person_event_id,
            attendance_city=ip_ac_city,
            prompt_war_on=ip_pw_date,
            session_label=ip_pw_label or "",
        )
        ip_submission_session_token_analytics = _encode_ip_submission_session_token(
            ip_ac_city, ip_pw_date, ip_pw_label or ""
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
        ip_arena_stats=ip_arena_stats,
        ip_submission_session_token_analytics=ip_submission_session_token_analytics,
        global_leaderboards_enabled=PW_GLOBAL_LEADERBOARDS_ENABLED,
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
    _ip_pws_lb = _in_person_pw_options(in_person_event_id)
    if not PW_GLOBAL_LEADERBOARDS_ENABLED and not ip_ac_city and _ip_pws_lb:
        pw0 = _default_in_person_pw_session_for_redirect(_ip_pws_lb) or _ip_pws_lb[0]
        return redirect(
            url_for(
                "in_person_leaderboard",
                inPersonEventId=in_person_event_id,
                virtualEventId=virtual_event_id,
                challengeId=challenge_id,
                ipActionCenterCity=pw0["city"],
                ipPromptWarDate=pw0["prompt_war_on_iso"],
                ipPromptWarLabel=pw0.get("session_label") or "",
                page=page,
                per_page=per_page,
            )
        )
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
        global_leaderboards_enabled=PW_GLOBAL_LEADERBOARDS_ENABLED,
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
    _ip_cx, _v_cx = (
        (int(in_person_event_id), int(virtual_event_id))
        if int(in_person_event_id) > 0 and int(virtual_event_id) > 0
        else (None, None)
    )
    data_center = _load_mdc_stats(
        virtual_event_id,
        mode="virtual",
        date_from=mdc_df,
        date_to=mdc_dt,
        mdc_crossover_in_person_event_id=_ip_cx,
        mdc_crossover_virtual_event_id=_v_cx,
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
    if not PW_GLOBAL_LEADERBOARDS_ENABLED and standings_is_global:
        if challenge_id is not None and (not id_set_int or int(challenge_id) in id_set_int):
            standings_is_global = False
            standings_challenge_id = int(challenge_id)
            standings_view_value = str(int(challenge_id))
        elif id_set_int:
            cid0 = sorted(id_set_int)[0]
            standings_is_global = False
            standings_challenge_id = cid0
            standings_view_value = str(cid0)
        else:
            standings_is_global = False
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
        global_leaderboards_enabled=PW_GLOBAL_LEADERBOARDS_ENABLED,
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
    if is_global and not PW_GLOBAL_LEADERBOARDS_ENABLED:
        if challenges:
            q = request.args.to_dict(flat=True)
            q.pop("global", None)
            q["arenaChallengeId"] = str(int(challenges[0]["id"]))
            return redirect(f"{request.path}?{urlencode(q)}")
        return redirect(
            url_for("virtual_page", virtualEventId=virtual_event_id, inPersonEventId=in_person_event_id)
        )
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
        global_leaderboards_enabled=PW_GLOBAL_LEADERBOARDS_ENABLED,
    )


@app.get("/overview/settings")
def overview_settings():
    return render_template(
        "module_sheet.html",
        title="Overview · Settings",
        description="Workspace defaults, API keys, and operational tools for the whole program.",
        show_admin_link=True,
    )


@app.get("/overview/submission-analytics")
@audit_view(entity="overview_submission_analytics", action="VIEW", module="overview")
def overview_submission_analytics():
    in_person_event_id = request.args.get("inPersonEventId", type=int) or DEFAULT_IN_PERSON_EVENT_ID
    virtual_event_id = request.args.get("virtualEventId", type=int) or DEFAULT_VIRTUAL_EVENT_ID
    challenges = _load_virtual_challenges_brief(virtual_event_id)
    return render_template(
        "overview_submission_analytics.html",
        title="Overview · Submission crossover",
        in_person_event_id=in_person_event_id,
        virtual_event_id=virtual_event_id,
        virtual_challenges=challenges,
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
    mdc_pw_raw = (request.args.get("mdc_pw_on") or "").strip()
    mdc_pw_d = _parse_ipcsr_prompt_war_date_from_form(mdc_pw_raw) if mdc_pw_raw else None
    if mdc_pw_d is not None:
        rej_u = _reject_legacy_prompt_war_on_date(mdc_pw_d)
        if rej_u:
            return rej_u
    mdc_sl = (request.args.get("mdc_session_label") or "").strip()[:200] or None
    sk, sd = _parse_mdc_users_roster_sort(request.args, mode="in_person")
    users = _load_mdc_users_page(
        DEFAULT_IN_PERSON_EVENT_ID,
        page,
        per_page,
        q,
        ac,
        advanced=advanced,
        mdc_pw_on=mdc_pw_d,
        mdc_session_label=mdc_sl,
        roster_sort_key=sk,
        roster_sort_dir=sd,
    )
    return render_template(
        "in_person_users.html",
        title="In-person · Users",
        users=users,
        in_person_event_id=DEFAULT_IN_PERSON_EVENT_ID,
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
                "participated_challenge_id",
                "submission_session",
                "arenaChallengeId",
                "arenaTeamSegment",
                "arenaAttemptsCompleted",
                "sort",
                "sort_dir",
            )
        )[:40],
    },
)
def in_person_users_export_csv():
    q = (request.args.get("q") or "").strip()[:200]
    ac_raw = request.args.get("attendance_city")
    ac = (ac_raw or "").strip()[:200] or None
    advanced = _parse_mdc_users_advanced_from_request(request.args)
    mdc_pw_raw = (request.args.get("mdc_pw_on") or "").strip()
    mdc_pw_d = _parse_ipcsr_prompt_war_date_from_form(mdc_pw_raw) if mdc_pw_raw else None
    if mdc_pw_d is not None:
        rej_x = _reject_legacy_prompt_war_on_date(mdc_pw_d)
        if rej_x:
            return rej_x
    mdc_sl = (request.args.get("mdc_session_label") or "").strip()[:200] or None
    sk, sd = _parse_mdc_users_roster_sort(request.args, mode="in_person")
    rows, err = _fetch_mdc_users_export_rows(
        DEFAULT_IN_PERSON_EVENT_ID,
        q,
        ac,
        advanced=advanced,
        mdc_pw_on=mdc_pw_d,
        mdc_session_label=mdc_sl,
        roster_sort_key=sk,
        roster_sort_dir=sd,
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
    in_person_event_id = request.args.get("inPersonEventId", type=int) or DEFAULT_IN_PERSON_EVENT_ID
    return render_template(
        "in_person_settings.html",
        title="In-person · Settings",
        description="Funnel thresholds, import schedules, and city configuration.",
        in_person_event_id=in_person_event_id,
    )


@app.get("/virtual/users")
def virtual_users():
    page = request.args.get("page", default=1, type=int) or 1
    per_page = request.args.get("per_page", default=25, type=int) or 25
    q = (request.args.get("q") or "").strip()[:200]
    cid = request.args.get("challengeId", type=int)
    virtual_event_id = request.args.get("virtualEventId", type=int) or DEFAULT_VIRTUAL_EVENT_ID
    advanced = _parse_mdc_users_advanced_from_request(request.args)
    sk, sd = _parse_mdc_users_roster_sort(request.args, mode="virtual")
    users = _load_mdc_users_page(
        virtual_event_id, page, per_page, q, None,
        mode="virtual", challenge_id=cid, advanced=advanced,
        roster_sort_key=sk,
        roster_sort_dir=sd,
    )
    challenges = _load_virtual_challenges_brief(virtual_event_id)
    return render_template(
        "virtual_users.html",
        title="Virtual · Users",
        users=users,
        virtual_challenges=challenges,
        active_challenge_id=cid,
        active_virtual_event_id=virtual_event_id,
        default_virtual_event_id=DEFAULT_VIRTUAL_EVENT_ID,
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
                "participated_challenge_id",
                "submission_session",
                "arenaChallengeId",
                "arenaTeamSegment",
                "arenaAttemptsCompleted",
                "sort",
                "sort_dir",
            )
        )[:40],
    },
)
def virtual_users_export_csv():
    q = (request.args.get("q") or "").strip()[:200]
    cid = request.args.get("challengeId", type=int)
    virtual_event_id = request.args.get("virtualEventId", type=int) or DEFAULT_VIRTUAL_EVENT_ID
    advanced = _parse_mdc_users_advanced_from_request(request.args)
    sk, sd = _parse_mdc_users_roster_sort(request.args, mode="virtual")
    rows, err = _fetch_mdc_users_export_rows(
        virtual_event_id,
        q,
        None,
        mode="virtual",
        challenge_id=cid,
        advanced=advanced,
        roster_sort_key=sk,
        roster_sort_dir=sd,
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
    pw_invalidate_read_caches()
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
    pw_invalidate_read_caches()
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
    pw_invalidate_read_caches()
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
    virtual_challenges = _load_virtual_challenges_brief(virtual_event_id)
    return render_template(
        "import_virtual.html",
        virtual_event_id=virtual_event_id,
        virtual_challenges=virtual_challenges,
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
    if DEBUG_MODE:
        print(
            f"Prompt Wars [{mode}] listening on http://{APP_HOST}:{APP_PORT}"
            f" (reloader={'on' if USE_RELOADER else 'off'})",
            file=sys.stderr,
        )
        app.run(host=APP_HOST, port=APP_PORT, debug=True, use_reloader=USE_RELOADER)
        return
    from waitress import serve  # noqa: WPS433

    threads = int(os.environ.get("WAITRESS_THREADS", "64"))
    channel_timeout = int(os.environ.get("WAITRESS_CHANNEL_TIMEOUT", "120"))
    cleanup_interval = int(os.environ.get("WAITRESS_CLEANUP_INTERVAL", "30"))
    # Waitress default max_request_body_size is 1 GiB; raise for large Vision exports (override via env).
    max_request_body_size = 200 * 1024 * 1024 * 1024  # 200 GiB default (Waitress has no true "unlimited")
    _w_body_raw = (os.environ.get("WAITRESS_MAX_REQUEST_BODY_BYTES") or "").strip()
    if _w_body_raw:
        try:
            max_request_body_size = int(_w_body_raw)
        except ValueError:
            print(
                f"Prompt Wars: invalid WAITRESS_MAX_REQUEST_BODY_BYTES={_w_body_raw!r}, using default",
                file=sys.stderr,
            )
            max_request_body_size = 200 * 1024 * 1024 * 1024
    if max_request_body_size <= 0:
        max_request_body_size = 200 * 1024 * 1024 * 1024
    print(
        f"Prompt Wars [PROD/waitress] http://{APP_HOST}:{APP_PORT} threads={threads} "
        f"channel_timeout={channel_timeout}s max_request_body_size={max_request_body_size}",
        file=sys.stderr,
    )
    serve(
        app,
        host=APP_HOST,
        port=APP_PORT,
        threads=threads,
        channel_timeout=channel_timeout,
        cleanup_interval=cleanup_interval,
        asyncore_use_poll=True,
        max_request_body_size=max_request_body_size,
    )


if __name__ == "__main__":
    main()
