"""Hawkeye RSVP mapping + snapshots (no live PostgreSQL)."""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any
from unittest.mock import MagicMock

import pytest

from services import hawkeye as hk
from services.hawkeye import HawkeyeNotConfiguredError


SAMPLE_RESPONSE: dict[str, Any] = {
    "event": {
        "_id": "...",
        "eventId": "EVT1775027285566C00QI",
        "eventTag": "test-rsvp-v1",
        "eventName": "Test RSVP V1",
        "startDate": "2026-04-03T11:00:00.000Z",
        "endDate": "2026-04-05T23:30:00.000Z",
        "location": "India",
        "isActive": True,
        "rsvpFlowEnabled": True,
        "rsvpMaxAccepted": 3,
    },
    "stats": {
        "totalParticipants": 4,
        "checkedInParticipants": 3,
        "checkedInAmongAccepted": 3,
        "totalStaff": 1,
        "checkInRate": 100,
        "rsvpFlowEnabled": True,
        "rsvpPending": 1,
        "rsvpAccepted": 3,
        "rsvpDeclined": 0,
        "rsvpPendingPct": 25,
        "rsvpAcceptedPct": 75,
        "rsvpDeclinedPct": 0,
        "rsvpInviteSent": 4,
        "qrEmailSent": 3,
        "rsvpCandidates": 4,
    },
    "recentCheckinsCount": 3,
    "staffCount": 1,
    "statsEmails": {
        "rsvpPending": ["..."],
        "rsvpAccepted": ["..."],
        "rsvpInviteSent": ["..."],
        "checkedInParticipants": ["..."],
    },
    "statsEmailsMeta": {"maxPerBucket": 0, "truncated": False},
}


class _MapResult:
    def __init__(self, mapping: dict[str, Any] | None = None, rows: list[dict[str, Any]] | None = None) -> None:
        self._m = mapping
        self._rows = rows

    def first(self) -> dict[str, Any] | None:
        if self._rows is not None:
            return self._rows[0] if self._rows else None
        return self._m

    def one(self) -> dict[str, Any]:
        if self._m is None:
            raise RuntimeError("expected row")
        return self._m

    def one_or_none(self) -> dict[str, Any] | None:
        return self._m

    def all(self) -> list[dict[str, Any]]:
        if self._rows is not None:
            return list(self._rows)
        return [self._m] if self._m is not None else []


class _ExecResult:
    def __init__(
        self,
        mapping: dict[str, Any] | None = None,
        scalar: Any | None = None,
        rows: list[dict[str, Any]] | None = None,
    ) -> None:
        self._mapping = mapping
        self._scalar = scalar
        self._rows = rows

    def mappings(self) -> _MapResult:
        return _MapResult(mapping=self._mapping, rows=self._rows)

    def scalar_one(self) -> Any:
        return self._scalar


