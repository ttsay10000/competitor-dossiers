"""
LinkedIn company posts collector.

Fetches posts from LinkedIn company pages via authenticated Playwright.
LinkedIn requires sign-in to view company feeds. Set LINKEDIN_STORAGE_STATE_PATH
to a saved Playwright storage state (from a logged-in session) to enable collection.

To create the storage state, run: python3 -m scripts.save_linkedin_session

Note: LinkedIn's ToS restricts scraping. Use for internal competitive intelligence only.
"""
import hashlib
import json
import re
import time
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .http import CHROMIUM_LAUNCH_ARGS, USER_AGENT_BROWSER


def _extract_from_voyager_response(body: str) -> list[dict]:
    """Try to extract post-like items from a Voyager API JSON response."""
    posts = []
    try:
        data = json.loads(body) if isinstance(body, str) else body
    except (json.JSONDecodeError, TypeError):
        return posts

    def walk(obj, depth=0):
        if depth > 10:
            return
        if isinstance(obj, dict):
            # Look for common Voyager post structures
            if obj.get("$type") and "Share" in str(obj.get("$type", "")):
                content = obj.get("commentary", {}) or obj.get("message", {}) or {}
                if isinstance(content, dict):
                    text_obj = content.get("text") or content.get("attributedText") or content
                    text = ""
                    if isinstance(text_obj, dict):
                        text = (text_obj.get("text") or "")[:2000]
                    elif isinstance(text_obj, str):
                        text = text_obj[:2000]
                    if text:
                        urn = obj.get("entityUrn") or obj.get("urn") or ""
                        posts.append({
                            "urn": urn,
                            "text": text.strip(),
                            "published_at": _parse_voyager_time(obj),
                        })
            for v in obj.values():
                walk(v, depth + 1)
        elif isinstance(obj, list):
            for item in obj:
                walk(item, depth + 1)

    walk(data)
    return posts


def _parse_voyager_time(obj: dict) -> Optional[str]:
    """Extract ISO timestamp from Voyager object."""
    for key in ("createdAt", "publishedAt", "lastModifiedAt", "created"):
        val = obj.get(key)
        if val is None:
            continue
        if isinstance(val, (int, float)):
            try:
                dt = datetime.fromtimestamp(val / 1000.0 if val > 1e12 else val, tz=timezone.utc)
                return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            except (OSError, ValueError):
                pass
        if isinstance(val, str) and len(val) >= 10:
            return val[:19] + "Z" if not val.endswith("Z") else val
    return None


def _parse_relative_date(s: str) -> Optional[datetime]:
    """Parse relative date strings like '2d', '1w', '3h', '5mo' into datetime (UTC)."""
    if not s or not isinstance(s, str):
        return None
    s = s.strip().lower()
    now = datetime.now(timezone.utc)
    # Patterns: 1m, 2h, 3d, 1w, 2mo, 1y or "2 hours ago", "3 days ago"
    m = re.match(r"^(\d+)\s*(m|min|minute|minutes|h|hr|hour|hours|d|day|days|w|week|weeks|mo|month|months|y|year|years)o?$", s)
    if m:
        from datetime import timedelta
        n = int(m.group(1))
        unit = m.group(2).lower()
        if unit in ("mo", "month", "months"):
            return now - timedelta(days=n * 30)
        if unit in ("m", "min", "minute", "minutes"):
            return now - timedelta(minutes=n)
        if unit in ("h", "hr", "hour", "hours"):
            return now - timedelta(hours=n)
        if unit in ("d", "day", "days"):
            return now - timedelta(days=n)
        if unit in ("w", "week", "weeks"):
            return now - timedelta(weeks=n)
        if unit in ("y", "year", "years"):
            return now - timedelta(days=n * 365)
    m = re.match(r"^(\d+)\s+(?:minute|hour|day|week)s?\s+ago$", s)
    if m:
        from datetime import timedelta
        n = int(m.group(1))
        if "minute" in s:
            return now - timedelta(minutes=n)
        if "hour" in s:
            return now - timedelta(hours=n)
        if "day" in s:
            return now - timedelta(days=n)
        if "week" in s:
            return now - timedelta(weeks=n)
    return None


def _is_within_days(published_at: Any, max_days: int) -> bool:
    """True if published_at is within the last max_days. Excludes undateable posts."""
    if published_at is None:
        return False
    cutoff = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    from datetime import timedelta
    cutoff = cutoff - timedelta(days=max_days)

    if isinstance(published_at, str):
        # Try ISO format first
        try:
            dt = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt >= cutoff
        except (ValueError, TypeError):
            pass
        # Try relative date
        dt = _parse_relative_date(published_at)
        if dt:
            return dt >= cutoff
        return False

    if isinstance(published_at, (int, float)):
        try:
            dt = datetime.fromtimestamp(
                published_at / 1000.0 if published_at > 1e12 else published_at,
                tz=timezone.utc,
            )
            return dt >= cutoff
        except (OSError, ValueError):
            return False

    return False


