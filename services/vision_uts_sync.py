"""
Vision UTS → ``virtual_main_data_center_registrations`` sync (single HTTP fetch, bulk upserts).

No Flask imports. Callers pass ``invalidate_caches`` after success (e.g. ``app.pw_invalidate_read_caches``).
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Callable
from datetime import date, datetime, timezone
from typing import Any, Mapping

import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Engine

from scripts import etl_data_center
from services import vision_uts_client
from services.vision_uts_client import VisionUtsError

logger = logging.getLogger(__name__)

TABLE_VIRTUAL_MDC = "virtual_main_data_center_registrations"

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
    """.format(
    table=TABLE_VIRTUAL_MDC
)

_VIRTUAL_MDC_UPSERT = text(_MDC_UPSERT_SQL_VIRTUAL)

_CHECKPOINT_UPSERT = text(
    """
    INSERT INTO vision_uts_virtual_mdc_sync_state (
      event_id, last_success_at, last_run_started_at, last_run_finished_at, last_run_status,
      last_rows_fetched, last_rows_inserted, last_rows_updated, last_rows_failed,
      last_error, last_triggered_by, last_payload_digest, updated_at
    ) VALUES (
      :event_id, :last_success_at, :last_run_started_at, :last_run_finished_at, :last_run_status,
      :last_rows_fetched, :last_rows_inserted, :last_rows_updated, :last_rows_failed,
      :last_error, :last_triggered_by, :last_payload_digest, now()
    )
    ON CONFLICT (event_id) DO UPDATE SET
      last_success_at = COALESCE(EXCLUDED.last_success_at, vision_uts_virtual_mdc_sync_state.last_success_at),
      last_run_started_at = EXCLUDED.last_run_started_at,
      last_run_finished_at = EXCLUDED.last_run_finished_at,
      last_run_status = EXCLUDED.last_run_status,
      last_rows_fetched = EXCLUDED.last_rows_fetched,
      last_rows_inserted = EXCLUDED.last_rows_inserted,
      last_rows_updated = EXCLUDED.last_rows_updated,
      last_rows_failed = EXCLUDED.last_rows_failed,
      last_error = EXCLUDED.last_error,
      last_triggered_by = EXCLUDED.last_triggered_by,
      last_payload_digest = EXCLUDED.last_payload_digest,
      updated_at = now()
    """
)


class VisionUtsPayloadError(VisionUtsError):
    """JSON shape or row mapping could not be interpreted."""


def _payload_digest(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, default=str)
    if len(raw) > 50_000:
        raw = raw[:50_000]
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _ci_map(rec: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in rec.items():
        if k is None:
            continue
        nk = str(k).strip().lower().replace(" ", "_").replace("-", "_").replace("/", "_")
        out[nk] = v
    return out


def _get_ci(m: dict[str, Any], *names: str) -> Any:
    for n in names:
        key = n.lower().replace(" ", "_").replace("-", "_")
        if key in m:
            v = m[key]
            if v is not None and (not isinstance(v, str) or v.strip() != ""):
                return v
    return None


_LIST_KEYS_TOP = ("data", "registrations", "results", "items", "records", "rows", "users", "participants")
_LIST_KEYS_NESTED = ("registrations", "data", "results", "items", "records", "rows", "users", "participants")
_ENVELOPE_KEYS = ("data", "result", "response", "payload", "body", "event")
_HEADER_KEYS = ("columns", "headers", "fields", "keys", "schema")


def _shape_summary(payload: Any, *, depth: int = 0) -> Any:
    """Return a JSON-friendly description of ``payload`` shape (for logging only)."""
    if depth > 4:
        return "…"
    if isinstance(payload, dict):
        out: dict[str, Any] = {}
        for k, v in list(payload.items())[:30]:
            if isinstance(v, dict):
                out[str(k)] = {"_type": "object", "keys": list(v.keys())[:30]}
            elif isinstance(v, list):
                first = v[0] if v else None
                out[str(k)] = {
                    "_type": "array",
                    "length": len(v),
                    "first_item_keys": list(first.keys())[:30] if isinstance(first, dict) else type(first).__name__,
                }
            else:
                out[str(k)] = type(v).__name__
        return out
    if isinstance(payload, list):
        first = payload[0] if payload else None
        return {
            "_type": "array",
            "length": len(payload),
            "first_item_keys": list(first.keys())[:30] if isinstance(first, dict) else type(first).__name__,
        }
    return type(payload).__name__


def _extract_header_list(root: dict[str, Any]) -> list[str] | None:
    """Look for a sibling list of column names alongside a tabular ``data`` array."""
    for key in _HEADER_KEYS:
        v = root.get(key)
        if isinstance(v, list) and v and all(isinstance(h, (str, int, float)) for h in v):
            return [str(h) for h in v]
    return None


def _tabular_to_records(rows: list[list[Any]], headers: list[str]) -> list[dict[str, Any]]:
    """Zip a list-of-lists tabular response into a list of dicts using ``headers``."""
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, list):
            continue
        item: dict[str, Any] = {}
        for i, h in enumerate(headers):
            if i < len(row):
                item[h] = row[i]
        out.append(item)
    return out


