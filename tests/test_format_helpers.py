"""Unit tests for the small formatting helpers in app.py."""

from __future__ import annotations


def test_fmt_int_basic(app_mod):
    assert app_mod._fmt_int(0) == "0"
    assert app_mod._fmt_int(7) == "7"
    assert app_mod._fmt_int(1234) == "1,234"
    assert app_mod._fmt_int(1_234_567) == "1,234,567"


def test_fmt_int_accepts_floats(app_mod):
    # int(...) coercion truncates floats
    assert app_mod._fmt_int(12.9) == "12"


def test_fmt_credits_zero(app_mod):
    assert app_mod._fmt_credits(0) == "0"


def test_fmt_credits_small_integer(app_mod):
    assert app_mod._fmt_credits(42) == "42"


def test_fmt_credits_small_decimal(app_mod):
    assert app_mod._fmt_credits(42.5) == "42.50"


def test_fmt_credits_thousands(app_mod):
    assert app_mod._fmt_credits(1500) == "1.5k"
    assert app_mod._fmt_credits(2000) == "2k"
    assert app_mod._fmt_credits(12_500) == "12.5k"


def test_fmt_credits_millions(app_mod):
    assert app_mod._fmt_credits(2_500_000) == "2.5M"
    assert app_mod._fmt_credits(3_000_000) == "3M"


def test_fmt_credits_negative(app_mod):
    assert app_mod._fmt_credits(-1500) == "-1.5k"
    assert app_mod._fmt_credits(-42) == "-42"
