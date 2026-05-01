"""
Cross-track submission analytics: in-person vs virtual challenge submissions
joined on leader_email_normalized (see database/init.sql).

Whitelisted dimensions only — no dynamic SQL from arbitrary client strings.
"""

from __future__ import annotations

from dataclasses import astuple, dataclass
from datetime import date, datetime, timezone
from typing import Any, Mapping

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

TABLE_IP_CSR = "in_person_challenge_submission_rows"
TABLE_V_CSR = "virtual_challenge_submission_rows"
TABLE_IP_MDC = "in_person_main_data_center_registrations"
TABLE_V_MDC = "virtual_main_data_center_registrations"

_SHEET_KINDS = frozenset({"main", "warmup"})


def submission_crossover_cache_key(p: SubmissionCrossoverParams) -> tuple[object, ...]:
    """Hashable cache key aligned with :func:`parse_submission_crossover_params` whitelists."""
    return ("submission_crossover_v1",) + astuple(p)


@dataclass(frozen=True)
class SubmissionCrossoverParams:
    in_person_event_id: int
    virtual_event_id: int
    ip_sheet_kind: str | None = None
    ip_attendance_city: str | None = None
    ip_prompt_war_on: date | None = None
    ip_session_label_contains: str | None = None
    ip_imported_from: datetime | None = None
    ip_imported_to: datetime | None = None
    ip_mdc_city: str | None = None
    ip_mdc_state: str | None = None
    ip_mdc_attendance_city: str | None = None
    # Exact match on in_person_challenge_submission_rows.session_label_normalized
    # (= lower(trim(raw label))). When set, supersedes ip_session_label_contains for IP filters.
    ip_session_label_normalized: str | None = None
    virtual_challenge_ids: tuple[int, ...] = ()
    v_imported_from: datetime | None = None
    v_imported_to: datetime | None = None
    v_mdc_city: str | None = None
    v_mdc_state: str | None = None
    breakdown_ip_attendance_city_limit: int = 20


def _parse_date(raw: str | None) -> date | None:
    if not raw or not str(raw).strip():
        return None
    s = str(raw).strip()[:10]
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def _parse_dt(raw: str | None) -> datetime | None:
    if not raw or not str(raw).strip():
        return None
    s = str(raw).strip()
    try:
        if len(s) == 10:
            d = date.fromisoformat(s)
            return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
        if "T" in s:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _parse_int_list(vals: list[str] | tuple[str, ...]) -> tuple[int, ...]:
    out: list[int] = []
    for v in vals:
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            continue
    return tuple(out)


def parse_submission_crossover_params(
    src: Mapping[str, Any],
    *,
    default_ip_event_id: int,
    default_v_event_id: int,
) -> SubmissionCrossoverParams | None:
    """Parse query dict (e.g. request.args). Returns None if event ids invalid."""
    def _get(name: str) -> str | None:
        v = src.get(name)
        if v is None:
            return None
        if isinstance(v, list):
            v = v[0] if v else None
        if v is None:
            return None
        s = str(v).strip()
        return s or None

    def _getlist(name: str) -> list[str]:
        gl = getattr(src, "getlist", None)
        if callable(gl):
            raw = gl(name)
            return [str(x).strip() for x in raw if str(x).strip()]
        v = src.get(name)
        if v is None:
            return []
        if isinstance(v, (list, tuple)):
            return [str(x).strip() for x in v if str(x).strip()]
        s = str(v).strip()
        return [s] if s else []

    try:
        ip_e = int(_get("inPersonEventId") or default_ip_event_id)
        v_e = int(_get("virtualEventId") or default_v_event_id)
    except (TypeError, ValueError):
        return None
    if ip_e < 1 or v_e < 1:
        return None

    sk = (_get("ipSheetKind") or "").strip().lower()
    ip_sheet = sk if sk in _SHEET_KINDS else None

    v_cids: tuple[int, ...] = ()
    lst = _getlist("virtualChallengeId") or _getlist("virtualChallengeIds")
    if lst:
        v_cids = _parse_int_list(lst)
    else:
        vc_raw = src.get("virtualChallengeId")
        if vc_raw is None:
            vc_raw = src.get("virtualChallengeIds")
        if isinstance(vc_raw, str) and vc_raw.strip():
            parts = [p.strip() for p in vc_raw.replace(",", " ").split() if p.strip()]
            v_cids = _parse_int_list(parts)
        elif vc_raw is not None and not isinstance(vc_raw, list):
            try:
                v_cids = (int(vc_raw),)
            except (TypeError, ValueError):
                v_cids = ()

    lim = 20
    if _get("breakdownLimit"):
        try:
            lim = min(50, max(1, int(_get("breakdownLimit") or "20")))
        except ValueError:
            lim = 20

    ip_session_label_normalized: str | None = None
    if hasattr(src, "__contains__") and "ipSessionLabel" in src:
        ip_session_label_normalized = (_get("ipSessionLabel") or "").strip()[:64]

    return SubmissionCrossoverParams(
        in_person_event_id=ip_e,
        virtual_event_id=v_e,
        ip_sheet_kind=ip_sheet,
        ip_attendance_city=_get("ipAttendanceCity"),
        ip_prompt_war_on=_parse_date(_get("ipPromptWarOn")),
        ip_session_label_contains=_get("ipSessionLabelContains"),
        ip_imported_from=_parse_dt(_get("ipImportedFrom")),
        ip_imported_to=_parse_dt(_get("ipImportedTo")),
        ip_mdc_city=_get("ipMdcCity"),
        ip_mdc_state=_get("ipMdcState"),
        ip_mdc_attendance_city=_get("ipMdcAttendanceCity"),
        ip_session_label_normalized=ip_session_label_normalized,
        virtual_challenge_ids=v_cids,
        v_imported_from=_parse_dt(_get("vImportedFrom")),
        v_imported_to=_parse_dt(_get("vImportedTo")),
        v_mdc_city=_get("vMdcCity"),
        v_mdc_state=_get("vMdcState"),
        breakdown_ip_attendance_city_limit=lim,
    )


