"""
Hawkeye external RSVP API: mapping + snapshot persistence.

No Flask imports. All SQL via SQLAlchemy ``text()`` against a passed-in Engine.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import date, datetime
from typing import Any
from urllib.parse import quote

import requests
from sqlalchemy import text
from sqlalchemy.engine import Engine

SYSTEM_HAWKEYE = "hawkeye"


def _fetch_pw_session_row(engine: Engine, event_id: int, pw_session_id: int) -> dict[str, Any] | None:
    """Load a PW session row; ``city`` is stored lowercase for ``scope_key`` alignment."""
    sql = text(
        """
        SELECT id, event_id, city, prompt_war_on, session_label, scope_key, display_name
        FROM in_person_pw_sessions
        WHERE id = :sid AND event_id = :eid
        """
    )
    with engine.connect() as conn:
        row = conn.execute(sql, {"sid": int(pw_session_id), "eid": int(event_id)}).mappings().first()
    return dict(row) if row else None


# Confirmed with product:
#   GET {HAWKEYE_BASE_URL}/api/api/integrations/hawkeye/events/{eventTag}/stats?includeEmails=1
# Auth: ``Authorization: Bearer <HAWKEYE_API_KEY>`` when key is set.
DEFAULT_HAWKEYE_BASE_URL = "https://hawkeye.hack2skill.com"
_HAWKEYE_STATS_PATH = "/api/api/integrations/hawkeye/events/{tag}/stats"


# ---------------------------------------------------------------------------
# Scope helpers
# ---------------------------------------------------------------------------

# scope_key formats:
#   ''                                        -> top-level mapping for the event
#   '<city_lower>|<YYYY-MM-DD>|<label>'       -> per Prompt War session
def make_pw_session_scope_key(city: str, prompt_war_on: date | str, session_label: str | None = None) -> str:
    """Stable per-PW-session scope_key (city + ISO date + optional label)."""
    if isinstance(prompt_war_on, datetime):
        iso = prompt_war_on.date().isoformat()
    elif isinstance(prompt_war_on, date):
        iso = prompt_war_on.isoformat()
    else:
        iso = str(prompt_war_on)[:10]
    return f"{(city or '').strip().lower()}|{iso}|{(session_label or '').strip()}"


class HawkeyeError(Exception):
    """Upstream Hawkeye failure or misconfiguration (base URL, HTTP, parse)."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class HawkeyeNotConfiguredError(HawkeyeError):
    """No ``event_external_mappings`` row for this event + scope + Hawkeye."""

    def __init__(self, event_id: int, scope_key: str = "") -> None:
        suffix = f", scope_key={scope_key!r}" if scope_key else ""
        super().__init__(f"Hawkeye is not configured for event_id={event_id}{suffix}")
        self.event_id = event_id
        self.scope_key = scope_key


def get_mapping(
    engine: Engine,
    event_id: int,
    scope_key: str = "",
    *,
    pw_session_id: int | None = None,
) -> dict[str, Any] | None:
    if pw_session_id is not None:
        sql = text(
            """
            SELECT id, event_id, system_name, external_key, notes, scope_key, scope,
                   pw_session_id, created_at, updated_at
            FROM event_external_mappings
            WHERE event_id = :event_id
              AND system_name = :system_name
              AND pw_session_id = :pw_session_id
            """
        )
        params: dict[str, Any] = {
            "event_id": int(event_id),
            "system_name": SYSTEM_HAWKEYE,
            "pw_session_id": int(pw_session_id),
        }
    else:
        sql = text(
            """
            SELECT id, event_id, system_name, external_key, notes, scope_key, scope,
                   pw_session_id, created_at, updated_at
            FROM event_external_mappings
            WHERE event_id = :event_id
              AND system_name = :system_name
              AND scope_key = :scope_key
            """
        )
        params = {
            "event_id": int(event_id),
            "system_name": SYSTEM_HAWKEYE,
            "scope_key": scope_key or "",
        }
    with engine.connect() as conn:
        row = conn.execute(sql, params).mappings().first()
    if row is None:
        return None
    return dict(row)


