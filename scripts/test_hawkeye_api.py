#!/usr/bin/env python3
"""
Smoke-test the Hawkeye stats API from the terminal (no DB, no Flask).

Reads HAWKEYE_BASE_URL and HAWKEYE_API_KEY from the environment. If a .env file
exists next to app.py (project root), loads it via python-dotenv.

Example:
  python scripts/test_hawkeye_api.py test-rsvp-v1
  python scripts/test_hawkeye_api.py my-tag --base-url https://hawkeye.hack2skill.com
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.parse import quote

import requests

ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_BASE = "https://hawkeye.hack2skill.com"
_STATS_PATH = "/api/api/integrations/hawkeye/events/{tag}/stats"


def _load_dotenv() -> None:
    env_path = ROOT / ".env"
    if not env_path.is_file():
        return
    try:
        from dotenv import load_dotenv

        load_dotenv(env_path)
    except ImportError:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="GET Hawkeye event stats (prints JSON to stdout).")
    parser.add_argument(
        "event_tag",
        help="Hawkeye eventTag slug (URL path segment, e.g. test-rsvp-v1)",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("HAWKEYE_BASE_URL", _DEFAULT_BASE).strip().rstrip("/"),
        help=f"Hawkeye API base (no trailing slash). Default: env HAWKEYE_BASE_URL or {_DEFAULT_BASE}",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="Request timeout in seconds (default: 30)",
    )
    parser.add_argument(
        "--no-bearer",
        action="store_true",
        help="Do not send Authorization header even if HAWKEYE_API_KEY is set",
    )
    args = parser.parse_args()

    _load_dotenv()

    tag = (args.event_tag or "").strip()
    if not tag:
        print("error: event_tag is empty", file=sys.stderr)
        return 2

    base = (args.base_url or "").strip().rstrip("/")
    if not base:
        print("error: base URL is empty", file=sys.stderr)
        return 2

    key = (os.environ.get("HAWKEYE_API_KEY") or "").strip()
    url = f"{base}{_STATS_PATH.format(tag=quote(tag, safe=''))}?includeEmails=1"

    headers = {"Accept": "application/json"}
    if key and not args.no_bearer:
        headers["Authorization"] = f"Bearer {key}"

    print(f"GET {url}", file=sys.stderr)
    if key and not args.no_bearer:
        print("Authorization: Bearer ***", file=sys.stderr)
    elif key and args.no_bearer:
        print("HAWKEYE_API_KEY is set but --no-bearer was passed (no Authorization header)", file=sys.stderr)
    else:
        print("HAWKEYE_API_KEY not set (no Authorization header)", file=sys.stderr)

    try:
        resp = requests.get(url, headers=headers, timeout=(5, float(args.timeout)))
    except requests.RequestException as exc:
        print(f"request failed: {exc}", file=sys.stderr)
        return 1

    print(f"HTTP {resp.status_code}", file=sys.stderr)
    body = (resp.text or "").strip()
    if not body:
        print("(empty body)", file=sys.stderr)
        return 0 if resp.status_code == 200 else 1

    try:
        data = resp.json()
        print(json.dumps(data, indent=2, ensure_ascii=False))
    except ValueError:
        print(body)
        return 1 if resp.status_code != 200 else 0

    return 0 if resp.status_code == 200 else 1


if __name__ == "__main__":
    raise SystemExit(main())