def _ip_csr_where(
    p: SubmissionCrossoverParams,
    params: dict[str, Any],
    *,
    alias: str = "csr",
) -> list[str]:
    """WHERE fragments for in-person submission rows (AND-prefixed)."""
    parts = [f"{alias}.event_id = :ip_event"]
    params["ip_event"] = p.in_person_event_id
    if p.ip_sheet_kind:
        parts.append(f"{alias}.sheet_kind = :ip_sheet_kind")
        params["ip_sheet_kind"] = p.ip_sheet_kind
    if p.ip_attendance_city:
        parts.append(f"lower(btrim({alias}.attendance_city)) = lower(btrim(:ip_ac_city))")
        params["ip_ac_city"] = p.ip_attendance_city.strip()
    if p.ip_prompt_war_on is not None:
        parts.append(f"{alias}.prompt_war_on = :ip_pwo")
        params["ip_pwo"] = p.ip_prompt_war_on
    if p.ip_session_label_normalized is not None:
        parts.append(f"{alias}.session_label_normalized = lower(btrim(:ip_sess_norm))")
        params["ip_sess_norm"] = str(p.ip_session_label_normalized).strip()[:64]
    elif p.ip_session_label_contains:
        parts.append(f"lower(btrim({alias}.session_label)) LIKE :ip_sess_like")
        params["ip_sess_like"] = f"%{p.ip_session_label_contains.strip().lower()}%"
    if p.ip_imported_from is not None:
        parts.append(f"{alias}.imported_at >= :ip_imp_from")
        params["ip_imp_from"] = p.ip_imported_from
    if p.ip_imported_to is not None:
        parts.append(f"{alias}.imported_at <= :ip_imp_to")
        params["ip_imp_to"] = p.ip_imported_to
    return parts


def _ip_mdc_exists_sql(
    alias_csr: str,
    p: SubmissionCrossoverParams,
    params: dict[str, Any],
) -> str:
    """EXISTS on IP MDC (event + email + optional city/state/attendance_city). Empty if no MDC filters."""
    if not (p.ip_mdc_city or p.ip_mdc_state or p.ip_mdc_attendance_city):
        return ""
    parts = [
        f"m.event_id = :ip_event",
        f"m.email_normalized = {alias_csr}.leader_email_normalized",
    ]
    if p.ip_mdc_city and str(p.ip_mdc_city).strip():
        parts.append("lower(btrim(m.city)) = lower(btrim(:ip_mdc_city))")
        params["ip_mdc_city"] = p.ip_mdc_city.strip()
    if p.ip_mdc_state and str(p.ip_mdc_state).strip():
        parts.append("lower(btrim(m.state)) = lower(btrim(:ip_mdc_state))")
        params["ip_mdc_state"] = p.ip_mdc_state.strip()
    if p.ip_mdc_attendance_city and str(p.ip_mdc_attendance_city).strip():
        parts.append("lower(btrim(m.attendance_city)) = lower(btrim(:ip_mdc_attendance_city))")
        params["ip_mdc_attendance_city"] = p.ip_mdc_attendance_city.strip()
    wh = " AND ".join(parts)
    return f"EXISTS (SELECT 1 FROM {TABLE_IP_MDC} m WHERE {wh})"


