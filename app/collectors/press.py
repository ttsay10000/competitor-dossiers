from datetime import datetime, timezone
from typing import Any, Tuple

import feedparser
from bs4 import BeautifulSoup

from .http import fetch_url, FetchResult


def parse_rss(url: str) -> Tuple[list[dict[str, Any]], FetchResult]:
    """Parse RSS/Atom feed. Date is publication date only (never fetch/pull time)."""
    fetched = fetch_url(url)
    feed = feedparser.parse(fetched.text)
    items = []
    for entry in feed.entries:
        # Prefer published (first publication); use updated only if published is missing.
        pub_date = entry.get("published") or entry.get("updated")
        items.append(
            {
                "title": entry.get("title"),
                "url": entry.get("link"),
                "date": pub_date,
                "source": feed.feed.get("title"),
            }
        )
    return items, fetched


def extract_press_from_html(html: str) -> list[dict[str, Any]]:
    """Extract press links from HTML. Date is left None unless we can parse publication date from the page (never use fetch/pull time)."""
    soup = BeautifulSoup(html, "html.parser")
    items = []
    for link in soup.find_all("a"):
        title = (link.get_text() or "").strip()
        href = link.get("href")
        if not title or not href:
            continue
        if len(title) < 6 or len(title) > 140:
            continue
        if "press" not in href and "blog" not in href and "news" not in href:
            continue
        # Only use publication date when we can parse it; never set date to fetch time.
        items.append({"title": title, "url": href, "date": None, "source": None})
    return items


def collect_press_snapshot(source_url: str) -> dict[str, Any]:
    if source_url.endswith(".xml") or "rss" in source_url:
        items, fetched = parse_rss(source_url)
        return {
            "source_url": fetched.url,
            "raw_content": fetched.text,
            "raw_hash": fetched.raw_hash,
            "items": items,
        }

    fetched = fetch_url(source_url)
    items = extract_press_from_html(fetched.text)
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