def save_mapping(
    engine: Engine,
    event_id: int,
    event_tag: str,
    notes: str | None = None,
    *,
    scope_key: str = "",
    scope: dict[str, Any] | None = None,
    pw_session_id: int | None = None,
) -> dict[str, Any]:
    tag = (event_tag or "").strip()
    sk = (scope_key or "").strip()
    psid: int | None = int(pw_session_id) if pw_session_id is not None else None
    if psid is not None:
        sess = _fetch_pw_session_row(engine, int(event_id), psid)
        if not sess:
            raise HawkeyeError(f"pw_session_id={psid} not found for event_id={event_id}")
        sk = str(sess.get("scope_key") or "")
    sql = text(
        """
        INSERT INTO event_external_mappings (
          event_id, system_name, external_key, notes, scope_key, scope, pw_session_id
        )
        VALUES (
          :event_id, :system_name, :external_key, :notes, :scope_key, CAST(:scope AS jsonb), :pw_session_id
        )
        ON CONFLICT (event_id, system_name, scope_key) DO UPDATE SET
          external_key = EXCLUDED.external_key,
          notes = EXCLUDED.notes,
          scope = EXCLUDED.scope,
          pw_session_id = EXCLUDED.pw_session_id,
          updated_at = now()
        RETURNING id, event_id, system_name, external_key, notes, scope_key, scope,
                  pw_session_id, created_at, updated_at
        """
    )
    notes_val: str | None
    if notes is None:
        notes_val = None
    else:
        n = str(notes).strip()
        notes_val = n or None
    scope_json = json.dumps(scope, ensure_ascii=False) if scope is not None else None
    with engine.begin() as conn:
        row = conn.execute(
            sql,
            {
                "event_id": int(event_id),
                "system_name": SYSTEM_HAWKEYE,
                "external_key": tag,
                "notes": notes_val,
                "scope_key": sk,
                "scope": scope_json,
                "pw_session_id": psid,
            },
        ).mappings().one()
    return dict(row)


