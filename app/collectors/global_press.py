from datetime import datetime, timedelta
import json
import re
from typing import Any, List, Optional
from urllib.parse import quote_plus, urljoin

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

    Implementation notes:
    - Uses BI's search endpoint (?q=...) and, when available, the JSON wrapper that
      includes a 'rendered' HTML field.
    - Parses ISO-like timestamps that appear near result cards and applies a 90-day window.
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

        # Look for an ISO-like timestamp close to this link by inspecting its ancestors' text.
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

    Implementation notes:
    - Uses CNBC's search page with tab=news and sort=recent.
    - Parses <time> elements when available; falls back to simple pattern matching.
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

        # Try to parse a date from <time> tag or card text.
        dt: Optional[datetime] = None
        time_tag = card.find("time")
        if time_tag is not None:
            # Many sites store ISO string in datetime attribute; otherwise text may be parseable.
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

    This function expects `maybe_ticker` to be a valid ticker symbol (e.g. "ABNB").
    For now we use a simple heuristic: the string must be 1–6 characters, alphanumeric,
    and uppercase. If it does not look like a ticker, we skip and return [] so that
    private companies are unaffected until an explicit mapping is introduced.
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

        # Look for a datetime attribute on a nearby <time> tag, or ISO-like text.
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