class _HawkeyeMemConn:
    def __init__(self, store: dict[str, Any]) -> None:
        self._s = store

    def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> _ExecResult:
        sql = str(stmt).strip()
        params = dict(params or {})
        self._s.setdefault("calls", []).append((sql, params))

        if "FROM event_external_mappings" in sql and "WHERE event_id" in sql and "unnest" not in sql:
            eid = int(params["event_id"])
            if params.get("pw_session_id") is not None:
                pid = int(params["pw_session_id"])
                for row in self._s["mappings"].values():
                    if int(row.get("event_id") or 0) == eid and int(row.get("pw_session_id") or 0) == pid:
                        return _ExecResult(mapping=dict(row))
                return _ExecResult(mapping=None)
            sk = params.get("scope_key", "") or ""
            row = self._s["mappings"].get((eid, sk))
            return _ExecResult(mapping=dict(row) if row else None)

        if "INSERT INTO event_external_mappings" in sql and "ON CONFLICT" in sql:
            eid = int(params["event_id"])
            sk = params.get("scope_key", "") or ""
            existing = self._s["mappings"].get((eid, sk))
            mid = existing.get("id") if existing else None
            if mid is None:
                mid = self._s["next_mapping_id"]
                self._s["next_mapping_id"] += 1
            scope_raw = params.get("scope")
            scope_obj = json.loads(scope_raw) if isinstance(scope_raw, str) and scope_raw else None
            row = {
                "id": mid,
                "event_id": eid,
                "system_name": params["system_name"],
                "external_key": params["external_key"],
                "notes": params.get("notes"),
                "scope_key": sk,
                "scope": scope_obj,
                "pw_session_id": params.get("pw_session_id"),
                "created_at": (existing or {}).get("created_at", "2026-01-01T00:00:00+00:00"),
                "updated_at": "2026-01-02T00:00:00+00:00",
            }
            self._s["mappings"][(eid, sk)] = row
            return _ExecResult(mapping=dict(row))

        if "INSERT INTO hawkeye_rsvp_snapshots" in sql and "RETURNING id" in sql:
            sid = self._s["next_snapshot_id"]
            self._s["next_snapshot_id"] += 1
            raw_em = params.get("raw_stats_emails")
            if isinstance(raw_em, str):
                try:
                    stats_emails_obj = json.loads(raw_em)
                except json.JSONDecodeError:
                    stats_emails_obj = None
            else:
                stats_emails_obj = raw_em
            snap = {
                "id": sid,
                "event_id": int(params["event_id"]),
                "scope_key": params.get("scope_key", "") or "",
                "pw_session_id": params.get("pw_session_id"),
                "mapping_id": params.get("mapping_id"),
                "hawkeye_event_id": params.get("hawkeye_event_id"),
                "hawkeye_event_tag": params.get("hawkeye_event_tag"),
                "hawkeye_event_name": params.get("hawkeye_event_name"),
                "rsvp_invite_sent": params.get("rsvp_invite_sent"),
                "rsvp_accepted": params.get("rsvp_accepted"),
                "checked_in_participants": params.get("checked_in_participants"),
                "fetched_at": "2026-04-10T12:00:00+00:00",
                "raw_stats": json.loads(params["raw_stats"]),
                "stats_emails": stats_emails_obj if isinstance(stats_emails_obj, dict) else {},
            }
            self._s.setdefault("snapshots", []).append(snap)
            return _ExecResult(scalar=sid)

        if (
            "FROM hawkeye_rsvp_snapshots" in sql
            and "ORDER BY fetched_at DESC" in sql
            and "FROM events" not in sql
            and "unnest" not in sql
        ):
            eid = int(params["event_id"])
            if params.get("pw_session_id") is not None:
                pid = int(params["pw_session_id"])
                snaps = [
                    x
                    for x in self._s.get("snapshots", [])
                    if x["event_id"] == eid and int(x.get("pw_session_id") or 0) == pid
                ]
            else:
                sk = params.get("scope_key", "") or ""
                snaps = [
                    x
                    for x in self._s.get("snapshots", [])
                    if x["event_id"] == eid and (x.get("scope_key", "") or "") == sk
                ]
            if not snaps:
                return _ExecResult(mapping=None)
            latest = max(snaps, key=lambda x: x["fetched_at"])
            if "raw_stats_emails" in sql:
                return _ExecResult(
                    mapping={
                        "stats_emails": latest.get("stats_emails"),
                        "fetched_at": latest["fetched_at"],
                        "hawkeye_event_name": latest.get("hawkeye_event_name"),
                    }
                )
            return _ExecResult(
                mapping={
                    "id": latest["id"],
                    "rsvp_invite_sent": latest["rsvp_invite_sent"],
                    "rsvp_accepted": latest["rsvp_accepted"],
                    "checked_in_participants": latest["checked_in_participants"],
                    "fetched_at": latest["fetched_at"],
                    "hawkeye_event_name": latest.get("hawkeye_event_name"),
                    "scope_key": latest.get("scope_key", ""),
                }
            )

        if "unnest" in sql and "event_external_mappings" in sql and "hawkeye_rsvp_snapshots" in sql:
            eid = int(params["event_id"])
            keys = list(params.get("scope_keys") or [])
            rows: list[dict[str, Any]] = []
            for k in sorted(keys):
                m = self._s["mappings"].get((eid, k))
                snaps = [
                    x
                    for x in self._s.get("snapshots", [])
                    if x["event_id"] == eid and (x.get("scope_key", "") or "") == k
                ]
                latest = max(snaps, key=lambda x: x["fetched_at"]) if snaps else None
                rows.append(
                    {
                        "mapping_id": (m or {}).get("id"),
                        "external_key": (m or {}).get("external_key"),
                        "notes": (m or {}).get("notes"),
                        "scope_key": k,
                        "mapping_updated_at": (m or {}).get("updated_at"),
                        "rsvp_invite_sent": (latest or {}).get("rsvp_invite_sent"),
                        "rsvp_accepted": (latest or {}).get("rsvp_accepted"),
                        "checked_in_participants": (latest or {}).get("checked_in_participants"),
                        "snapshot_fetched_at": (latest or {}).get("fetched_at"),
                        "hawkeye_event_name": (latest or {}).get("hawkeye_event_name"),
                    }
                )
            return _ExecResult(rows=rows)

        if "FROM events e" in sql and "LEFT JOIN event_external_mappings" in sql:
            rows = []
            for ev in sorted(self._s.get("events", []), key=lambda x: int(x["id"])):
                m = self._s["mappings"].get((int(ev["id"]), ""))
                snaps = [
                    x
                    for x in self._s.get("snapshots", [])
                    if x["event_id"] == int(ev["id"]) and (x.get("scope_key", "") or "") == ""
                ]
                latest = max(snaps, key=lambda x: x["fetched_at"]) if snaps else None
                rows.append(
                    {
                        "event_id": int(ev["id"]),
                        "event_name": ev.get("name"),
                        "event_slug": ev.get("slug"),
                        "parent_event_id": ev.get("parent_event_id"),
                        "external_key": (m or {}).get("external_key"),
                        "mapping_notes": (m or {}).get("notes"),
                        "mapping_updated_at": (m or {}).get("updated_at"),
                        "snapshot_id": (latest or {}).get("id"),
                        "rsvp_invite_sent": (latest or {}).get("rsvp_invite_sent"),
                        "rsvp_accepted": (latest or {}).get("rsvp_accepted"),
                        "checked_in_participants": (latest or {}).get("checked_in_participants"),
                        "snapshot_fetched_at": (latest or {}).get("fetched_at"),
                        "hawkeye_event_name": (latest or {}).get("hawkeye_event_name"),
                    }
                )
            return _ExecResult(rows=rows)

        return _ExecResult(mapping=None)