def _v_csr_where(p: SubmissionCrossoverParams, params: dict[str, Any], *, alias: str = "csr") -> list[str]:
    parts = [f"{alias}.event_id = :v_event"]
    params["v_event"] = p.virtual_event_id
    if p.virtual_challenge_ids:
        parts.append(f"{alias}.challenge_id = ANY(:v_cids)")
        params["v_cids"] = list(p.virtual_challenge_ids)
    if p.v_imported_from is not None:
        parts.append(f"{alias}.imported_at >= :v_imp_from")
        params["v_imp_from"] = p.v_imported_from
    if p.v_imported_to is not None:
        parts.append(f"{alias}.imported_at <= :v_imp_to")
        params["v_imp_to"] = p.v_imported_to
    return parts


def _v_mdc_exists_sql(alias_csr: str, p: SubmissionCrossoverParams, params: dict[str, Any]) -> str:
    if not (p.v_mdc_city or p.v_mdc_state):
        return ""
    parts = [
        f"m.event_id = :v_event",
        f"m.email_normalized = {alias_csr}.leader_email_normalized",
    ]
    if p.v_mdc_city and str(p.v_mdc_city).strip():
        parts.append("lower(btrim(m.city)) = lower(btrim(:v_mdc_city))")
        params["v_mdc_city"] = p.v_mdc_city.strip()
    if p.v_mdc_state and str(p.v_mdc_state).strip():
        parts.append("lower(btrim(m.state)) = lower(btrim(:v_mdc_state))")
        params["v_mdc_state"] = p.v_mdc_state.strip()
    wh = " AND ".join(parts)
    return f"EXISTS (SELECT 1 FROM {TABLE_V_MDC} m WHERE {wh})"


