import json
import re
import gzip
from datetime import datetime
from urllib.parse import urljoin, urlparse, urlencode, urlunparse, parse_qs
from typing import Any, Dict, List, Optional

import requests
from bs4 import BeautifulSoup

from .http import fetch_url, fetch_url_js, fetch_url_js_exhaust

# Max characters of page text to send to LLM for property extraction (fit context, control cost).
_LLM_EXTRACT_MAX_CHARS = 14_000


def discover_sitemap(url: str) -> list[str]:
    """Build sitemap URLs from origin only (scheme + netloc), not the full path."""
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else url
    if origin.endswith("/"):
        origin = origin.rstrip("/")
    return [f"{origin}/sitemap.xml", f"{origin}/sitemap.xml.gz"]


def extract_links_from_sitemap(xml_text: str) -> list[str]:
    soup = BeautifulSoup(xml_text, "xml")
    urls = []
    for loc in soup.find_all("loc"):
        if loc.text:
            urls.append(loc.text.strip())
    return urls


def expand_sitemap(url: str) -> list[str]:
    fetched = fetch_url(url)
    if fetched.status_code != 200:
        return []

    if url.endswith(".gz"):
        try:
            xml_text = gzip.decompress(fetched.text.encode("utf-8")).decode("utf-8")
        except Exception:
            return []
    else:
        xml_text = fetched.text

    urls = extract_links_from_sitemap(xml_text)
    return urls


def is_property_like(url: str) -> bool:
    """True if URL looks like a property/location page (for HTML link and sitemap filtering)."""
    patterns = [
        r"/properties/",
        r"/property/",
        r"/portfolio/",  # e.g. Lark: larkhospitality.com/portfolio/property-slug
        r"/locations/",
        r"/apartments/",
        r"/homes/",
        r"/destinations/",
        r"/search",
        r"/listing/",
        r"/stay/",
        r"/vacation-rentals/",
        # Placemakr-style single-segment city-state paths (e.g. /saltlakecity-ut)
        r"/[a-z0-9]+-[a-z0-9]+(?:\?|$|/)",
    ]
    return any(re.search(pattern, url, re.IGNORECASE) for pattern in patterns)


