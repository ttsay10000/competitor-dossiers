"""Homepage / product page collector for digital footprint change detection."""

import hashlib
import re
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from bs4 import BeautifulSoup

from .http import fetch_url, fetch_url_js

# Substrings/regex to strip from visible text so content_hash ignores cosmetic noise.
NOISE_PATTERNS = [
    re.compile(r"today'?s?\s+date", re.I),
    re.compile(r"\d{1,2}/\d{1,2}/\d{2,4}"),
    re.compile(r"(?:last\s+)?updated\s*:?\s*[\w\s,/-]+", re.I),
    re.compile(r"©\s*\d{4}(?:-\d{4})?\s*[\w\s.,]+", re.I),
    re.compile(r"cookie\s*(?:notice|banner|policy)", re.I),
    re.compile(r"we\s+use\s+cookies", re.I),
    re.compile(r"all\s+rights\s+reserved", re.I),
]


def extract_visible_text(html: str) -> str:
    """Extract visible body text from HTML; drop script/style; normalize whitespace."""
    if not (html or "").strip():
        return ""
    try:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup.find_all(["script", "style", "noscript"]):
            tag.decompose()
        body = soup.find("body") or soup
        text = body.get_text(separator="\n", strip=True) if body else ""
    except Exception:
        text = html[:50000] if html else ""
    # Normalize: collapse runs of whitespace to single space, then single newlines
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n", "\n", text)
    return text.strip()


def normalize_noise(text: str) -> str:
    """Remove common non-meaningful bits so content_hash is stable for cosmetic changes."""
    out = text
    for pat in NOISE_PATTERNS:
        out = pat.sub(" ", out)
    out = re.sub(r"\s+", " ", out).strip()
    return out


def content_hash(text: str) -> str:
    """SHA256 of normalized visible text for meaningful-change detection."""
    normalized = normalize_noise(text) if text else ""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def collect_homepage_snapshot(source_url: str, js_required: bool = False) -> dict[str, Any]:
    """Fetch a single URL (homepage or product page); return raw content and hash."""
    if js_required:
        try:
            fetched = fetch_url_js(source_url)
        except (RuntimeError, Exception):
            fetched = fetch_url(source_url)
    else:
        fetched = fetch_url(source_url)
    return {
        "source_url": fetched.url,
        "raw_content": fetched.text,
        "raw_hash": fetched.raw_hash,
        "status_code": fetched.status_code,
    }


def build_composite_hash(pages: list[dict[str, Any]]) -> str:
    """Deterministic hash from sorted url:raw_hash so skip works across all pages."""
    parts = sorted((p.get("source_url") or "", p.get("raw_hash") or "") for p in pages)
    blob = "\n".join(f"{u}:{h}" for u, h in parts)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# Max chars of visible text stored per page for LLM diff/interpretation (old vs new).
VISIBLE_TEXT_SNIPPET_LEN = 4000


def _structured_page(page: dict[str, Any], detect_phrases: Callable[[str], list[str]]) -> dict[str, Any]:
    """Build one entry for structured_json.pages: url, raw_hash, content_hash, coming_soon_phrases, visible_text_snippet."""
    url = page.get("source_url") or ""
    raw_hash = page.get("raw_hash") or ""
    raw = (page.get("raw_content") or "").strip()
    text = extract_visible_text(raw) if raw else ""
    ch = content_hash(text)
    phrases = detect_phrases(text) if text else []
    snippet = (text or "")[:VISIBLE_TEXT_SNIPPET_LEN]
    return {
        "url": url,
        "raw_hash": raw_hash,
        "content_hash": ch,
        "coming_soon_phrases": phrases,
        "visible_text_snippet": snippet,
    }


def build_structured_json(
    snapshot: dict[str, Any],
    *,
    detect_coming_soon: Optional[Callable[[str], list[str]]] = None,
) -> dict[str, Any]:
    """Build structured JSON for one or multiple pages.

    - If snapshot has "pages" (list of page dicts with source_url, raw_content, raw_hash),
      returns { "pages": [ { url, raw_hash, content_hash, coming_soon_phrases }, ... ], "fetched_at" }.
    - Else treats snapshot as single page (legacy); returns same shape with one element in "pages".
    detect_coming_soon(text) -> list of phrase/snippet strings; if None, coming_soon_phrases are [].
    """
    noop = lambda t: []
    detect = detect_coming_soon if detect_coming_soon is not None else noop
    fetched_at = datetime.now(timezone.utc).isoformat()

    if "pages" in snapshot and isinstance(snapshot["pages"], list):
        pages = snapshot["pages"]
    else:
        pages = [snapshot]

    structured_pages = [_structured_page(p, detect) for p in pages]
    return {
        "pages": structured_pages,
        "fetched_at": fetched_at,
    }