class HawkeyeMemoryEngine:
    """In-memory engine that understands hawkeye service SQL only."""

    def __init__(self) -> None:
        self.store: dict[str, Any] = {
            "calls": [],
            "events": [],
            "mappings": {},
            "snapshots": [],
            "next_mapping_id": 1,
            "next_snapshot_id": 1,
        }

    def add_event(self, event_id: int, name: str, slug: str | None = None, parent: int | None = None) -> None:
        self.store["events"].append(
            {"id": int(event_id), "name": name, "slug": slug or f"e{event_id}", "parent_event_id": parent}
        )

    @contextmanager
    def begin(self):
        yield _HawkeyeMemConn(self.store)

    @contextmanager
    def connect(self):
        yield _HawkeyeMemConn(self.store)


def test_save_and_get_mapping():
    eng = HawkeyeMemoryEngine()
    hk.save_mapping(eng, 10, "tag-a", notes="n1")
    m = hk.get_mapping(eng, 10)
    assert m is not None
    assert m["external_key"] == "tag-a"
    assert m["notes"] == "n1"


def test_save_mapping_upsert():
    eng = HawkeyeMemoryEngine()
    hk.save_mapping(eng, 5, "first-tag")
    hk.save_mapping(eng, 5, "second-tag", notes=None)
    m = hk.get_mapping(eng, 5)
    assert m["external_key"] == "second-tag"
    upserts = [c for c in eng.store["calls"] if "INSERT INTO event_external_mappings" in c[0]]
    assert len(upserts) == 2
    assert upserts[1][1]["external_key"] == "second-tag"


def test_get_latest_snapshot_none():
    eng = HawkeyeMemoryEngine()
    assert hk.get_latest_snapshot(eng, 99) is None


def test_save_snapshot_and_retrieve():
    eng = HawkeyeMemoryEngine()
    hk.save_mapping(eng, 3, "evt-tag")
    m = hk.get_mapping(eng, 3)
    sid = hk.save_snapshot(eng, 3, m["id"], SAMPLE_RESPONSE, "manual")
    assert sid == 1
    latest = hk.get_latest_snapshot(eng, 3)
    assert latest is not None
    assert latest["rsvp_invite_sent"] == 4
    assert latest["rsvp_accepted"] == 3
    assert latest["checked_in_participants"] == 3
    assert latest["hawkeye_event_name"] == "Test RSVP V1"


