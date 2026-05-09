"""Unit tests for ``services.vision_uts_client``."""

from __future__ import annotations

from unittest import mock

import pytest

from services.vision_uts_client import VisionUtsError, build_vision_uts_url, fetch_vision_uts_json


def test_build_vision_uts_url_ok(monkeypatch):
    monkeypatch.setenv("VISION_UTS_BASE_URL", "https://example.com/")
    monkeypatch.setenv("VISION_UTS_EVENT_ID", "12345")
    assert build_vision_uts_url() == "https://example.com/api/v1/event/vision/uts/12345"


def test_build_vision_uts_url_adds_https_scheme(monkeypatch):
    monkeypatch.setenv("VISION_UTS_BASE_URL", "api.example.com")
    monkeypatch.setenv("VISION_UTS_EVENT_ID", "abc123")
    assert build_vision_uts_url() == "https://api.example.com/api/v1/event/vision/uts/abc123"


def test_build_vision_uts_url_hex_event_id(monkeypatch):
    monkeypatch.setenv("VISION_UTS_BASE_URL", "https://vision.example")
    monkeypatch.setenv("VISION_UTS_EVENT_ID", "69cca4b245645a10e1172e76")
    assert build_vision_uts_url() == (
        "https://vision.example/api/v1/event/vision/uts/69cca4b245645a10e1172e76"
    )


def test_build_vision_uts_url_missing(monkeypatch):
    monkeypatch.delenv("VISION_UTS_BASE_URL", raising=False)
    monkeypatch.delenv("VISION_UTS_EVENT_ID", raising=False)
    with pytest.raises(VisionUtsError, match="BASE_URL"):
        build_vision_uts_url()


def test_fetch_retries_on_503_then_success(monkeypatch):
    monkeypatch.setenv("VISION_UTS_BASE_URL", "https://uts.test")
    monkeypatch.setenv("VISION_UTS_EVENT_ID", "1")
    monkeypatch.setenv("VISION_UTS_API_KEY", "secret")
    body503 = mock.Mock(status_code=503, text="busy")
    body200 = mock.Mock(status_code=200, text="{}")
    body200.json = mock.Mock(return_value={"data": []})

    calls = {"n": 0}

    def fake_get(*_a, **_k):
        calls["n"] += 1
        if calls["n"] < 3:
            return body503
        return body200

    with mock.patch("services.vision_uts_client.requests.get", side_effect=fake_get):
        with mock.patch("services.vision_uts_client.time.sleep", lambda _s: None):
            out = fetch_vision_uts_json()
    assert out == {"data": []}
    assert calls["n"] == 3


def test_fetch_sends_bearer_when_key_set(monkeypatch):
    monkeypatch.setenv("VISION_UTS_BASE_URL", "https://uts.test")
    monkeypatch.setenv("VISION_UTS_EVENT_ID", "9")
    monkeypatch.setenv("VISION_UTS_API_KEY", "tok")
    resp = mock.Mock(status_code=200, text="{}")
    resp.json = mock.Mock(return_value={"ok": True})

    with mock.patch("services.vision_uts_client.requests.get", return_value=resp) as rg:
        fetch_vision_uts_json()
    kwargs = rg.call_args.kwargs
    assert kwargs["headers"]["Authorization"] == "Bearer tok"
