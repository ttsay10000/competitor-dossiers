"""
Global press collectors (BI, CNBC, Yahoo, Google News, PR Newswire).

All items use "date" = publication date only. We never set date to fetch/pull/upload time.
When we cannot determine publication date, we leave date as None.
"""
from datetime import datetime, timedelta
import json
import re
from typing import Any, List, Optional
from urllib.parse import quote_plus, urljoin

import feedparser
from bs4 import BeautifulSoup

from .http import fetch_url


def _parse_iso_date(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    val = value.strip()
    if not val:
        return None
    try:
        # Handle trailing Z from ISO-8601
        return datetime.fromisoformat(val.replace("Z", "+00:00"))
    except Exception:
        return None


def _within_window(dt: Optional[datetime], cutoff: datetime) -> bool:
    if dt is None:
        # When we cannot parse a date, keep the item but rely on per-source caps.
        return True
    return dt >= cutoff


def collect_business_insider_items(
    company_name: str,
    max_items: int = 25,
    window_days: int = 90,
) -> List[dict]:
    """
    Fetch recent Business Insider stories that mention the given company name.
    Date on each item is publication date only (from card/listing; never fetch time).

    Implementation notes:
    - Uses BI's search endpoint (?q=...) and, when available, the JSON wrapper that
      includes a 'rendered' HTML field.
    - Parses publication-date timestamps near result cards (ISO-like); 90-day window.
    - Best-effort and defensive: on any parsing issue, returns an empty list instead of failing.
    """
    company_name = (company_name or "").strip()
    if not company_name:
        return []

    cutoff = datetime.utcnow() - timedelta(days=window_days)
    query = quote_plus(company_name)
    # json=1 often returns a small JSON envelope with 'rendered' HTML, but plain HTML
    # also works — we handle both paths.
    search_url = f"https://www.businessinsider.com/s?q={query}&json=1"

    try:
        fetched = fetch_url(search_url)
    except Exception:
        return []
    if fetched.status_code != 200 or not fetched.text:
        return []

    text = fetched.text
    html = text
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            html = data.get("rendered") or ""
    except Exception:
        # Not JSON; treat entire response body as HTML.
        html = text

    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    results: List[dict] = []
    seen_urls: set[str] = set()

    # Heuristic: search results are rendered as article cards with links to BI stories.
    # We look for anchor tags that link to businessinsider.com and have a reasonably long title.
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        title = (a.get_text() or "").strip()
        if not href or not title or len(title) < 6:
            continue
        if "businessinsider.com" not in href:
            continue
        # Avoid navigation and generic links.
        if href.startswith("#"):
            continue
        # Normalize URL to absolute.
        url = urljoin(fetched.url, href)
        if url in seen_urls:
            continue

        # Publication date only: look for timestamp near the card (never use fetch time).
        dt: Optional[datetime] = None
        container = a
        for _ in range(3):
            if container.parent is None:
                break
            container = container.parent
        text_block = container.get_text(" ", strip=True)
        m = re.search(r"\d{4}-\d{2}-\d{2}T[0-9:.]+Z", text_block)
        if m:
            dt = _parse_iso_date(m.group(0))

        if not _within_window(dt, cutoff):
            continue

        seen_urls.add(url)
        results.append(
            {
                "title": title,
                "url": url,
                "date": dt.isoformat() if dt else None,
                "source": "Business Insider",
                "provider": "business_insider",
            }
        )
        if len(results) >= max_items:
            break

    return results


def collect_cnbc_items(
    company_name: str,
    max_items: int = 20,
    window_days: int = 90,
) -> List[dict]:
    """
    Fetch recent CNBC news items that mention the given company name.
    Date on each item is publication date only (<time> or listing; never fetch time).

    Implementation notes:
    - Uses CNBC's search page with tab=news and sort=recent.
    - Parses <time datetime> (publication date) when available; fallback to ISO pattern.
    - Applies a 90-day window and per-run cap.
    """
    company_name = (company_name or "").strip()
    if not company_name:
        return []

    cutoff = datetime.utcnow() - timedelta(days=window_days)
    query = quote_plus(company_name)
    search_url = f"https://www.cnbc.com/search/?query={query}&tab=news&sort=recent"

    try:
        fetched = fetch_url(search_url)
    except Exception:
        return []
    if fetched.status_code != 200 or not fetched.text:
        return []

    soup = BeautifulSoup(fetched.text, "html.parser")
    results: List[dict] = []
    seen_urls: set[str] = set()

    # CNBC search results usually contain <a> links inside cards, often with a <time> tag.
    for card in soup.find_all(["article", "div", "li"]):
        # Find link first
        a = card.find("a", href=True)
        if not a:
            continue
        href = (a.get("href") or "").strip()
        title = (a.get_text() or "").strip()
        if not href or not title or len(title) < 6:
            continue
        if "cnbc.com" not in href:
            continue
        if href.startswith("#"):
            continue
        url = urljoin(fetched.url, href)
        if url in seen_urls:
            continue

        # Publication date only: <time datetime> or ISO in card (never fetch/upload time).
        dt: Optional[datetime] = None
        time_tag = card.find("time")
        if time_tag is not None:
            candidate = time_tag.get("datetime") or time_tag.get_text(strip=True)
            dt = _parse_iso_date(candidate)
        if dt is None:
            text_block = card.get_text(" ", strip=True)
            m = re.search(r"\d{4}-\d{2}-\d{2}T[0-9:.]+Z", text_block)
            if m:
                dt = _parse_iso_date(m.group(0))

        if not _within_window(dt, cutoff):
            continue

        seen_urls.add(url)
        results.append(
            {
                "title": title,
                "url": url,
                "date": dt.isoformat() if dt else None,
                "source": "CNBC",
                "provider": "cnbc",
            }
        )
        if len(results) >= max_items:
            break

    return results


def collect_yahoo_finance_items(
    maybe_ticker: str,
    max_items: int = 20,
    window_days: int = 90,
) -> List[dict]:
    """
    Fetch recent Yahoo Finance news items for a given ticker.
    Date on each item is publication date only (<time> or listing; never fetch time).

    Expects `maybe_ticker` to be a valid ticker symbol (e.g. "ABNB").
    Simple heuristic: 1–6 chars, alphanumeric, uppercase; else return [] for private companies.
    """
    ticker = (maybe_ticker or "").strip().upper()
    if not ticker or len(ticker) > 6 or not ticker.isalnum():
        return []

    cutoff = datetime.utcnow() - timedelta(days=window_days)
    url = f"https://finance.yahoo.com/quote/{ticker}/news/"

    try:
        fetched = fetch_url(url)
    except Exception:
        return []
    if fetched.status_code != 200 or not fetched.text:
        return []

    soup = BeautifulSoup(fetched.text, "html.parser")
    results: List[dict] = []
    seen_urls: set[str] = set()

    # Yahoo Finance news pages generally render stories as <a> links inside cards
    # under the /news/ path. We filter for those and try to read any ISO-like
    # datetime strings that appear nearby.
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        title = (a.get_text() or "").strip()
        if not href or not title or len(title) < 6:
            continue
        if "/news/" not in href:
            continue
        if href.startswith("#"):
            continue
        full_url = urljoin(fetched.url, href)
        if full_url in seen_urls:
            continue

        # Publication date only: <time datetime> or ISO in card (never fetch time).
        dt: Optional[datetime] = None
        card = a
        for _ in range(3):
            if card.parent is None:
                break
            card = card.parent
        time_tag = card.find("time")
        if time_tag is not None:
            candidate = time_tag.get("datetime") or time_tag.get_text(strip=True)
            dt = _parse_iso_date(candidate)
        if dt is None:
            text_block = card.get_text(" ", strip=True)
            m = re.search(r"\d{4}-\d{2}-\d{2}T[0-9:.]+Z", text_block)
            if m:
                dt = _parse_iso_date(m.group(0))

        if not _within_window(dt, cutoff):
            continue

        seen_urls.add(full_url)
        results.append(
            {
                "title": title,
                "url": full_url,
                "date": dt.isoformat() if dt else None,
                "source": f"Yahoo Finance ({ticker})",
                "provider": "yahoo_finance",
            }
        )
        if len(results) >= max_items:
            break

    return results


def collect_google_news_items(
    company_name: str,
    max_items: int = 40,
    window_days: int = 30,
) -> List[dict]:
    """
    Fetch Google News articles from the past 30 days that contain the company name
    as an exact phrase (quoted search). Date on each item is publication date only
    (from RSS published/published_parsed; never fetch time).

    Uses Google News RSS search: q="CompanyName" when:1m for past month.
    Duplicates/noise are handled by the pipeline's LLM classification and dedup.
    """
    company_name = (company_name or "").strip()
    if not company_name:
        return []

    cutoff = datetime.utcnow() - timedelta(days=window_days)
    # Quoted phrase so we only get articles that contain the exact name (e.g. "Avantstay").
    query = f'"{company_name}" when:1m'
    encoded = quote_plus(query)
    rss_url = (
        f"https://news.google.com/rss/search?q={encoded}&hl=en-US&gl=US&ceid=US:en"
    )

    try:
        fetched = fetch_url(rss_url, timeout=25)
    except Exception:
        return []
    if fetched.status_code != 200 or not fetched.text:
        return []

    feed = feedparser.parse(fetched.text)
    results: List[dict] = []
    seen_urls: set[str] = set()

    for entry in feed.entries:
        if len(results) >= max_items:
            break
        title = (entry.get("title") or "").strip()
        link = (entry.get("link") or "").strip()
        if not title or not link or len(title) < 6:
            continue
        if link in seen_urls:
            continue
        # Publication date only: RSS "published" / published_parsed (never updated or fetch time).
        dt: Optional[datetime] = None
        if entry.get("published_parsed"):
            try:
                import calendar
                ts = calendar.timegm(entry.published_parsed)
                dt = datetime.utcfromtimestamp(ts)
            except Exception:
                pass
        if dt is None and entry.get("published"):
            dt = _parse_iso_date(entry.published)
        if dt and dt < cutoff:
            continue
        seen_urls.add(link)
        results.append(
            {
                "title": title,
                "url": link,
                "date": dt.isoformat() if dt else None,
                "source": "Google News",
                "provider": "google_news",
            }
        )

    return results


def collect_prnewswire_items(
    company_name: str,
    max_items: int = 100,
    window_days: int = 90,
) -> List[dict]:
    """
    Fetch PR Newswire press releases for the company by searching with company name.
    Date on each item is the release publication date (from card text; never upload/fetch time).
    Used as the second-priority news source (after user-provided company news links).
    """
    company_name = (company_name or "").strip()
    if not company_name:
        return []

    cutoff = datetime.utcnow() - timedelta(days=window_days)
    query = quote_plus(company_name)
    # Try max 100 results per page; PR Newswire may cap server-side.
    search_url = (
        f"https://www.prnewswire.com/search/all/?keyword={query}&pagesize=100"
    )

    try:
        fetched = fetch_url(search_url, timeout=25)
    except Exception:
        return []
    if fetched.status_code != 200 or not fetched.text:
        return []

    soup = BeautifulSoup(fetched.text, "html.parser")
    results: List[dict] = []
    seen_urls: set[str] = set()

    # Find all links to PR Newswire news releases.
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if "/news-releases/" not in href or "prnewswire.com" not in href:
            continue
        if not href.startswith("http"):
            href = urljoin("https://www.prnewswire.com", href)
        if href in seen_urls:
            continue
        title = (a.get_text() or "").strip()
        if not title or len(title) < 10:
            # Try parent or sibling for title/date.
            parent = a.parent
            if parent:
                title = (parent.get_text() or "").strip()[:200]
            if not title or len(title) < 10:
                continue
        # Publication date only: parse release date from card (e.g. "Jan 21, 2026, 11:00 ET").
        date_match = re.search(
            r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2},\s*\d{4}",
            title,
            re.IGNORECASE,
        )
        dt: Optional[datetime] = None
        if date_match:
            try:
                dt = datetime.strptime(date_match.group(0), "%b %d, %Y")
                if dt and dt < cutoff:
                    continue
            except Exception:
                pass
            # Clean title: remove leading "Mon DD, YYYY, HH:MM ET " if present.
            title = re.sub(r"^[A-Za-z]{3}\s+\d{1,2},\s*\d{4}[^:]*:\s*", "", title).strip()
        seen_urls.add(href)
        results.append(
            {
                "title": title[:300],
                "url": href,
                "date": dt.isoformat() if dt else None,
                "source": "PR Newswire",
                "provider": "prnewswire",
            }
        )
        if len(results) >= max_items:
            break

    return results