def test_sync_event_no_mapping_raises():
    eng = HawkeyeMemoryEngine()
    with pytest.raises(HawkeyeNotConfiguredError):
        hk.sync_event(eng, 404, triggered_by="manual")


def test_sync_event_mocked_fetch(monkeypatch):
    eng = HawkeyeMemoryEngine()
    hk.save_mapping(eng, 7, "test-rsvp-v1")
    inv = MagicMock()
    monkeypatch.setattr(hk, "fetch_from_hawkeye", lambda _tag: dict(SAMPLE_RESPONSE))
    out = hk.sync_event(eng, 7, triggered_by="manual", invalidate_caches=inv)
    assert out["rsvp_invite_sent"] == 4
    assert out["rsvp_accepted"] == 3
    assert out["checked_in_participants"] == 3
    assert out["snapshot_id"] == 1
    inv.assert_called_once()


def test_api_mapping_post(client, monkeypatch, app_mod):
    def _fake_save(engine, event_id, event_tag, notes=None, **kwargs):
        return {
            "id": 42,
            "event_id": event_id,
            "system_name": "hawkeye",
            "external_key": event_tag,
            "notes": notes,
            "created_at": None,
            "updated_at": None,
        }

    monkeypatch.setattr(app_mod.hawkeye_service, "save_mapping", _fake_save)
    rv = client.post(
        "/api/in-person/hawkeye/mapping",
        json={"event_id": 1, "event_tag": "my-tag", "notes": "hello"},
        content_type="application/json",
    )
    assert rv.status_code == 200
    data = rv.get_json()
    assert data["external_key"] == "my-tag"
    assert data["id"] == 42


def test_api_stats_not_configured(client, monkeypatch, app_mod):
    monkeypatch.setattr(app_mod.hawkeye_service, "get_mapping", lambda _e, _x: None)
    rv = client.get("/api/in-person/hawkeye/stats?event_id=999")
    assert rv.status_code == 200
    assert rv.get_json() == {"configured": False}


def test_api_sync_no_mapping(client, monkeypatch, app_mod):
    def _boom(_engine, _eid, **_kw):
        raise HawkeyeNotConfiguredError(123)

    monkeypatch.setattr(app_mod.hawkeye_service, "sync_event", _boom)
    rv = client.post("/api/in-person/hawkeye/sync", json={"event_id": 123})
    assert rv.status_code == 404
    assert "error" in rv.get_json()


def test_list_in_person_events_aggregates_mapping_and_latest():
    eng = HawkeyeMemoryEngine()
    eng.add_event(1, "Mumbai PW")
    eng.add_event(2, "Delhi PW")
    eng.add_event(3, "Pune PW")
    hk.save_mapping(eng, 1, "mumbai-tag")
    hk.save_mapping(eng, 2, "delhi-tag")
    m1 = hk.get_mapping(eng, 1)
    hk.save_snapshot(eng, 1, m1["id"], SAMPLE_RESPONSE, "manual")

    rows = hk.list_in_person_events(eng)
    by_id = {r["event_id"]: r for r in rows}
    assert sorted(by_id.keys()) == [1, 2, 3]
    assert by_id[1]["external_key"] == "mumbai-tag"
    assert by_id[1]["latest"]["rsvp_invite_sent"] == 4
    assert by_id[2]["external_key"] == "delhi-tag"
    assert by_id[2]["latest"] is None
    assert by_id[3]["external_key"] is None
    assert by_id[3]["latest"] is None


def test_save_mapping_and_sync_in_one_call(monkeypatch):
    eng = HawkeyeMemoryEngine()
    eng.add_event(9, "Bengaluru PW")
    monkeypatch.setattr(hk, "fetch_from_hawkeye", lambda _tag: dict(SAMPLE_RESPONSE))
    out = hk.save_mapping_and_sync(eng, 9, "bengaluru-tag", triggered_by="manual")
    assert out["rsvp_invite_sent"] == 4
    assert hk.get_mapping(eng, 9)["external_key"] == "bengaluru-tag"


