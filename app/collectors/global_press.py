"""
Global press collectors (BI, CNBC, Yahoo, Google News, PR Newswire).

All items use "date" = publication date only. We never set date to fetch/pull/upload time.
When we cannot determine publication date, we leave date as None.
"""
from datetime import datetime, timedelta, timezone
import json
import re
from typing import Any, List, Optional
from urllib.parse import quote_plus, urljoin

import feedparser
from bs4 import BeautifulSoup

from .http import fetch_url, USER_AGENT_BROWSER


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

    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    query = quote_plus(company_name)
    # json=1 often returns a small JSON envelope with 'rendered' HTML, but plain HTML
    # also works — we handle both paths.
    search_url = f"https://www.businessinsider.com/s?q={query}&json=1"

    try:
        fetched = fetch_url(search_url, headers={"User-Agent": USER_AGENT_BROWSER})
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

    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    query = quote_plus(company_name)
    search_url = f"https://www.cnbc.com/search/?query={query}&tab=news&sort=recent"

    try:
        fetched = fetch_url(search_url, headers={"User-Agent": USER_AGENT_BROWSER})
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

    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    url = f"https://finance.yahoo.com/quote/{ticker}/news/"

    try:
        fetched = fetch_url(url, headers={"User-Agent": USER_AGENT_BROWSER})
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


def _fetch_google_news_rss(
    quoted_phrase: str,
    when_days: int,
    max_items: int,
    cutoff: datetime,
) -> List[dict]:
    """Fetch and parse Google News RSS for a single quoted phrase. Returns list of items.
    Google News RSS accepts when:Nd (days) but returns empty feed for when:Nm (months).
    We use when:{when_days}d in the query and also filter by cutoff in code.
    """
    query = f'"{quoted_phrase}" when:{when_days}d'
    encoded = quote_plus(query)
    rss_url = (
        f"https://news.google.com/rss/search?q={encoded}&hl=en-US&gl=US&ceid=US:en"
    )
    try:
        fetched = fetch_url(rss_url, timeout=25, headers={"User-Agent": USER_AGENT_BROWSER})
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
        dt: Optional[datetime] = None
        if entry.get("published_parsed"):
            try:
                import calendar
                ts = calendar.timegm(entry.published_parsed)
                dt = datetime.fromtimestamp(ts, tz=timezone.utc)
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