def fetch_from_hawkeye(event_tag: str) -> dict[str, Any]:
    base = (os.environ.get("HAWKEYE_BASE_URL") or DEFAULT_HAWKEYE_BASE_URL).strip().rstrip("/")
    if not base:
        raise HawkeyeError("HAWKEYE_BASE_URL is not set")
    key = (os.environ.get("HAWKEYE_API_KEY") or "").strip()
    tag = str(event_tag or "").strip()
    if not tag:
        raise HawkeyeError("event_tag is required")
    safe_tag = quote(tag, safe="")
    url = f"{base}{_HAWKEYE_STATS_PATH.format(tag=safe_tag)}?includeEmails=1"
    headers: dict[str, str] = {"Accept": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        resp = requests.get(url, headers=headers, timeout=(5, 15))
    except requests.RequestException as exc:
        raise HawkeyeError(f"Hawkeye request failed: {exc}") from exc
    if resp.status_code != 200:
        body = (resp.text or "")[:500]
        raise HawkeyeError(
            f"Hawkeye returned HTTP {resp.status_code}: {body}",
            status_code=resp.status_code,
        )
    try:
        return resp.json()
    except ValueError as exc:
        raise HawkeyeError("Hawkeye response is not valid JSON") from exc


def save_snapshot(
    engine: Engine,
    event_id: int,
    mapping_id: int | None,
    api_response: dict[str, Any],
    triggered_by: str,
    *,
    scope_key: str = "",
    pw_session_id: int | None = None,
) -> int:
    ev = api_response.get("event") if isinstance(api_response.get("event"), dict) else {}
    stats = api_response.get("stats") if isinstance(api_response.get("stats"), dict) else {}
    stats_emails = (
        api_response.get("statsEmails")
        if isinstance(api_response.get("statsEmails"), dict)
        else {}
    )
    hawkeye_event_id = ev.get("eventId")
    hawkeye_event_tag = ev.get("eventTag")
    hawkeye_event_name = ev.get("eventName")
    rsvp_invite_sent = stats.get("rsvpInviteSent")
    rsvp_accepted = stats.get("rsvpAccepted")
    checked_in = stats.get("checkedInParticipants")
    sql = text(
        """
        INSERT INTO hawkeye_rsvp_snapshots (
          event_id, mapping_id, scope_key, pw_session_id,
          hawkeye_event_id, hawkeye_event_tag, hawkeye_event_name,
          rsvp_invite_sent, rsvp_accepted, checked_in_participants,
          raw_stats, raw_stats_emails, raw_event_meta, fetch_triggered_by
        ) VALUES (
          :event_id, :mapping_id, :scope_key, :pw_session_id,
          :hawkeye_event_id, :hawkeye_event_tag, :hawkeye_event_name,
          :rsvp_invite_sent, :rsvp_accepted, :checked_in_participants,
          CAST(:raw_stats AS jsonb), CAST(:raw_stats_emails AS jsonb), CAST(:raw_event_meta AS jsonb),
          :fetch_triggered_by
        )
        RETURNING id
        """
    )
    params = {
        "event_id": int(event_id),
        "mapping_id": int(mapping_id) if mapping_id is not None else None,
        "scope_key": scope_key or "",
        "pw_session_id": int(pw_session_id) if pw_session_id is not None else None,
        "hawkeye_event_id": str(hawkeye_event_id) if hawkeye_event_id is not None else None,
        "hawkeye_event_tag": str(hawkeye_event_tag) if hawkeye_event_tag is not None else None,
        "hawkeye_event_name": str(hawkeye_event_name) if hawkeye_event_name is not None else None,
        "rsvp_invite_sent": int(rsvp_invite_sent) if rsvp_invite_sent is not None else None,
        "rsvp_accepted": int(rsvp_accepted) if rsvp_accepted is not None else None,
        "checked_in_participants": int(checked_in) if checked_in is not None else None,
        "raw_stats": json.dumps(stats, ensure_ascii=False),
        "raw_stats_emails": json.dumps(stats_emails, ensure_ascii=False),
        "raw_event_meta": json.dumps(ev, ensure_ascii=False),
        "fetch_triggered_by": (triggered_by or "manual")[:64],
    }
    with engine.begin() as conn:
        new_id = conn.execute(sql, params).scalar_one()
    return int(new_id)


def _norm_email(value: Any) -> str:
    return str(value or "").strip().lower()


def _email_set_from_stats_emails(stats_emails: dict[str, Any], key: str) -> set[str]:
    lst = stats_emails.get(key)
    if not isinstance(lst, list):
        return set()
    return {_norm_email(x) for x in lst if _norm_email(x)}


def summarize_hawkeye_rsvp_for_email(stats_emails: Any, email: str | None) -> dict[str, Any]:
    """
    Map Hawkeye ``statsEmails`` bucket lists to a single registration email.

    Priority when an address appears in more than one list: checked-in wins,
    then accepted, then pending, then invite-sent (defensive).
    """
    em = _norm_email(email)
    out: dict[str, Any] = {
        "snapshot_has_emails": False,
        "email_matched": False,
        "bucket": None,
        "summary": "",
    }
    if not em:
        out["summary"] = "No email on this registration row."
        return out
    if not isinstance(stats_emails, dict):
        out["summary"] = "No Hawkeye email bucket data for this snapshot."
        return out

    checked = _email_set_from_stats_emails(stats_emails, "checkedInParticipants")
    accepted = _email_set_from_stats_emails(stats_emails, "rsvpAccepted")
    pending = _email_set_from_stats_emails(stats_emails, "rsvpPending")
    invited = _email_set_from_stats_emails(stats_emails, "rsvpInviteSent")
    if checked or accepted or pending or invited:
        out["snapshot_has_emails"] = True

    if em in checked:
        out.update(
            {
                "email_matched": True,
                "bucket": "checked_in",
                "summary": "RSVP accepted and checked in (attended).",
            }
        )
    elif em in accepted:
        out.update(
            {
                "email_matched": True,
                "bucket": "accepted_not_attended",
                "summary": "RSVP accepted · not checked in yet (no attendance in Hawkeye).",
            }
        )
    elif em in pending:
        out.update(
            {
                "email_matched": True,
                "bucket": "pending",
                "summary": "RSVP still pending in Hawkeye.",
            }
        )
    elif em in invited:
        out.update(
            {
                "email_matched": True,
                "bucket": "invite_sent",
                "summary": "RSVP invite was sent; not yet accepted in Hawkeye.",
            }
        )
    else:
        if not out["snapshot_has_emails"]:
            out["summary"] = (
                "This Hawkeye snapshot has no email bucket lists yet; re-fetch stats with "
                "includeEmails enabled in settings."
            )
        else:
            out["summary"] = (
                "This email was not found in Hawkeye RSVP email lists for this session "
                "(Hawkeye may use a different address)."
            )
    return out


def get_latest_snapshot_stats_emails(
    engine: Engine,
    event_id: int,
    scope_key: str = "",
    *,
    pw_session_id: int | None = None,
) -> dict[str, Any] | None:
    """Latest snapshot row: ``stats_emails`` dict + ``fetched_at`` + ``hawkeye_event_name``."""
    if pw_session_id is not None:
        sql = text(
            """
            SELECT raw_stats_emails AS stats_emails, fetched_at, hawkeye_event_name
            FROM hawkeye_rsvp_snapshots
            WHERE event_id = :event_id
              AND pw_session_id = :pw_session_id
            ORDER BY fetched_at DESC
            LIMIT 1
            """
        )
        qparams = {"event_id": int(event_id), "pw_session_id": int(pw_session_id)}
    else:
        sql = text(
            """
            SELECT raw_stats_emails AS stats_emails, fetched_at, hawkeye_event_name
            FROM hawkeye_rsvp_snapshots
            WHERE event_id = :event_id
              AND scope_key = :scope_key
            ORDER BY fetched_at DESC
            LIMIT 1
            """
        )
        qparams = {"event_id": int(event_id), "scope_key": scope_key or ""}
    with engine.connect() as conn:
        row = conn.execute(sql, qparams).mappings().first()
    if row is None:
        return None
    d = dict(row)
    se = d.get("stats_emails")
    if isinstance(se, str):
        try:
            se = json.loads(se)
        except json.JSONDecodeError:
            se = None
    d["stats_emails"] = se if isinstance(se, dict) else None
    fa = d.get("fetched_at")
    if fa is not None and hasattr(fa, "isoformat"):
        d["fetched_at"] = fa.isoformat()
    return d


def get_latest_snapshot(
    engine: Engine,
    event_id: int,
    scope_key: str = "",
    *,
    pw_session_id: int | None = None,
) -> dict[str, Any] | None:
    if pw_session_id is not None:
        sql = text(
            """
            SELECT
              id,
              rsvp_invite_sent,
              rsvp_accepted,
              checked_in_participants,
              fetched_at,
              hawkeye_event_name,
              scope_key,
              pw_session_id
            FROM hawkeye_rsvp_snapshots
            WHERE event_id = :event_id
              AND pw_session_id = :pw_session_id
            ORDER BY fetched_at DESC
            LIMIT 1
            """
        )
        qparams = {"event_id": int(event_id), "pw_session_id": int(pw_session_id)}
    else:
        sql = text(
            """
            SELECT
              id,
              rsvp_invite_sent,
              rsvp_accepted,
              checked_in_participants,
              fetched_at,
              hawkeye_event_name,
              scope_key,
              pw_session_id
            FROM hawkeye_rsvp_snapshots
            WHERE event_id = :event_id
              AND scope_key = :scope_key
            ORDER BY fetched_at DESC
            LIMIT 1
            """
        )
        qparams = {"event_id": int(event_id), "scope_key": scope_key or ""}
    with engine.connect() as conn:
        row = conn.execute(sql, qparams).mappings().first()
    if row is None:
        return None
    d = dict(row)
    fa = d.get("fetched_at")
    if fa is not None and hasattr(fa, "isoformat"):
        d["fetched_at"] = fa.isoformat()
    return d


def list_pw_session_rows(
    engine: Engine,
    event_id: int,
    pw_sessions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    For an in-person event_id and a caller-provided list of PW sessions
    (``city``, ``prompt_war_on_iso``, ``session_label``, ``display``,
    ``team_count``), return one row per session annotated with its current
    Hawkeye mapping (``external_key``) and latest snapshot (3 stats +
    ``fetched_at`` + ``hawkeye_event_name``).
    """
    if not pw_sessions:
        return []
    keys = [
        make_pw_session_scope_key(
            s.get("city") or "",
            s.get("prompt_war_on_iso") or "",
            s.get("session_label") or "",
        )
        for s in pw_sessions
    ]
    sql = text(
        """
        SELECT
          m.id              AS mapping_id,
          m.external_key    AS external_key,
          m.notes           AS notes,
          m.scope_key       AS scope_key,
          m.updated_at      AS mapping_updated_at,
          s.rsvp_invite_sent,
          s.rsvp_accepted,
          s.checked_in_participants,
          s.fetched_at      AS snapshot_fetched_at,
          s.hawkeye_event_name
        FROM unnest(CAST(:scope_keys AS text[])) AS k(scope_key)
        LEFT JOIN event_external_mappings m
          ON m.event_id = :event_id
         AND m.system_name = :system_name
         AND m.scope_key = k.scope_key
        LEFT JOIN LATERAL (
          SELECT rsvp_invite_sent, rsvp_accepted, checked_in_participants,
                 fetched_at, hawkeye_event_name
          FROM hawkeye_rsvp_snapshots
          WHERE event_id = :event_id
            AND scope_key = k.scope_key
          ORDER BY fetched_at DESC
          LIMIT 1
        ) s ON TRUE
        ORDER BY k.scope_key
        """
    )
    by_key: dict[str, dict[str, Any]] = {}
    with engine.connect() as conn:
        for r in conn.execute(
            sql,
            {
                "event_id": int(event_id),
                "system_name": SYSTEM_HAWKEYE,
                "scope_keys": keys,
            },
        ).mappings().all():
            by_key[r["scope_key"] if r["scope_key"] is not None else ""] = dict(r)

    out: list[dict[str, Any]] = []
    for sess, key in zip(pw_sessions, keys):
        meta = by_key.get(key) or {}
        latest: dict[str, Any] | None = None
        if meta.get("snapshot_fetched_at") is not None:
            fa = meta["snapshot_fetched_at"]
            latest = {
                "rsvp_invite_sent": meta.get("rsvp_invite_sent"),
                "rsvp_accepted": meta.get("rsvp_accepted"),
                "checked_in_participants": meta.get("checked_in_participants"),
                "fetched_at": fa.isoformat() if hasattr(fa, "isoformat") else fa,
                "hawkeye_event_name": meta.get("hawkeye_event_name"),
            }
        m_updated = meta.get("mapping_updated_at")
        out.append(
            {
                "event_id": int(event_id),
                "pw_session_id": sess.get("pw_session_id"),
                "scope_key": key,
                "city": sess.get("city"),
                "prompt_war_on_iso": sess.get("prompt_war_on_iso"),
                "session_label": sess.get("session_label") or "",
                "display": sess.get("display") or "",
                "team_count": int(sess.get("team_count") or 0),
                "external_key": meta.get("external_key"),
                "notes": meta.get("notes"),
                "mapping_updated_at": m_updated.isoformat() if hasattr(m_updated, "isoformat") else m_updated,
                "latest": latest,
            }
        )
    return out


def list_in_person_events(engine: Engine) -> list[dict[str, Any]]:
    """All in-person events with their top-level Hawkeye mapping (scope_key='') + latest snapshot."""
    sql = text(
        """
        SELECT
          e.id              AS event_id,
          e.name            AS event_name,
          e.slug            AS event_slug,
          e.parent_event_id AS parent_event_id,
          m.external_key    AS external_key,
          m.notes           AS mapping_notes,
          m.updated_at      AS mapping_updated_at,
          s.id              AS snapshot_id,
          s.rsvp_invite_sent,
          s.rsvp_accepted,
          s.checked_in_participants,
          s.fetched_at      AS snapshot_fetched_at,
          s.hawkeye_event_name
        FROM events e
        LEFT JOIN event_external_mappings m
          ON m.event_id = e.id
         AND m.system_name = :system_name
         AND m.scope_key = ''
        LEFT JOIN LATERAL (
          SELECT id, rsvp_invite_sent, rsvp_accepted, checked_in_participants,
                 fetched_at, hawkeye_event_name
          FROM hawkeye_rsvp_snapshots
          WHERE event_id = e.id
            AND scope_key = ''
          ORDER BY fetched_at DESC
          LIMIT 1
        ) s ON TRUE
        WHERE e.kind = 'in_person'
        ORDER BY e.id ASC
        """
    )
    out: list[dict[str, Any]] = []
    with engine.connect() as conn:
        rows = conn.execute(sql, {"system_name": SYSTEM_HAWKEYE}).mappings().all()
    for r in rows:
        m_updated = r.get("mapping_updated_at")
        s_fetched = r.get("snapshot_fetched_at")
        latest: dict[str, Any] | None = None
        if r.get("snapshot_id") is not None:
            latest = {
                "snapshot_id": r["snapshot_id"],
                "rsvp_invite_sent": r["rsvp_invite_sent"],
                "rsvp_accepted": r["rsvp_accepted"],
                "checked_in_participants": r["checked_in_participants"],
                "fetched_at": s_fetched.isoformat() if hasattr(s_fetched, "isoformat") else s_fetched,
                "hawkeye_event_name": r.get("hawkeye_event_name"),
            }
        out.append(
            {
                "event_id": int(r["event_id"]),
                "event_name": r.get("event_name"),
                "event_slug": r.get("event_slug"),
                "parent_event_id": r.get("parent_event_id"),
                "external_key": r.get("external_key"),
                "mapping_notes": r.get("mapping_notes"),
                "mapping_updated_at": m_updated.isoformat() if hasattr(m_updated, "isoformat") else m_updated,
                "latest": latest,
            }
        )
    return out


def save_mapping_and_sync(
    engine: Engine,
    event_id: int,
    event_tag: str,
    triggered_by: str = "manual",
    *,
    scope_key: str = "",
    scope: dict[str, Any] | None = None,
    notes: str | None = None,
    pw_session_id: int | None = None,
    invalidate_caches: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Upsert the Hawkeye event tag for ``(event_id, scope_key)`` and immediately fetch + snapshot."""
    tag = (event_tag or "").strip()
    if not tag:
        raise HawkeyeError("event_tag is required")
    save_mapping(
        engine,
        event_id,
        tag,
        notes=notes,
        scope_key=scope_key,
        scope=scope,
        pw_session_id=pw_session_id,
    )
    return sync_event(
        engine,
        event_id,
        triggered_by=triggered_by,
        scope_key=scope_key,
        pw_session_id=pw_session_id,
        invalidate_caches=invalidate_caches,
    )


def sync_event(
    engine: Engine,
    event_id: int,
    triggered_by: str = "manual",
    *,
    scope_key: str = "",
    pw_session_id: int | None = None,
    invalidate_caches: Callable[[], None] | None = None,
) -> dict[str, Any]:
    sk = (scope_key or "").strip()
    psid: int | None = int(pw_session_id) if pw_session_id is not None else None
    if psid is not None:
        sess = _fetch_pw_session_row(engine, int(event_id), psid)
        if not sess:
            raise HawkeyeError(f"pw_session_id={psid} not found for event_id={event_id}")
        sk = str(sess.get("scope_key") or "")
    m = get_mapping(engine, event_id, scope_key=sk)
    if not m:
        raise HawkeyeNotConfiguredError(int(event_id), scope_key=sk or "")
    external_key = m.get("external_key") or ""
    if not str(external_key).strip():
        raise HawkeyeNotConfiguredError(int(event_id), scope_key=sk or "")
    data = fetch_from_hawkeye(external_key)
    snapshot_id = save_snapshot(
        engine,
        int(event_id),
        m.get("id"),
        data,
        triggered_by=triggered_by,
        scope_key=sk,
        pw_session_id=psid,
    )
    if invalidate_caches is not None:
        invalidate_caches()
    stats = data.get("stats") if isinstance(data.get("stats"), dict) else {}
    ev = data.get("event") if isinstance(data.get("event"), dict) else {}
    latest = get_latest_snapshot(engine, event_id, scope_key=sk, pw_session_id=psid)
    fetched_at = (latest or {}).get("fetched_at")
    return {
        "snapshot_id": snapshot_id,
        "scope_key": sk or "",
        "pw_session_id": psid,
        "rsvp_invite_sent": int(stats.get("rsvpInviteSent") or 0),
        "rsvp_accepted": int(stats.get("rsvpAccepted") or 0),
        "checked_in_participants": int(stats.get("checkedInParticipants") or 0),
        "fetched_at": fetched_at,
        "hawkeye_event_name": ev.get("eventName"),
    }
