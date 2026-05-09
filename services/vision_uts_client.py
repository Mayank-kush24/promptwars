"""
HTTP client for Vision UTS Virtual Datacenter API.

No Flask imports. Optional Bearer auth when ``VISION_UTS_API_KEY`` is set.
"""

from __future__ import annotations

import os
import re
import time
from typing import Any
from urllib.parse import quote

import requests

_VISION_UTS_PATH = "/api/v1/event/vision/uts/{event_id}"
# Vision event ids are often Mongo-style hex strings, not only decimal digits.
_EVENT_ID_SAFE = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")

# Stable advisory lock namespace (used by vision_uts_sync).
ADVISORY_LOCK_KEY_1 = 1_853_272_190
ADVISORY_LOCK_KEY_2 = 90_010_001


class VisionUtsError(Exception):
    """Configuration, transport, or HTTP failure for Vision UTS."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _normalize_vision_uts_base_url(raw: str) -> str:
    s = (raw or "").strip().rstrip("/")
    if not s:
        return ""
    low = s.lower()
    if not (low.startswith("http://") or low.startswith("https://")):
        s = "https://" + s
    return s.rstrip("/")


def build_vision_uts_url() -> str:
    base = _normalize_vision_uts_base_url(os.environ.get("VISION_UTS_BASE_URL") or "")
    eid_raw = (os.environ.get("VISION_UTS_EVENT_ID") or "").strip()
    if not base:
        raise VisionUtsError("VISION_UTS_BASE_URL is not set (empty after trim)")
    if not eid_raw:
        raise VisionUtsError("VISION_UTS_EVENT_ID is not set")
    if not _EVENT_ID_SAFE.match(eid_raw):
        raise VisionUtsError(
            "VISION_UTS_EVENT_ID must be 1–128 characters: letters, digits, hyphen, or underscore"
        )
    eid_path = quote(eid_raw, safe="-_")
    return f"{base}{_VISION_UTS_PATH.format(event_id=eid_path)}"


def _request_headers() -> dict[str, str]:
    headers: dict[str, str] = {"Accept": "application/json"}
    key = (os.environ.get("VISION_UTS_API_KEY") or "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def _request_timeout() -> tuple[int, int]:
    return (
        max(1, _env_int("VISION_UTS_CONNECT_TIMEOUT_SEC", 10)),
        max(1, _env_int("VISION_UTS_READ_TIMEOUT_SEC", 60)),
    )


def fetch_vision_uts_json() -> dict[str, Any]:
    """
    GET the event payload once. Retries up to 3 times on transient errors with
    sleeps 5s, 15s, 45s between attempts.
    """
    url = build_vision_uts_url()
    headers = _request_headers()
    timeout = _request_timeout()
    backoff_sec = (5, 15, 45)
    for attempt in range(len(backoff_sec) + 1):
        try:
            resp = requests.get(url, headers=headers, timeout=timeout)
            last_status = resp.status_code
            if resp.status_code != 200:
                body = (resp.text or "")[:500]
                if attempt < len(backoff_sec) and last_status in (429, 500, 502, 503, 504):
                    time.sleep(backoff_sec[attempt])
                    continue
                raise VisionUtsError(
                    f"Vision UTS returned HTTP {resp.status_code}: {body}",
                    status_code=last_status,
                )
            try:
                data = resp.json()
            except ValueError as exc:
                raise VisionUtsError("Vision UTS response is not valid JSON") from exc
            if not isinstance(data, dict):
                raise VisionUtsError("Vision UTS JSON root must be an object")
            return data
        except VisionUtsError:
            raise
        except (requests.Timeout, requests.ConnectionError) as exc:
            if attempt < len(backoff_sec):
                time.sleep(backoff_sec[attempt])
                continue
            raise VisionUtsError(f"Vision UTS request failed after retries: {exc}") from exc
        except requests.RequestException as exc:
            raise VisionUtsError(f"Vision UTS request failed: {exc}") from exc