def _extract_from_dom(html: str, base_url: str) -> list[dict]:
    """Fallback: extract post-like blocks from DOM. LinkedIn uses obfuscated classes."""
    posts = []
    soup = BeautifulSoup(html, "html.parser")

    # LinkedIn often uses article elements or divs with data-urn for feed items
    candidates = (
        soup.find_all("article", limit=50)
        or soup.find_all("div", attrs={"data-urn": True}, limit=50)
        or soup.select("[role='article']")
    )
    if not candidates:
        # Broader: look for feed-shared or similar patterns in class
        candidates = soup.find_all("div", class_=re.compile(r"feed-shared|social-details|update-components", re.I), limit=50)

    seen_texts: set[str] = set()
    for el in candidates:
        # Skip tiny elements (likely UI chrome)
        text = (el.get_text(separator=" ", strip=True) or "")[:2000]
        if len(text) < 30:
            continue
        # Dedupe by normalized text
        key = text[:300].replace("\n", " ").strip()
        if key in seen_texts:
            continue
        seen_texts.add(key)

        # Try to find a link to the post
        link_el = el.find("a", href=re.compile(r"/feed/update|/posts/|/activity/"))
        post_url = ""
        if link_el and link_el.get("href"):
            post_url = urljoin("https://www.linkedin.com", link_el["href"])

        # Try to get a relative date (e.g. "2d", "1w") - we'll store as-is
        date_str = None
        for span in el.find_all(["span", "time"]):
            t = (span.get_text() or "").strip()
            if re.match(r"^\d+[mhdw]o?$", t, re.I) or re.match(r"\d+ (?:minute|hour|day|week)s? ago", t, re.I):
                date_str = t
                break

        posts.append({
            "urn": el.get("data-urn", ""),
            "text": text.strip(),
            "url": post_url,
            "published_at": date_str,
        })

    return posts


def _post_id(item: dict, platform: str = "linkedin") -> str:
    """Stable id for dedup."""
    key = f"{platform}|{item.get('urn','')}|{item.get('url','')}|{item.get('text','')[:200]}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


def collect_linkedin_company_posts(
    url: str,
    storage_state_path: Optional[str] = None,
    max_posts: int = 30,
    scroll_pauses: int = 3,
    max_age_days: int = 30,
) -> dict[str, Any]:
    """
    Fetch company posts from a LinkedIn company page (e.g. /company/xyz/posts/).

    Requires authenticated session via storage_state_path. Without auth, LinkedIn
    returns a login page and no posts will be extracted.

    Only includes posts within max_age_days (default 30). Posts with unparseable
    dates are excluded.

    Returns:
        dict with source_url, raw_content, raw_hash, items (list of post dicts)
    """
    from ..config import settings

    if not getattr(settings, "playwright_enabled", False):
        return {
            "source_url": url,
            "raw_content": "",
            "raw_hash": "",
            "items": [],
        }

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {
            "source_url": url,
            "raw_content": "",
            "raw_hash": "",
            "items": [],
        }

    voyager_posts: list[dict] = []
    html_content = ""

    def handle_response(response):
        nonlocal voyager_posts
        try:
            req_url = response.url
            if "/voyager/api/" in req_url and ("feed" in req_url or "updates" in req_url or "ugcPosts" in req_url):
                body = response.text()
                if body and len(body) > 100:
                    extracted = _extract_from_voyager_response(body)
                    for p in extracted:
                        if p.get("text") and p not in voyager_posts:
                            voyager_posts.append(p)
        except Exception:
            pass

    with sync_playwright() as p:
        launch_opts: dict[str, Any] = {"headless": True, "args": CHROMIUM_LAUNCH_ARGS}
        browser = p.chromium.launch(**launch_opts)

        context_opts: dict[str, Any] = {
            "user_agent": USER_AGENT_BROWSER,
            "viewport": {"width": 1280, "height": 800},
            "locale": "en-US",
        }
        if storage_state_path:
            try:
                from pathlib import Path
                if Path(storage_state_path).exists():
                    context_opts["storage_state"] = storage_state_path
            except Exception:
                pass

        context = browser.new_context(**context_opts)
        context.set_extra_http_headers({"Accept-Language": "en-US,en;q=0.9"})
        page = context.new_page()

        page.on("response", handle_response)
        page.goto(url, wait_until="networkidle", timeout=45000)
        time.sleep(2)

        # Scroll to trigger loading more posts
        for _ in range(scroll_pauses):
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(1.5)

        html_content = page.content()
        context.close()
        browser.close()

    # Prefer Voyager data; fall back to DOM
    if voyager_posts:
        raw_items = voyager_posts[:max_posts]
    else:
        raw_items = _extract_from_dom(html_content, url)[:max_posts]

    out_items = []
    for r in raw_items:
        text = (r.get("text") or "").strip()
        if not text:
            continue
        pub_at = r.get("published_at")
        if not _is_within_days(pub_at, max_age_days):
            continue
        post_url = r.get("url") or ""
        if not post_url and r.get("urn"):
            post_url = url

        item = {
            "id": _post_id(r),
            "platform": "linkedin",
            "text": text,
            "title": text[:200] + ("..." if len(text) > 200 else ""),
            "url": post_url,
            "published_at": pub_at,
            "source": "linkedin",
        }
        out_items.append(item)

    raw_hash = hashlib.sha256(html_content.encode("utf-8")).hexdigest()
    return {
        "source_url": url,
        "raw_content": html_content[:50000],
        "raw_hash": raw_hash,
        "items": out_items,
    }