def load_submission_crossover(conn: Connection, p: SubmissionCrossoverParams) -> dict[str, Any]:
    params: dict[str, Any] = {}
    ip_w = _ip_csr_where(p, params, alias="i")
    v_w = _v_csr_where(p, params, alias="v")

    ip_where_sql = " AND ".join(ip_w)
    v_where_sql = " AND ".join(v_w)

    ip_mdc_frag = _ip_mdc_exists_sql("i", p, params)
    ip_mdc_sql = f" AND {ip_mdc_frag}" if ip_mdc_frag else ""

    v_mdc_frag = _v_mdc_exists_sql("v", p, params)
    v_mdc_sql = f" AND {v_mdc_frag}" if v_mdc_frag else ""

    sql = f"""
WITH ip AS (
  SELECT DISTINCT i.leader_email_normalized AS e
  FROM {TABLE_IP_CSR} i
  WHERE {ip_where_sql}
  {ip_mdc_sql}
),
v AS (
  SELECT DISTINCT v.leader_email_normalized AS e
  FROM {TABLE_V_CSR} v
  WHERE {v_where_sql}
  {v_mdc_sql}
)
SELECT
  (SELECT COUNT(*)::bigint FROM ip) AS n_distinct_ip_leaders,
  (SELECT COUNT(*)::bigint FROM v) AS n_distinct_v_leaders,
  (SELECT COUNT(*)::bigint FROM ip INNER JOIN v ON ip.e = v.e) AS n_both,
  (SELECT COUNT(*)::bigint FROM ip LEFT JOIN v ON ip.e = v.e WHERE v.e IS NULL) AS n_ip_only,
  (SELECT COUNT(*)::bigint FROM v LEFT JOIN ip ON v.e = ip.e WHERE ip.e IS NULL) AS n_v_only
"""
    row = conn.execute(text(sql), params).mappings().first()
    if not row:
        return {"error": "empty result"}

    out: dict[str, Any] = {
        "error": None,
        "scope": {
            "in_person_event_id": p.in_person_event_id,
            "virtual_event_id": p.virtual_event_id,
            "virtual_challenge_ids": list(p.virtual_challenge_ids) if p.virtual_challenge_ids else None,
            "match_on": "leader_email_normalized",
        },
        "counts": {
            "distinct_ip_leaders": int(row["n_distinct_ip_leaders"] or 0),
            "distinct_v_leaders": int(row["n_distinct_v_leaders"] or 0),
            "both_tracks": int(row["n_both"] or 0),
            "ip_only": int(row["n_ip_only"] or 0),
            "v_only": int(row["n_v_only"] or 0),
        },
        "filters_applied": {
            "ip_sheet_kind": p.ip_sheet_kind,
            "ip_attendance_city": p.ip_attendance_city,
            "ip_prompt_war_on": p.ip_prompt_war_on.isoformat() if p.ip_prompt_war_on else None,
            "ip_session_label_contains": p.ip_session_label_contains,
            "ip_imported_from": p.ip_imported_from.isoformat() if p.ip_imported_from else None,
            "ip_imported_to": p.ip_imported_to.isoformat() if p.ip_imported_to else None,
            "ip_mdc_city": p.ip_mdc_city,
            "ip_mdc_state": p.ip_mdc_state,
            "ip_mdc_attendance_city": p.ip_mdc_attendance_city,
            "ip_session_label_normalized": p.ip_session_label_normalized,
            "v_imported_from": p.v_imported_from.isoformat() if p.v_imported_from else None,
            "v_imported_to": p.v_imported_to.isoformat() if p.v_imported_to else None,
            "v_mdc_city": p.v_mdc_city,
            "v_mdc_state": p.v_mdc_state,
        },
        "by_ip_attendance_city": [],
    }

    lim = min(50, max(1, p.breakdown_ip_attendance_city_limit))
    params2 = dict(params)
    params2["bd_lim"] = lim
    ip_w2 = _ip_csr_where(p, params2, alias="c")
    ip_where2 = " AND ".join(ip_w2)
    ip_mdc2_frag = _ip_mdc_exists_sql("c", p, params2)
    ip_mdc2 = f" AND {ip_mdc2_frag}" if ip_mdc2_frag else ""

    bd_sql = f"""
WITH ip AS (
  SELECT DISTINCT i.leader_email_normalized AS e
  FROM {TABLE_IP_CSR} i
  WHERE {ip_where_sql}
  {ip_mdc_sql}
),
v AS (
  SELECT DISTINCT v.leader_email_normalized AS e
  FROM {TABLE_V_CSR} v
  WHERE {v_where_sql}
  {v_mdc_sql}
),
both AS (
  SELECT ip.e FROM ip INNER JOIN v ON ip.e = v.e
),
leader_city AS (
  SELECT DISTINCT ON (b.e)
    b.e,
    NULLIF(TRIM(c.attendance_city), '') AS city_label
  FROM both b
  INNER JOIN {TABLE_IP_CSR} c ON c.leader_email_normalized = b.e AND {ip_where2}
  {ip_mdc2}
  ORDER BY b.e, lower(coalesce(nullif(trim(c.attendance_city), ''), ''))
)
SELECT city_label,
       COUNT(*)::bigint AS n_leaders
FROM leader_city
WHERE city_label IS NOT NULL
GROUP BY 1
ORDER BY n_leaders DESC, city_label ASC
LIMIT :bd_lim
"""
    try:
        rows = conn.execute(text(bd_sql), params2).mappings().all()
        out["by_ip_attendance_city"] = [
            {"attendance_city": str(r["city_label"]), "n_leaders_both_tracks": int(r["n_leaders"] or 0)}
            for r in rows
            if r.get("city_label")
        ]
    except Exception as exc:  # noqa: BLE001
        out["by_ip_attendance_city_error"] = str(exc)

    return out


def load_submission_crossover_uncached(
    engine: Engine,
    p: SubmissionCrossoverParams,
) -> dict[str, Any]:
    try:
        with engine.connect() as conn:
            return load_submission_crossover(conn, p)
    except Exception as exc:  # noqa: BLE001
        return {
            "error": str(exc),
            "scope": None,
            "counts": None,
            "filters_applied": None,
            "by_ip_attendance_city": [],
        }