def test_fetch_from_hawkeye_url_uses_includeemails(monkeypatch):
    captured: dict[str, Any] = {}

    class _Resp:
        status_code = 200
        text = "{}"

        def json(self) -> dict[str, Any]:
            return dict(SAMPLE_RESPONSE)

    def _fake_get(url, headers=None, timeout=None):
        captured["url"] = url
        captured["headers"] = dict(headers or {})
        return _Resp()

    monkeypatch.setenv("HAWKEYE_BASE_URL", "https://hawkeye.hack2skill.com")
    monkeypatch.setenv("HAWKEYE_API_KEY", "secret-key")
    monkeypatch.setattr(hk.requests, "get", _fake_get)
    hk.fetch_from_hawkeye("test-rsvp-v1")
    assert captured["url"] == (
        "https://hawkeye.hack2skill.com/api/api/integrations/hawkeye/events/"
        "test-rsvp-v1/stats?includeEmails=1"
    )
    assert captured["headers"].get("Authorization") == "Bearer secret-key"


def test_api_events_endpoint_per_pw_session(client, monkeypatch, app_mod):
    sessions = [
        {
            "city": "Bengaluru",
            "prompt_war_on_iso": "2026-03-28",
            "session_label": "",
            "display": "Bengaluru · 28 Mar 2026",
            "team_count": 150,
        },
        {
            "city": "Hyderabad",
            "prompt_war_on_iso": "2026-03-20",
            "session_label": "",
            "display": "Hyderabad · 20 Mar 2026",
            "team_count": 115,
        },
    ]

    rows_payload = [
        {
            "event_id": 1,
            "scope_key": "bengaluru|2026-03-28|",
            "city": "Bengaluru",
            "prompt_war_on_iso": "2026-03-28",
            "session_label": "",
            "display": "Bengaluru · 28 Mar 2026",
            "team_count": 150,
            "external_key": "blr-mar-28",
            "notes": None,
            "mapping_updated_at": None,
            "latest": {
                "rsvp_invite_sent": 4,
                "rsvp_accepted": 3,
                "checked_in_participants": 3,
                "fetched_at": "2026-04-10T12:00:00+00:00",
                "hawkeye_event_name": "Test RSVP V1",
            },
        },
        {
            "event_id": 1,
            "scope_key": "hyderabad|2026-03-20|",
            "city": "Hyderabad",
            "prompt_war_on_iso": "2026-03-20",
            "session_label": "",
            "display": "Hyderabad · 20 Mar 2026",
            "team_count": 115,
            "external_key": None,
            "notes": None,
            "mapping_updated_at": None,
            "latest": None,
        },
    ]

    monkeypatch.setattr(app_mod, "_in_person_pw_options", lambda _eid: list(sessions))
    monkeypatch.setattr(
        app_mod.hawkeye_service, "list_pw_session_rows", lambda _e, _eid, _s: list(rows_payload)
    )
    rv = client.get("/api/in-person/hawkeye/events?event_id=1")
    assert rv.status_code == 200
    j = rv.get_json()
    assert j["event_id"] == 1
    assert len(j["events"]) == 2
    by_city = {r["city"]: r for r in j["events"]}
    assert by_city["Bengaluru"]["external_key"] == "blr-mar-28"
    assert by_city["Bengaluru"]["latest"]["rsvp_accepted"] == 3
    assert by_city["Hyderabad"]["external_key"] is None
    assert by_city["Hyderabad"]["latest"] is None


def test_list_pw_session_rows_joins_mapping_and_latest():
    eng = HawkeyeMemoryEngine()
    eng.add_event(1, "Demo In-Person Tour")

    sessions = [
        {"city": "Bengaluru", "prompt_war_on_iso": "2026-03-28", "session_label": "", "display": "Bengaluru · 28 Mar 2026", "team_count": 150},
        {"city": "Pune", "prompt_war_on_iso": "2026-04-25", "session_label": "", "display": "Pune · 25 Apr 2026", "team_count": 73},
    ]

    blr_key = hk.make_pw_session_scope_key("Bengaluru", "2026-03-28", "")
    hk.save_mapping(eng, 1, "blr-tag", scope_key=blr_key, scope={"city": "Bengaluru"})
    m = hk.get_mapping(eng, 1, scope_key=blr_key)
    hk.save_snapshot(eng, 1, m["id"], SAMPLE_RESPONSE, "manual", scope_key=blr_key)

    rows = hk.list_pw_session_rows(eng, 1, sessions)
    assert len(rows) == 2
    by_city = {r["city"]: r for r in rows}
    assert by_city["Bengaluru"]["external_key"] == "blr-tag"
    assert by_city["Bengaluru"]["latest"]["rsvp_invite_sent"] == 4
    assert by_city["Pune"]["external_key"] is None
    assert by_city["Pune"]["latest"] is None


