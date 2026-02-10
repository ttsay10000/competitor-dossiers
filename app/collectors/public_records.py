"""Public records collector: trademark filings, regulatory filings, etc."""

from datetime import datetime, timezone
from typing import Any

import feedparser
from bs4 import BeautifulSoup

from .http import fetch_url

# Links or titles containing these are treated as public-record signals.
FILING_KEYWORDS = [
    "trademark", "uspto", "sec ", "filing", "registration", "regulatory",
    "patent", "incorporation", "d/b/a", "doing business",
]


def _looks_like_filing(href: str, text: str) -> bool:
    combined = f"{(href or '').lower()} {(text or '').lower()}"
    return any(kw in combined for kw in FILING_KEYWORDS)


def extract_filings_from_html(html: str, source_url: str) -> list[dict[str, Any]]:
    """Extract links that look like trademark/regulatory filings."""
    soup = BeautifulSoup(html, "html.parser")
    items = []
    seen_urls = set()
    for link in soup.find_all("a", href=True):
        href = link.get("href") or ""
        title = (link.get_text() or "").strip()
        if not _looks_like_filing(href, title):
            continue
        if not title or len(title) > 200:
            title = href
        if href in seen_urls:
            continue
        seen_urls.add(href)
        if not href.startswith("http"):
            continue
        items.append({"title": title, "url": href, "date": None, "source": source_url})
    return items


def collect_public_records_snapshot(source_url: str) -> dict[str, Any]:
    """Fetch URL (HTML or RSS) and extract filing-like items."""
    if source_url.endswith(".xml") or "rss" in source_url.lower():
        fetched = fetch_url(source_url)
        feed = feedparser.parse(fetched.text)
        items = []
        for entry in feed.entries:
            title = entry.get("title") or ""
            url = entry.get("link") or ""
            if not _looks_like_filing(url, title):
                continue
            items.append({
                "title": title,
                "url": url,
                "date": entry.get("published") or entry.get("updated"),
                "source": feed.feed.get("title"),
            })
        return {
            "source_url": fetched.url,
            "raw_content": fetched.text,
            "raw_hash": fetched.raw_hash,
            "items": items,
        }

    fetched = fetch_url(source_url)
    items = extract_filings_from_html(fetched.text, fetched.url)
    return {
        "source_url": fetched.url,
        "raw_content": fetched.text,
        "raw_hash": fetched.raw_hash,
        "items": items,
    }


def build_structured_json(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_url": snapshot.get("source_url"),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "items": snapshot.get("items", []),
    }
