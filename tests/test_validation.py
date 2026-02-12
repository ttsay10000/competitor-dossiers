"""Tests for app.validation (URL format, suggest from domain)."""
import pytest

from app.validation import (
    validate_url_format,
    suggest_urls_from_domain,
    normalize_domain,
)


def test_validate_url_format_valid():
    assert validate_url_format("https://example.com") == (True, None)
    assert validate_url_format("https://example.com/careers") == (True, None)
    assert validate_url_format("http://jobs.lever.co/company") == (True, None)
    assert validate_url_format("") == (True, None)
    assert validate_url_format("   ") == (True, None)


def test_validate_url_format_invalid():
    ok, msg = validate_url_format("not-a-url")
    assert ok is False
    assert "http" in (msg or "").lower() or "host" in (msg or "").lower()

    ok, msg = validate_url_format("ftp://example.com")
    assert ok is False
    assert "http" in (msg or "").lower()

    ok, msg = validate_url_format("https://")
    assert ok is False

    ok, msg = validate_url_format("example")
    assert ok is False


def test_normalize_domain():
    assert normalize_domain("Example.COM") == "example.com"
    assert normalize_domain("https://example.com/path") == "example.com"
    assert normalize_domain("  example.com  ") == "example.com"
    assert normalize_domain("") is None
    assert normalize_domain("   ") is None


def test_suggest_urls_from_domain():
    out = suggest_urls_from_domain("example.com")
    assert "talent" in out
    assert "asset" in out
    assert "press" in out
    assert out["talent"][0] == "https://example.com/careers"
    assert "https://example.com/jobs" in out["talent"]
    assert "https://example.com/locations" in out["asset"]
    assert "https://example.com/blog" in out["press"]

    out_empty = suggest_urls_from_domain("")
    assert out_empty["talent"] == []
    assert out_empty["asset"] == []
    assert out_empty["press"] == []
