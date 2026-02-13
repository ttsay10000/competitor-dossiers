import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin
from typing import Any, Optional, Tuple

import feedparser
from bs4 import BeautifulSoup

from .http import fetch_url, FetchResult


def _parse_publication_date_from_text(text: str) -> Optional[str]:
    """Parse common publication date formats from page text. Returns ISO date string or None."""
    if not text or not isinstance(text, str):
        return None
    text = text.strip()
    if not text:
        return None
    # ISO date (YYYY-MM-DD or with time)
    m = re.search(r"\b(20\d{2}-\d{2}-\d{2})(?:T[\d:.]+Z?)?\b", text)
    if m:
        try:
            dt = datetime.fromisoformat(m.group(1).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.strftime("%Y-%m-%d")
        except Exception:
            pass
    # "Jan 1, 2025" / "Jan 01, 2025"
    m = re.search(
        r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},?\s*20\d{2}\b",
        text,
        re.IGNORECASE,
    )
    if m:
        try:
            # Normalize "Jan 1, 2025" for strptime
            s = m.group(0).replace(",", "")
            parts = s.split()
            if len(parts) >= 3:
                month_abbr = parts[0][:3].title()
                day = parts[1].zfill(2)
                year = parts[2]
                dt = datetime.strptime(f"{month_abbr} {day} {year}", "%b %d %Y")
                dt = dt.replace(tzinfo=timezone.utc)
                return dt.strftime("%Y-%m-%d")
        except Exception:
            pass
    # RFC 2822 style (e.g. "Mon, 10 Feb 2025 12:00:00 GMT")
    m = re.search(
        r"\b(Mon|Tue|Wed|Thu|Fri|Sat|Sun),?\s+\d{1,2}\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+20\d{2}\s+[\d:]+\s*(?:GMT|UTC)?",
        text,
        re.IGNORECASE,
    )
    if m:
        try:
            dt = parsedate_to_datetime(m.group(0).strip())
            return dt.strftime("%Y-%m-%d")
        except Exception:
            pass
    return None


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


def _find_date_for_link(link) -> Optional[str]:
    """Look for publication date in the link's container: <time datetime>, then date-like text."""
    node = link
    for _ in range(6):
        if node is None:
            break
        time_tag = node.find("time", datetime=True)
        if time_tag:
            dt_val = time_tag.get("datetime", "").strip()
            if dt_val:
                parsed = _parse_publication_date_from_text(dt_val)
                if parsed:
                    return parsed
                try:
                    dt = datetime.fromisoformat(dt_val.replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    return dt.strftime("%Y-%m-%d")
                except Exception:
                    pass
        # Date-like text in container (e.g. "Feb 10, 2025", "2025-02-10")
        text = node.get_text(" ", strip=True) if hasattr(node, "get_text") else ""
        if text:
            parsed = _parse_publication_date_from_text(text)
            if parsed:
                return parsed
        node = node.parent
    return None


def extract_press_from_html(html: str, base_url: Optional[str] = None) -> list[dict[str, Any]]:
    """Extract press links from HTML. Tries to parse publication date from container (e.g. <time>, or date text); never uses fetch/pull time.
    If base_url is provided, relative hrefs are resolved to absolute URLs so downstream company-domain filtering works."""
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
        url = href if href.startswith("http") else (urljoin(base_url or "", href) if base_url else href)
        date_val = _find_date_for_link(link)
        # Only keep links that have a publication date so we don't treat blog category/nav links (e.g. "City Guides", "Seasonal Travel") as news.
        if date_val is None:
            continue
        items.append({"title": title, "url": url, "date": date_val, "source": None})
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
    items = extract_press_from_html(fetched.text, base_url=fetched.url or source_url)
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