def test_api_fetch_endpoint(client, monkeypatch, app_mod):
    def _fake(engine, event_id, event_tag, triggered_by="manual", **_kw):
        return {
            "snapshot_id": 7,
            "rsvp_invite_sent": 4,
            "rsvp_accepted": 3,
            "checked_in_participants": 3,
            "fetched_at": "2026-04-10T12:00:00+00:00",
            "hawkeye_event_name": "Test RSVP V1",
        }

    monkeypatch.setattr(app_mod.hawkeye_service, "save_mapping_and_sync", _fake)
    rv = client.post(
        "/api/in-person/hawkeye/fetch",
        json={"event_id": 1, "event_tag": "test-rsvp-v1"},
        content_type="application/json",
    )
    assert rv.status_code == 200
    j = rv.get_json()
    assert j["ok"] is True
    assert j["rsvp_invite_sent"] == 4


def test_api_fetch_upstream_failure(client, monkeypatch, app_mod):
    from services.hawkeye import HawkeyeError

    def _bad(*_a, **_kw):
        raise HawkeyeError("Hawkeye returned HTTP 500: oops", status_code=500)

    monkeypatch.setattr(app_mod.hawkeye_service, "save_mapping_and_sync", _bad)
    rv = client.post(
        "/api/in-person/hawkeye/fetch",
        json={"event_id": 1, "event_tag": "broken"},
        content_type="application/json",
    )
    assert rv.status_code == 502
    j = rv.get_json()
    assert j["ok"] is False
    assert "Hawkeye" in j["error"]


def test_summarize_hawkeye_accepted_not_checked_in():
    se = {
        "rsvpAccepted": ["User@Example.com"],
        "checkedInParticipants": ["other@x.com"],
        "rsvpPending": [],
        "rsvpInviteSent": [],
    }
    r = hk.summarize_hawkeye_rsvp_for_email(se, "user@example.com")
    assert r["email_matched"] is True
    assert r["bucket"] == "accepted_not_attended"
    assert r["snapshot_has_emails"] is True


def test_summarize_hawkeye_checked_in_wins_over_accepted():
    se = {
        "rsvpAccepted": ["a@b.com"],
        "checkedInParticipants": ["a@b.com"],
        "rsvpPending": [],
        "rsvpInviteSent": [],
    }
    r = hk.summarize_hawkeye_rsvp_for_email(se, "a@b.com")
    assert r["bucket"] == "checked_in"


def test_summarize_hawkeye_empty_email_lists():
    r = hk.summarize_hawkeye_rsvp_for_email({}, "a@b.com")
    assert r["snapshot_has_emails"] is False
    assert r["email_matched"] is False
    assert "no email bucket" in r["summary"].lower()


def test_get_latest_snapshot_stats_emails_scoped():
    eng = HawkeyeMemoryEngine()
    eng.add_event(1, "Demo")
    sk = hk.make_pw_session_scope_key("Pune", "2026-04-20", "")
    hk.save_mapping(eng, 1, "pune-apr", scope_key=sk)
    m = hk.get_mapping(eng, 1, scope_key=sk)
    payload = {
        **SAMPLE_RESPONSE,
        "statsEmails": {
            "rsvpPending": [],
            "rsvpAccepted": ["x@yz.com"],
            "rsvpInviteSent": [],
            "checkedInParticipants": [],
        },
    }
    hk.save_snapshot(eng, 1, m["id"], payload, "manual", scope_key=sk)
    snap = hk.get_latest_snapshot_stats_emails(eng, 1, sk)
    assert snap is not None
    assert snap["stats_emails"]["rsvpAccepted"] == ["x@yz.com"]
    summ = hk.summarize_hawkeye_rsvp_for_email(snap["stats_emails"], "x@yz.com")
    assert summ["bucket"] == "accepted_not_attended"
