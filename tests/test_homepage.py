"""Tests for homepage / website changes flow: collector and rules."""

import pytest

from app.collectors.homepage import (
    build_composite_hash,
    build_structured_json,
    content_hash,
    extract_visible_text,
    normalize_noise,
)
from app.rules.homepage_rules import (
    detect_coming_soon_phrases,
    build_homepage_updated_event,
    build_coming_soon_event,
)


def test_extract_visible_text_strips_script_style():
    html = "<html><body><p>Hello</p><script>alert(1)</script><style>.x{}</style><p>World</p></body></html>"
    text = extract_visible_text(html)
    assert "Hello" in text
    assert "World" in text
    assert "alert" not in text
    assert ".x" not in text


def test_normalize_noise_removes_dates_and_copyright():
    # Date and "all rights reserved" are stripped; content before copyright is preserved.
    text = "Real content here. Updated 3/15/2024. All rights reserved."
    out = normalize_noise(text)
    assert "Real content here" in out
    # Copyright line (© 2024 ...) is removed; date pattern removed
    text2 = "Leading sentence. © 2024 Acme Inc."
    out2 = normalize_noise(text2)
    assert "Leading sentence" in out2


def test_content_hash_stable_for_same_normalized_text():
    a = content_hash("  hello   world  ")
    b = content_hash("hello world")
    assert a == b


def test_content_hash_different_for_different_content():
    a = content_hash("page one")
    b = content_hash("page two")
    assert a != b


def test_build_composite_hash_deterministic():
    pages = [
        {"source_url": "https://a.com/", "raw_hash": "h1"},
        {"source_url": "https://b.com/", "raw_hash": "h2"},
    ]
    h1 = build_composite_hash(pages)
    h2 = build_composite_hash(list(reversed(pages)))
    assert h1 == h2  # sorted by url


def test_build_structured_json_single_page():
    snapshot = {
        "source_url": "https://example.com/",
        "raw_content": "<html><body><p>Coming soon</p></body></html>",
        "raw_hash": "abc",
    }
    out = build_structured_json(snapshot, detect_coming_soon=detect_coming_soon_phrases)
    assert "pages" in out
    assert len(out["pages"]) == 1
    assert out["pages"][0]["url"] == "https://example.com/"
    assert out["pages"][0]["raw_hash"] == "abc"
    assert "content_hash" in out["pages"][0]
    assert "coming soon" in (out["pages"][0].get("coming_soon_phrases") or [])


def test_detect_coming_soon_phrases():
    assert "coming soon" in detect_coming_soon_phrases("We are coming soon to your city.")
    assert "beta" in detect_coming_soon_phrases("Sign up for our BETA.")
    assert not detect_coming_soon_phrases("No signal here.")


def test_build_homepage_updated_event():
    ev = build_homepage_updated_event("https://example.com/page")
    assert ev["type"] == "narrative.homepage_updated"
    assert ev["evidence"]["source_url"] == "https://example.com/page"
    assert "occurred_at" in ev


def test_build_coming_soon_event():
    ev = build_coming_soon_event("https://example.com/", "coming soon")
    assert ev["type"] == "narrative.coming_soon"
    assert ev["evidence"]["source_url"] == "https://example.com/"
    assert ev["evidence"]["phrase"] == "coming soon"
    assert "occurred_at" in ev