def extract_properties_from_html(html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    properties = []
    for link in soup.find_all("a"):
        href = link.get("href") or ""
        text = (link.get_text() or "").strip()
        if not href or len(text) < 3:
            continue
        if not is_property_like(href):
            continue
        properties.append(
            {
                "url": href,
                "name": text,
                "market": None,
                "status": None,
            }
        )
    return properties


def _html_to_text_for_llm(html: str, max_chars: int = _LLM_EXTRACT_MAX_CHARS) -> str:
    """Reduce HTML to plain text for LLM (strip scripts, get body text, truncate)."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(["script", "style", "noscript"]):
        tag.decompose()
    body = soup.find("body") or soup
    text = body.get_text(separator="\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n[... truncated]"
    return text


def _extract_properties_via_llm(html: str, page_url: str) -> List[dict[str, Any]]:
    """
    Use OpenAI to extract property names (and URLs when present) from portfolio-style page content.
    Returns list of dicts with "name" and optionally "url" (absolute). Requires OPENAI_API_KEY.
    """
    try:
        from ..config import settings
        if not settings.openai_api_key:
            return []
    except Exception:
        return []
    try:
        from openai import OpenAI
    except ImportError:
        return []

    text = _html_to_text_for_llm(html)
    parsed = urlparse(page_url)
    base_url = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else page_url

    system = (
        "You are extracting a list of properties (hotels, apartments, vacation rentals, etc.) from web page text. "
        "Return a JSON array of objects. Each object must have: \"name\" (string, the property/location name). "
        "If the page provides a URL or path to that property, include \"url\" (string). "
        "Use only the exact names and URLs from the content. Skip navigation, footers, and non-property items. "
        "Output only the JSON array, no markdown or explanation."
    )
    user = f"Page base URL: {base_url}\n\nExtract all properties from this page text:\n\n{text}"

    try:
        client = OpenAI(api_key=settings.openai_api_key)
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            max_tokens=2000,
            temperature=0.1,
        )
        content = (resp.choices[0].message.content or "").strip()
        # Allow optional markdown code fence
        if content.startswith("```"):
            content = re.sub(r"^```\w*\n?", "", content).rstrip("`\n")
        raw = json.loads(content)
        if not isinstance(raw, list):
            return []
        out = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            name = (item.get("name") or "").strip()
            if not name:
                continue
            url_val = (item.get("url") or "").strip()
            if url_val and not url_val.startswith("http"):
                url_val = urljoin(base_url, url_val)
            out.append({"name": name, "url": url_val or None, "market": None, "status": None})
        return out
    except Exception:
        return []


def normalize_properties(properties: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = []
    for prop in properties:
        normalized.append(
            {
                "url": prop.get("url"),
                "name": (prop.get("name") or "").strip(),
                "market": (prop.get("market") or "").strip() or None,
                "status": (prop.get("status") or "").strip() or None,
            }
        )
    return normalized


def _get_by_path(data: Any, path: str) -> Any:
    """Get nested key, e.g. 'data.items' -> data['data']['items']."""
    if not path:
        return data
    for key in path.split("."):
        if isinstance(data, dict) and key in data:
            data = data[key]
        else:
            return None
    return data


def fetch_from_api(api_config: Dict[str, Any], source_url: str) -> List[dict[str, Any]]:
    """
    Fetch list from JSON API, with optional pagination. Map to property dicts.
    api_config: url (or use source_url), list_path (e.g. 'data.items'), name_key, url_key, market_key, status_key;
                optional pagination: param ('page'), start (1), max_pages (100).
    """
    api_url = api_config.get("url") or source_url
    list_path = api_config.get("list_path", "")
    name_key = api_config.get("name_key", "name")
    url_key = api_config.get("url_key", "url")
    market_key = api_config.get("market_key", "market")
    status_key = api_config.get("status_key", "status")
    pagination = api_config.get("pagination") or {}
    param_name = pagination.get("param", "page")
    start = pagination.get("start", 1)
    max_pages = pagination.get("max_pages", 100)

    all_items: List[dict[str, Any]] = []
    page = start
    seen = 0

    while page - start < max_pages:
        if param_name:
            parsed = list(urlparse(api_url))
            qs = parse_qs(parsed[4])
            qs[param_name] = [str(page)]
            parsed[4] = urlencode(qs, doseq=True)
            request_url = urlunparse(parsed)
        else:
            request_url = api_url
            if page > start:
                break
        resp = requests.get(request_url, timeout=30, headers={"User-Agent": "competitor-signals/0.1"})
        if resp.status_code != 200:
            break
        try:
            data = resp.json()
        except Exception:
            break
        raw_list = _get_by_path(data, list_path) if list_path else data
        if not isinstance(raw_list, list):
            raw_list = [raw_list] if raw_list is not None else []
        if not raw_list:
            break
        for item in raw_list:
            if not isinstance(item, dict):
                continue
            all_items.append({
                "url": item.get(url_key) or item.get("permalink") or "",
                "name": (item.get(name_key) or item.get("title") or "").strip(),
                "market": (item.get(market_key) or "").strip() or None,
                "status": (item.get(status_key) or "").strip() or None,
            })
        seen = len(raw_list)
        if not pagination:
            break
        page += 1
        if seen == 0:
            break

    return all_items


def _playwright_available() -> bool:
    """True if Playwright is enabled and importable (e.g. not on lean Render build)."""
    try:
        from ..config import settings
        if not settings.playwright_enabled:
            return False
        from playwright.sync_api import sync_playwright  # noqa: F401
        return True
    except ImportError:
        return False


def collect_asset_snapshot(
    source_url: str,
    js_required: bool = False,
    use_sitemap_first: bool = False,
    extra_options: Optional[Dict[str, Any]] = None,
) -> dict[str, Any]:
    opts = extra_options or {}
    strategy = opts.get("strategy")
    # Infer strategy: prefer sitemap_first when set so we can run without Playwright (e.g. on Render).
    if not strategy:
        if js_required and opts.get("load_more"):
            strategy = "js_exhaust"
        elif use_sitemap_first:
            strategy = "sitemap_first"
        elif js_required:
            strategy = "js"
        else:
            strategy = "html"

    def fetch_from_sitemap() -> Optional[Dict[str, Any]]:
        for sitemap_url in discover_sitemap(source_url):
            urls = expand_sitemap(sitemap_url)
            if not urls:
                continue
            property_urls = [url for url in urls if is_property_like(url)]
            # Only return sitemap result when we found property-like URLs so caller can fall back to HTML.
            if not property_urls:
                continue
            return {
                "source_url": sitemap_url,
                "raw_content": None,
                "raw_hash": None,
                "properties": normalize_properties([{"url": url, "name": url} for url in property_urls]),
            }
        return None

    def fetch_from_html() -> dict[str, Any]:
        fetched = fetch_url(source_url)
        if fetched.status_code != 200:
            raise RuntimeError(
                f"Asset fetch failed: {fetched.url} returned HTTP {fetched.status_code}. "
                "Refusing to parse or persist; check Runs for this error."
            )
        properties = extract_properties_from_html(fetched.text)
        return {
            "source_url": fetched.url,
            "raw_content": fetched.text,
            "raw_hash": fetched.raw_hash,
            "properties": normalize_properties(properties),
        }

    def _fetch_without_browser() -> dict[str, Any]:
        """Sitemap first, then HTML; used when JS/Playwright is not available (e.g. Render cron).
        If opts.llm_extract is True and OPENAI_API_KEY is set, HTML is parsed via LLM to extract
        properties from card-style content (e.g. Lark portfolio)."""
        sitemap_snapshot = fetch_from_sitemap()
        if sitemap_snapshot and sitemap_snapshot.get("properties"):
            return {**sitemap_snapshot, "note": "sitemap_first"}
        fetched = fetch_url(source_url)
        if fetched.status_code != 200:
            raise RuntimeError(
                f"Asset fetch failed: {fetched.url} returned HTTP {fetched.status_code}. "
                "Refusing to parse or persist; check Runs for this error."
            )
        if opts.get("llm_extract"):
            properties = _extract_properties_via_llm(fetched.text, source_url)
            if not properties:
                properties = extract_properties_from_html(fetched.text)
                return {
                    "source_url": fetched.url,
                    "raw_content": fetched.text,
                    "raw_hash": fetched.raw_hash,
                    "properties": normalize_properties(properties),
                    "note": "html",
                }
            return {
                "source_url": fetched.url,
                "raw_content": fetched.text,
                "raw_hash": fetched.raw_hash,
                "properties": normalize_properties(properties),
                "note": "llm",
            }
        properties = extract_properties_from_html(fetched.text)
        return {
            "source_url": fetched.url,
            "raw_content": fetched.text,
            "raw_hash": fetched.raw_hash,
            "properties": normalize_properties(properties),
            "note": "html",
        }

    # --- API strategy ---
    if strategy == "api":
        api_cfg = opts.get("api") or {}
        properties = fetch_from_api(api_cfg, source_url)
        return {
            "source_url": api_cfg.get("url") or source_url,
            "raw_content": None,
            "raw_hash": None,
            "properties": normalize_properties(properties),
            "note": "api",
        }

    # --- JS exhaust (Load more): use browser when available, else sitemap+HTML ---
    if strategy == "js_exhaust":
        if _playwright_available():
            load_more = opts.get("load_more") or {}
            fetched = fetch_url_js_exhaust(source_url, load_more)
            properties = extract_properties_from_html(fetched.text)
            return {
                "source_url": fetched.url,
                "raw_content": fetched.text,
                "raw_hash": fetched.raw_hash,
                "properties": normalize_properties(properties),
                "note": "js_exhaust",
            }
        return _fetch_without_browser()

    # --- Plain JS (single paint): use browser when available, else sitemap+HTML ---
    if strategy == "js":
        if _playwright_available():
            fetched = fetch_url_js(source_url)
            properties = extract_properties_from_html(fetched.text)
            return {
                "source_url": fetched.url,
                "raw_content": fetched.text,
                "raw_hash": fetched.raw_hash,
                "properties": normalize_properties(properties),
                "note": "js_rendered",
            }
        return _fetch_without_browser()

    # --- Sitemap first then HTML fallback ---
    if strategy == "sitemap_first":
        sitemap_snapshot = fetch_from_sitemap()
        if sitemap_snapshot:
            return sitemap_snapshot
        fetched = fetch_url(source_url)
        if fetched.status_code != 200:
            raise RuntimeError(
                f"Asset fetch failed: {fetched.url} returned HTTP {fetched.status_code}. "
                "Refusing to parse or persist; check Runs for this error."
            )
        if opts.get("llm_extract"):
            llm_props = _extract_properties_via_llm(fetched.text, source_url)
            if llm_props:
                properties = llm_props
                note = "llm"
            else:
                properties = extract_properties_from_html(fetched.text)
                note = "html"
            return {
                "source_url": fetched.url,
                "raw_content": fetched.text,
                "raw_hash": fetched.raw_hash,
                "properties": normalize_properties(properties),
                "note": note,
            }
        properties = extract_properties_from_html(fetched.text)
        return {
            "source_url": fetched.url,
            "raw_content": fetched.text,
            "raw_hash": fetched.raw_hash,
            "properties": normalize_properties(properties),
            "note": "html",
        }

    # --- Default: HTML first, then sitemap fallback ---
    html_snapshot = fetch_from_html()
    if html_snapshot.get("properties"):
        return html_snapshot
    sitemap_snapshot = fetch_from_sitemap()
    return sitemap_snapshot or html_snapshot


def build_structured_json(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_url": snapshot.get("source_url"),
        "fetched_at": datetime.utcnow().isoformat(),
        "properties": snapshot.get("properties", []),
        "note": snapshot.get("note"),
    }