def extract_registration_records(payload: Any) -> list[dict[str, Any]]:
    """Resolve the list of registration objects from common Vision UTS response shapes."""
    if isinstance(payload, list):
        items = [x for x in payload if isinstance(x, dict)]
        if not items and payload:
            raise VisionUtsPayloadError("Vision UTS JSON array must contain objects")
        return items

    if not isinstance(payload, dict):
        raise VisionUtsPayloadError("Vision UTS JSON root must be an object or array")

    root = dict(payload)

    for key in _LIST_KEYS_TOP:
        v = root.get(key)
        if isinstance(v, list) and any(isinstance(x, dict) for x in v):
            return [x for x in v if isinstance(x, dict)]

    for env_key in _ENVELOPE_KEYS:
        ev = root.get(env_key)
        if isinstance(ev, dict):
            evm = dict(ev)
            for key in _LIST_KEYS_NESTED:
                v = evm.get(key)
                if isinstance(v, list) and any(isinstance(x, dict) for x in v):
                    return [x for x in v if isinstance(x, dict)]
            tab = _extract_tabular(evm)
            if tab is not None:
                return tab

    tab = _extract_tabular(root)
    if tab is not None:
        return tab

    for key in _LIST_KEYS_TOP:
        v = root.get(key)
        if isinstance(v, list):
            return [x for x in v if isinstance(x, dict)]

    shape = _shape_summary(root)
    logger.warning("Vision UTS payload shape (no registration list found): %s", shape)
    raise VisionUtsPayloadError(
        "Unrecognized Vision UTS JSON. Top-level keys: "
        f"{list(root.keys())[:30]}. See server log for shape; extend "
        "services/vision_uts_sync.py extract_registration_records()."
    )


