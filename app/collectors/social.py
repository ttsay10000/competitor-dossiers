"""
Social media collector: Twitter and LinkedIn only.
Twitter: via RSS (e.g. Nitter). LinkedIn: authenticated Playwright scraping
(company posts require sign-in; set LINKEDIN_STORAGE_STATE_PATH).
"""
import hashlib
import re
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlparse

from .http import fetch_url
from .press import parse_rss


def _is_rss_url(url: str) -> bool:
    if not url or not isinstance(url, str):
        return False
    u = url.strip().lower()
    return ".xml" in u or "/rss" in u or "rss" in u or "feed" in u or "atom" in u


def _extract_twitter_handle(profile_url: str) -> Optional[str]:
    """Extract @handle or username from twitter.com/x.com profile URL."""
    if not profile_url or not isinstance(profile_url, str):
        return None
    try:
        parsed = urlparse(profile_url.strip())
        path = (parsed.path or "").strip().strip("/")
        if not path:
            return None
        # path can be "CompanyName" or "i/user/CompanyName" etc.
        parts = [p for p in path.split("/") if p and p not in ("i", "user", "intent")]
        return parts[0] if parts else None
    except Exception:
        return None


def _is_linkedin_company_posts_url(url: str) -> bool:
    """True if URL is a LinkedIn company posts page we can scrape."""
    if not url or not isinstance(url, str):
        return False
    u = url.strip().lower()
    return "linkedin.com/company/" in u and ("/posts" in u or "/feed" in u)


def _resolve_feed_url(url: str, platform: str, twitter_rss_bridge_base: Optional[str] = None) -> Optional[str]:
    """
    Return the URL we can fetch for this source, or None if we cannot.
    - If url is already an RSS-like URL, return it.
    - If platform is twitter and we have a bridge base, construct Nitter RSS URL.
    - LinkedIn company posts URLs: return as-is (we scrape via Playwright, not RSS).
    """
    if not url or not isinstance(url, str):
        return None
    url = url.strip()
    if _is_rss_url(url):
        return url
    if platform == "twitter":
        handle = _extract_twitter_handle(url)
        if handle and twitter_rss_bridge_base:
            return f"{twitter_rss_bridge_base}/{handle}/rss"
    if platform == "linkedin" and _is_linkedin_company_posts_url(url):
        return url
    return None


def _normalize_date(value: Any) -> Optional[str]:
    """Return ISO date string or None for post published_at."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        try:
            from email.utils import parsedate_to_datetime
            dt = parsedate_to_datetime(value)
        except Exception:
            try:
                dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except Exception:
                return value[:10] if len(value) >= 10 else value
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _post_id(item: dict, platform: str) -> str:
    """Stable id for dedup and diff."""
    url = (item.get("url") or item.get("link") or "").strip()
    date = (item.get("date") or item.get("published_at") or "").strip()
    text = (item.get("title") or item.get("text") or "").strip()[:200]
    key = f"{platform}|{url}|{date}|{text}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


def collect_social_feed(
    url: str,
    platform: str,
    twitter_rss_bridge_base: Optional[str] = None,
    linkedin_storage_state_path: Optional[str] = None,
) -> dict[str, Any]:
    """
    Fetch one social source (Twitter or LinkedIn). Returns dict with:
    - source_url, raw_content, raw_hash
    - items: list of { id, platform, text, url, published_at, title, source }
    If the URL cannot be resolved, returns empty items.
    """
    feed_url = _resolve_feed_url(url, platform, twitter_rss_bridge_base)
    if not feed_url:
        return {
            "source_url": url,
            "raw_content": "",
            "raw_hash": "",
            "items": [],
        }

    if platform == "linkedin" and _is_linkedin_company_posts_url(feed_url):
        try:
            from .linkedin import collect_linkedin_company_posts
            return collect_linkedin_company_posts(
                feed_url,
                storage_state_path=linkedin_storage_state_path,
                max_age_days=30,
            )
        except Exception:
            return {
                "source_url": url,
                "raw_content": "",
                "raw_hash": "",
                "items": [],
            }

    try:
        items, fetched = parse_rss(feed_url)
    except Exception:
        return {
            "source_url": url,
            "raw_content": "",
            "raw_hash": "",
            "items": [],
        }
    from .linkedin import _is_within_days
    out_items = []
    for entry in items:
        link = entry.get("url") or entry.get("link") or ""
        title = (entry.get("title") or "").strip()
        date = entry.get("date") or entry.get("updated")
        published_at = _normalize_date(date)
        if not _is_within_days(published_at, 30):
            continue
        item = {
            "id": _post_id({"url": link, "date": date, "title": title}, platform),
            "platform": platform,
            "text": title,
            "url": link,
            "published_at": published_at,
            "title": title,
            "source": entry.get("source"),
        }
        out_items.append(item)
    raw_content = fetched.text if hasattr(fetched, "text") else ""
    raw_hash = hashlib.sha256((raw_content or "").encode("utf-8")).hexdigest()
    return {
        "source_url": feed_url,
        "raw_content": raw_content,
        "raw_hash": raw_hash,
        "items": out_items,
    }


def build_structured_json(snapshot: dict[str, Any]) -> dict[str, Any]:
    """
    snapshot has "posts" (list of post dicts from merged feeds, possibly already LLM-enriched).
    Ensure each post has id, platform, text, url, published_at; optional relevance, executive_summary.
    """
    posts = snapshot.get("posts") or []
    out = []
    for p in posts:
        if not isinstance(p, dict):
            continue
        out.append({
            "id": p.get("id") or _post_id(p, p.get("platform") or "unknown"),
            "platform": p.get("platform") or "unknown",
            "text": p.get("text") or p.get("title") or "",
            "url": p.get("url") or "",
            "published_at": p.get("published_at"),
            "engagement": p.get("engagement"),
            "relevance": p.get("relevance"),
            "executive_summary": p.get("executive_summary"),
        })
    return {
        "posts": out,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