def collect_google_news_items(
    company_name: str,
    max_items: int = 40,
    window_days: int = 120,
) -> List[dict]:
    """
    Fetch Google News articles that contain the company name as an exact phrase
    (quoted search). Date on each item is publication date only (from RSS
    published/published_parsed; never fetch time).

    Uses when:Nd (days) in the query; Google News RSS accepts days but returns empty for when:Nm (months).
    We also enforce the window in code by dropping entries older than cutoff.
    For multi-word names (e.g. "Lark Hotels") we fetch both the full phrase and
    the first word ("Lark") and merge so we get headlines that use either
    (e.g. "Lark appoints...", "Lark Hotels to open..."). Downstream LLM
    classification filters irrelevant matches. Company blog links are filtered
    out by the pipeline (company_domains).
    """
    company_name = (company_name or "").strip()
    if not company_name:
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)

    results = _fetch_google_news_rss(company_name, window_days, max_items, cutoff)
    seen_urls = {item["url"] for item in results}

    # Multi-word names: also fetch with first word and merge. Many headlines use
    # only the brand (e.g. "Lark appoints...", "Lark Expands...") while others use
    # the full name ("Lark Hotels renews..."). We want both, not only one.
    if " " in company_name:
        first_word = company_name.split()[0].strip()
        if first_word and first_word.lower() != company_name.lower():
            extra = _fetch_google_news_rss(first_word, window_days, max_items, cutoff)
            for item in extra:
                if item["url"] not in seen_urls and len(results) < max_items:
                    seen_urls.add(item["url"])
                    results.append(item)

    # Supplementary: partnership/deal stories often appear under "X partnership" or
    # "X partners with Y" and can be ranked differently. Fetch with quoted "Company partnership"
    # and merge so we don't miss stories like "Hilton partners with Placemakr".
    seen_urls = {item["url"] for item in results}
    partnership_phrase = f"{company_name} partnership"
    extra = _fetch_google_news_rss(partnership_phrase, window_days, max_items, cutoff)
    for item in extra:
        if item["url"] not in seen_urls and len(results) < max_items:
            seen_urls.add(item["url"])
            results.append(item)

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

    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    query = quote_plus(company_name)
    # Try max 100 results per page; PR Newswire may cap server-side.
    search_url = (
        f"https://www.prnewswire.com/search/all/?keyword={query}&pagesize=100"
    )

    html_content: Optional[str] = None
    try:
        fetched = fetch_url(search_url, timeout=25, headers={"User-Agent": USER_AGENT_BROWSER})
        if fetched.status_code == 200 and fetched.text:
            html_content = fetched.text
    except Exception:
        pass

    # PR Newswire search results are JS-rendered; initial HTML often has no article links (only nav links).
    # If we have no release-style links (path contains /news-releases/ and ends with .html or has long path), try Playwright.
    def _count_release_links(html: str) -> int:
        if not html:
            return 0
        s = BeautifulSoup(html, "html.parser")
        n = 0
        for a in s.find_all("a", href=True):
            h = (a.get("href") or "").strip()
            if "/news-releases/" not in h or "prnewswire.com" not in h:
                continue
            if ".html" in h or "/news-releases/" in h.split("?")[0]:
                n += 1
        return n

    release_links = _count_release_links(html_content or "")
    if release_links < 2:
        try:
            from ..config import settings
            if getattr(settings, "playwright_enabled", False):
                from .http import fetch_url_js
                js_fetched = fetch_url_js(search_url)
                if js_fetched.text and len(js_fetched.text) > 1000:
                    html_content = js_fetched.text
        except Exception:
            pass

    if not html_content:
        return []

    soup = BeautifulSoup(html_content, "html.parser")
    results: List[dict] = []
    seen_urls: set[str] = set()
    name_lower = company_name.lower()
    name_slug = re.sub(r"[^a-z0-9]", "", name_lower)

    def _title_mentions_company(title_text: str, url_for_fallback: str = "") -> bool:
        t = (title_text or "").lower()
        if name_lower in t:
            return True
        if name_slug and name_slug in re.sub(r"[^a-z0-9]", "", t):
            return True
        first_word = name_lower.split()[0] if name_lower.split() else ""
        if len(first_word) >= 4 and first_word in t:
            return True
        if url_for_fallback and (name_lower in url_for_fallback.lower() or name_slug in re.sub(r"[^a-z0-9]", "", url_for_fallback.lower())):
            return True
        return False

    def _slug_to_title(slug: str) -> str:
        if not slug:
            return "Press release"
        return slug.replace("-", " ").strip()[:300]

    # Rescue: include article if body contains partnership/collaboration phrasing re company (e.g. "The Code" hotel with Avantstay).
    _rescue_fetches_left = 10  # cap to avoid rate limits

    def _article_mentions_partnership_with_company(article_url: str, company: str) -> bool:
        nonlocal _rescue_fetches_left
        if _rescue_fetches_left <= 0:
            return False
        _rescue_fetches_left -= 1
        try:
            resp = fetch_url(article_url, timeout=15, headers={"User-Agent": USER_AGENT_BROWSER})
            if resp.status_code != 200 or not resp.text:
                return False
            # Use raw HTML for the check: partnership/collaboration text may live in script/JSON (e.g. PR Newswire).
            body_lower = resp.text.lower()[:15000]
            company_lower = (company or "").strip().lower()
            if not company_lower:
                return False
            partnership_phrases = (
                "in collaboration with",
                "in partnership with",
                "partnership with",
                "collaboration with",
                "partnered with",
                "collaborating with",
            )
            for phrase in partnership_phrases:
                idx = body_lower.find(phrase)
                if idx == -1:
                    continue
                snippet = body_lower[max(0, idx - 40) : idx + len(phrase) + 100]
                if company_lower in snippet:
                    return True
                company_slug = re.sub(r"[^a-z0-9]", "", company_lower)
                if company_slug and company_slug in re.sub(r"[^a-z0-9]", "", snippet):
                    return True
        except Exception:
            pass
        return False

    # 1) Find all <a> links to PR Newswire news releases.
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
            parent = a.parent
            if parent:
                title = (parent.get_text() or "").strip()[:200]
            if not title or len(title) < 10:
                continue
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
            title = re.sub(r"^[A-Za-z]{3}\s+\d{1,2},\s*\d{4}[^:]*:\s*", "", title).strip()
        if not _title_mentions_company(title, href):
            if not _article_mentions_partnership_with_company(href, company_name):
                continue
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
            return results

    # 2) Fallback: PR Newswire injects result links via JS but often embeds URLs in the HTML (e.g. in script/data).
    #    Extract relative paths /news-releases/...html and build items so we get articles without Playwright.
    #    Slug is lowercase and ends with -<id>; strip the ID and title-case for display.
    if len(results) < 2 and html_content:
        path_pattern = re.compile(r"/news-releases/([^\s\"'<>?]+\.html)")
        for m in path_pattern.finditer(html_content):
            path = "/news-releases/" + m.group(1).split("?")[0].strip()
            href = urljoin("https://www.prnewswire.com", path)
            if href in seen_urls:
                continue
            slug = m.group(1).replace(".html", "").replace("-", " ")
            slug = re.sub(r"\s+\d{7,}$", "", slug).strip()  # strip trailing numeric ID (e.g. 302576370)
            title = slug[:300].title() if len(slug) >= 10 else f"Press release: {company_name}"
            if not _title_mentions_company(title, href):
                if not _article_mentions_partnership_with_company(href, company_name):
                    continue
            seen_urls.add(href)
            results.append(
                {
                    "title": title[:300],
                    "url": href,
                    "date": None,
                    "source": "PR Newswire",
                    "provider": "prnewswire",
                }
            )
            if len(results) >= max_items:
                break

    return results

