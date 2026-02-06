"""Homepage / product page collector for digital footprint change detection."""

from datetime import datetime
from typing import Any

from .http import fetch_url, fetch_url_js


def collect_homepage_snapshot(source_url: str, js_required: bool = False) -> dict[str, Any]:
    """Fetch a single URL (homepage or product page); return raw content and hash."""
    if js_required:
        fetched = fetch_url_js(source_url)
    else:
        fetched = fetch_url(source_url)
    return {
        "source_url": fetched.url,
        "raw_content": fetched.text,
        "raw_hash": fetched.raw_hash,
        "status_code": fetched.status_code,
    }


def build_structured_json(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_url": snapshot.get("source_url"),
        "fetched_at": datetime.utcnow().isoformat(),
        "raw_hash": snapshot.get("raw_hash"),
    }