def _extract_tabular(root: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Handle list-of-lists payloads (e.g. ``{"data": [[...row1...], [...row2...], ...]}``)."""
    for key in _LIST_KEYS_TOP:
        v = root.get(key)
        if not (isinstance(v, list) and v and isinstance(v[0], list)):
            continue
        headers = _extract_header_list(root)
        if headers:
            rows: list[list[Any]] = [r for r in v if isinstance(r, list)]
        else:
            first = v[0]
            if not all(isinstance(x, (str, int, float)) for x in first):
                continue
            headers = [str(x) for x in first]
            rows = [r for r in v[1:] if isinstance(r, list)]
        recs = _tabular_to_records(rows, headers)
        if recs:
            logger.warning(
                "vision uts tabular detected on key=%r headers=%s",
                key,
                headers[:30],
            )
            return recs
    return None


def _parse_dob(val: Any) -> date | None:
    if val is None or val == "":
        return None
    if isinstance(val, date) and not isinstance(val, datetime):
        return val
    if isinstance(val, datetime):
        return val.date()
    try:
        if isinstance(val, float) and pd.isna(val):
            return None
    except (TypeError, ValueError):
        pass
    ts = pd.to_datetime(val, errors="coerce")
    if pd.isna(ts):
        return None
    d = ts.date()
    if d == date(1970, 1, 1):
        return None
    return d


def map_registration_row(rec: Mapping[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """
    Map one API registration object to upsert params (without ``event_id``).

    Returns ``(row, None)`` or ``(None, failure_reason)``.
    """
    m = _ci_map(rec)
    email_raw = _get_ci(m, "email", "e_mail", "mail", "email_address", "user_email")
    if email_raw is None:
        return None, "missing email"
    email = str(email_raw).strip()
    if not email:
        return None, "empty email"

    des_raw = _get_ci(
        m,
        "designation",
        "designation_year_of_exp",
        "designation_(year_of_exp.)",
        "designation_years_experience",
    )
    des_t, des_y = etl_data_center.split_designation_with_years(des_raw)

    fts_raw = _get_ci(
        m,
        "timestamp",
        "form_timestamp",
        "created_at",
        "registered_at",
        "submitted_at",
        "registration_date",
    )
    form_ts = etl_data_center._normalize_form_timestamp_for_db(fts_raw)

    dob_raw = _get_ci(m, "dob", "date_of_birth", "dateofbirth", "birth_date", "birthdate")
    dob = _parse_dob(dob_raw)

    row: dict[str, Any] = {
        "email": email,
        "form_timestamp": form_ts,
        "utm_source": _as_opt_str(_get_ci(m, "utm_source", "utmsource")),
        "utm_medium": _as_opt_str(_get_ci(m, "utm_medium", "utmmedium")),
        "utm_campaign": _as_opt_str(_get_ci(m, "utm_campaign", "utmcampaign")),
        "utm_term": _as_opt_str(_get_ci(m, "utm_term", "utmterm")),
        "utm_content": _as_opt_str(_get_ci(m, "utm_content", "utmcontent")),
        "org_name": _as_opt_str(
            _get_ci(
                m,
                "org_name",
                "college_school_company_startup_name",
                "organization",
                "company",
                "institute",
            )
        ),
        "org_state": _as_opt_str(_get_ci(m, "org_state", "college_school_state", "organization_state")),
        "org_city": _as_opt_str(_get_ci(m, "org_city", "college_school_city", "organization_city")),
        "class_stream": _as_opt_str(_get_ci(m, "class_stream", "class/stream")),
        "portfolio": _as_opt_str(_get_ci(m, "portfolio")),
        "domain": _as_opt_str(_get_ci(m, "domain")),
        "designation": des_t,
        "designation_years_experience": des_y,
        "founded_info": _as_opt_str(_get_ci(m, "founded_info", "founded_in_(startup_size)", "founded_in")),
        "degree": _as_opt_str(_get_ci(m, "degree", "degree_(passout_year)")),
        "profile_name": _as_opt_str(_get_ci(m, "profile_name", "profilename")),
        "full_name": _as_opt_str(_get_ci(m, "full_name", "fullname", "name", "participant_name")),
        "mobile": _as_opt_str(_get_ci(m, "mobile", "mobile_number", "phone", "phone_number")),
        "whatsapp": _as_opt_str(_get_ci(m, "whatsapp", "whatsapp_number")),
        "country": _as_opt_str(_get_ci(m, "country")),
        "state": _as_opt_str(_get_ci(m, "state", "state_province")),
        "city": _as_opt_str(_get_ci(m, "city", "city_residence")),
        "dob": dob,
        "gender": _as_opt_str(_get_ci(m, "gender")),
        "occupation": _as_opt_str(_get_ci(m, "occupation")),
        "github_url": _as_opt_str(_get_ci(m, "github_url", "github", "githubprofile")),
        "linkedin_url": _as_opt_str(_get_ci(m, "linkedin_url", "linkedin", "linkedinprofile")),
        "attendance_city": _as_opt_str(
            _get_ci(
                m,
                "attendance_city",
                "in_which_city_would_you_like_to_attend_the_in_person_promptwars_promptathon?",
                "preferred_city",
            )
        ),
    }
    return row, None


def _as_opt_str(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None


def _dedupe_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Last row wins per normalized email (matches DB unique on email_normalized)."""
    by_email: dict[str, dict[str, Any]] = {}
    for r in rows:
        key = r["email"].strip().lower()
        by_email[key] = r
    return list(by_email.values())


def _write_checkpoint(
    conn,
    *,
    event_id: int,
    started: datetime,
    finished: datetime,
    status: str,
    triggered_by: str,
    fetched: int,
    inserted: int,
    updated: int,
    failed: int,
    err: str | None,
    digest: str | None,
    success_at: datetime | None,
) -> None:
    conn.execute(
        _CHECKPOINT_UPSERT,
        {
            "event_id": event_id,
            "last_success_at": success_at,
            "last_run_started_at": started,
            "last_run_finished_at": finished,
            "last_run_status": status[:64],
            "last_rows_fetched": fetched,
            "last_rows_inserted": inserted,
            "last_rows_updated": updated,
            "last_rows_failed": failed,
            "last_error": (err or "")[:4000] or None,
            "last_triggered_by": (triggered_by or "")[:64],
            "last_payload_digest": digest,
        },
    )


def run_virtual_mdc_vision_uts_sync(
    engine: Engine,
    virtual_event_id: int,
    *,
    triggered_by: str,
    fetch_json: Callable[[], Any] | None = None,
    invalidate_caches: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """
    Advisory lock → single HTTP GET → map → transactional upserts → checkpoint.

    ``fetch_json`` defaults to ``vision_uts_client.fetch_vision_uts_json`` (overridable in tests).
    Returns a result dict (including ``status`` and ``execution_time_ms``); does not raise for
    normal upstream failures — see ``error`` when ``status`` is ``error``.
    """
    t0 = time.perf_counter()
    fetcher = fetch_json or vision_uts_client.fetch_vision_uts_json
    lock_k1 = vision_uts_client.ADVISORY_LOCK_KEY_1
    lock_k2 = vision_uts_client.ADVISORY_LOCK_KEY_2
    started = datetime.now(tz=timezone.utc)

    def _done(
        *,
        status: str,
        fetched: int,
        inserted: int,
        updated: int,
        failed: int,
        skipped_lock: bool,
        err: str | None = None,
    ) -> dict[str, Any]:
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        out: dict[str, Any] = {
            "status": status,
            "fetched_count": fetched,
            "inserted_count": inserted,
            "updated_count": updated,
            "failed_count": failed,
            "execution_time_ms": elapsed_ms,
            "skipped_due_to_lock": skipped_lock,
        }
        if err:
            out["error"] = err
        return out

    def _end_open_txn(conn) -> None:
        """Close any SQLAlchemy 2.0 autobegin transaction so we can call ``conn.begin()``."""
        try:
            if conn.in_transaction():
                conn.commit()
        except Exception:  # noqa: BLE001
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass

    with engine.connect() as conn:
        locked = conn.execute(
            text("SELECT pg_try_advisory_lock(:k1, :k2)"),
            {"k1": lock_k1, "k2": lock_k2},
        ).scalar()
        _end_open_txn(conn)
        if not bool(locked):
            return _done(
                status="skipped",
                fetched=0,
                inserted=0,
                updated=0,
                failed=0,
                skipped_lock=True,
            )

        records: list[dict[str, Any]] = []
        digest_src: dict[str, Any] = {}

        try:
            ev = conn.execute(
                text("SELECT id, kind FROM events WHERE id = :id"),
                {"id": int(virtual_event_id)},
            ).fetchone()
            _end_open_txn(conn)
            if not ev:
                raise VisionUtsError("event not found")
            if str(ev[1]) != "virtual":
                raise VisionUtsError("event must be virtual kind")

            raw_any = fetcher()
            logger.warning(
                "vision uts payload shape: %s",
                _shape_summary(raw_any),
            )
            records = extract_registration_records(raw_any)
            digest_src = raw_any if isinstance(raw_any, dict) else {"_root": "array", "n": len(records)}
            logger.warning(
                "vision uts records extracted: count=%d first_keys=%s",
                len(records),
                list(records[0].keys())[:30] if records else None,
            )

            mapped: list[dict[str, Any]] = []
            failed = 0
            failed_reasons: dict[str, int] = {}
            for rec in records:
                row, rec_err = map_registration_row(rec)
                if row is None:
                    failed += 1
                    reason = rec_err or "unknown"
                    failed_reasons[reason] = failed_reasons.get(reason, 0) + 1
                    continue
                mapped.append(row)
            if failed:
                logger.warning(
                    "vision uts mapping skipped %d/%d rows: %s",
                    failed,
                    len(records),
                    failed_reasons,
                )
            mapped = _dedupe_rows(mapped)

            inserted = 0
            updated = 0
            with conn.begin():
                for row in mapped:
                    params = {**row, "event_id": int(virtual_event_id)}
                    res = conn.execute(_VIRTUAL_MDC_UPSERT, params)
                    if bool(res.scalar_one()):
                        inserted += 1
                    else:
                        updated += 1

                finished = datetime.now(tz=timezone.utc)
                _write_checkpoint(
                    conn,
                    event_id=int(virtual_event_id),
                    started=started,
                    finished=finished,
                    status="success",
                    triggered_by=triggered_by,
                    fetched=len(records),
                    inserted=inserted,
                    updated=updated,
                    failed=failed,
                    err=None,
                    digest=_payload_digest(digest_src),
                    success_at=finished,
                )

            if invalidate_caches:
                invalidate_caches()

            return _done(
                status="success",
                fetched=len(records),
                inserted=inserted,
                updated=updated,
                failed=failed,
                skipped_lock=False,
            )

        except Exception as exc:  # noqa: BLE001
            logger.warning("vision uts sync failed: %s", exc)
            err_msg = str(exc)
            finished = datetime.now(tz=timezone.utc)
            _end_open_txn(conn)
            try:
                with conn.begin():
                    _write_checkpoint(
                        conn,
                        event_id=int(virtual_event_id),
                        started=started,
                        finished=finished,
                        status="error",
                        triggered_by=triggered_by,
                        fetched=len(records),
                        inserted=0,
                        updated=0,
                        failed=0,
                        err=err_msg,
                        digest=_payload_digest(digest_src) if digest_src else None,
                        success_at=None,
                    )
            except Exception as inner:  # noqa: BLE001
                logger.warning("vision uts checkpoint after error failed: %s", inner)

            return _done(
                status="error",
                fetched=len(records),
                inserted=0,
                updated=0,
                failed=0,
                skipped_lock=False,
                err=err_msg,
            )

        finally:
            try:
                _end_open_txn(conn)
                conn.execute(
                    text("SELECT pg_advisory_unlock(:k1, :k2)"),
                    {"k1": lock_k1, "k2": lock_k2},
                )
                _end_open_txn(conn)
            except Exception:  # noqa: BLE001
                pass
