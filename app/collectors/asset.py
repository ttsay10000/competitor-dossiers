import json
import re
import gzip
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse, urlencode, urlunparse, parse_qs
from typing import Any, Dict, List, Optional

import requests
from bs4 import BeautifulSoup

from .http import fetch_url, fetch_url_js, fetch_url_js_exhaust

# Max characters of page text to send to LLM for property extraction (fit context, control cost).
# Increased to 50k so large property lists (e.g. AvantStay-style search pages) are not silently
# truncated to just the first handful of properties when using the generic HTML→LLM extractor.
_LLM_EXTRACT_MAX_CHARS = 50_000
# Max blocks per LLM call when extracting from Lark-style blocks (avoids truncation; we chunk and merge).
_LARK_BLOCKS_BATCH_SIZE = 80


def discover_sitemap(url: str) -> list[str]:
    """Build sitemap URLs from origin only (scheme + netloc), not the full path."""
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else url
    if origin.endswith("/"):
        origin = origin.rstrip("/")
    return [f"{origin}/sitemap.xml", f"{origin}/sitemap.xml.gz"]


def extract_links_from_sitemap(xml_text: str) -> list[str]:
    """
    Parse a sitemap XML string and return all <loc> URLs.

    Uses the 'xml' tree builder when available (preferred), and falls back to the
    default HTML parser when an XML-capable parser is not installed so that runs
    do not hard-fail with "Couldn't find a tree builder with the features you
    requested: xml". In the worst case, returns an empty list so callers can
    continue with HTML/JS-based extraction.
    """
    try:
        soup = BeautifulSoup(xml_text, "xml")
    except Exception:
        try:
            soup = BeautifulSoup(xml_text, "html.parser")
        except Exception:
            return []
    urls: list[str] = []
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
    u = (url or "").strip()
    # Exclude blog, privacy, and other non-property paths that can match broad patterns
    if re.search(r"/blog|/blogs|/privacy|/privacy-policy|/legal|/terms\b", u, re.IGNORECASE):
        return False
    # Exclude obvious marketing / nav / legal pages that are not individual assets
    # (e.g. Placemakr: /extended-stays, /corporate-group, /business, /residents, /cookie-notice).
    if "://" in u:
        parsed = urlparse(u)
        path = parsed.path or ""
    else:
        path = u
    path_lower = path.lower().rstrip("/") or "/"
    # Exclude portfolio index (e.g. /portfolio or /portfolio/) — only /portfolio/slug is a property.
    if path_lower in ("/portfolio", "/properties", "/locations", "/property"):
        return False
    if re.search(
        r"/(extended-stays?|corporate-group|corporate-stays?|business|residents|about|contact-us?|cookie-notice|faqs?|help|support)(?:/|\?|$)",
        path_lower,
    ):
        return False
    patterns = [
        r"/properties/",   # /properties/slug not /properties
        r"/property/",     # /property/slug not /property
        r"/portfolio/.+",  # e.g. Lark: .../portfolio/property-slug (excludes /portfolio and /portfolio/)
        r"/locations/",
        r"/apartments/",
        r"/homes/",
        r"/destinations/",
        r"/search",
        r"/listing/",
        r"/stay/",
        r"/vacation-rentals/",
        # Placemakr-style single-segment city-state paths (e.g. /saltlakecity-ut) — require trailing 2-letter state.
        r"/[a-z0-9]+-[a-z]{2}(?:\?|$|/)",
        # AvantStay-style: /{numeric_id}/{destination}/{property-slug} (e.g. /429468/newport-beach/sand-castle)
        r"/[0-9]+/[a-z0-9-]+/[a-z0-9-]+(?:\?|$|/)",
    ]
    return any(re.search(pattern, u, re.IGNORECASE) for pattern in patterns)


def _is_junk_property_link(link_text: str, href: str) -> bool:
    """True if this link is nav/footer/metadata, not a real property (e.g. See all X blogs, Privacy Policy)."""
    text = (link_text or "").strip().lower()
    if not text or len(text) < 3:
        return True
    if text == "privacy policy" or text.startswith("see all ") and "blog" in text:
        return True
    if re.match(r"^(see all|view all)\s", text) and ("blog" in text or "location" in text):
        return True
    # Generic "All properties/locations" navigation links (e.g. Placemakr city-level pages) — not individual assets.
    if text in ("all properties", "all locations"):
        return True
    if text.startswith("all ") and ("properties" in text or "locations" in text):
        return True
    # Site-wide navigation and legal links (common across competitors, including Placemakr).
    nav_labels = {
        "extended stays",
        "extended stay",
        "corporate stays",
        "corporate stay",
        "business",
        "residents",
        "about",
        "about us",
        "contact",
        "contact us",
        "cookie notice",
        "cookie policy",
        "terms & conditions",
        "terms and conditions",
        "terms of use",
        "privacy",
        "privacy policy",
        "faq",
        "faqs",
        "help",
        "support",
    }
    if text in nav_labels:
        return True
    return False


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
        if _is_junk_property_link(text, href):
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


# Data attributes and aria labels that often contain property location on cards (so LLM can see them).
_LOCATION_ATTRS = ("data-city", "data-state", "data-region", "data-market", "data-location", "data-address", "aria-label")

# US state abbreviation -> full name (for parsing "City, ST" from Lark-style blocks).
_US_STATE_ABBREV = {
    "al": "Alabama", "ak": "Alaska", "az": "Arizona", "ar": "Arkansas", "ca": "California",
    "co": "Colorado", "ct": "Connecticut", "de": "Delaware", "fl": "Florida", "ga": "Georgia",
    "hi": "Hawaii", "id": "Idaho", "il": "Illinois", "in": "Indiana", "ia": "Iowa",
    "ks": "Kansas", "ky": "Kentucky", "la": "Louisiana", "me": "Maine", "md": "Maryland",
    "ma": "Massachusetts", "mi": "Michigan", "mn": "Minnesota", "ms": "Mississippi", "mo": "Missouri",
    "mt": "Montana", "ne": "Nebraska", "nv": "Nevada", "nh": "New Hampshire", "nj": "New Jersey",
    "nm": "New Mexico", "ny": "New York", "nc": "North Carolina", "nd": "North Dakota", "oh": "Ohio",
    "ok": "Oklahoma", "or": "Oregon", "pa": "Pennsylvania", "ri": "Rhode Island", "sc": "South Carolina",
    "sd": "South Dakota", "tn": "Tennessee", "tx": "Texas", "ut": "Utah", "vt": "Vermont",
    "va": "Virginia", "wa": "Washington", "dc": "Washington DC", "wv": "West Virginia", "wi": "Wisconsin", "wy": "Wyoming",
}

# Pattern: "City, ST" or "City, State" at start of line (for location line in Lark blocks).
_RE_CITY_ST = re.compile(r"^([^,]+),\s*([a-z]{2}|[A-Za-z\s]+)$", re.IGNORECASE)

# Lark: h2 that is a detail line (Keys, F&B, Brand) not a property name — skip so we don't count as separate property
_RE_LARK_DETAIL_H2 = re.compile(
    r"^(Keys|F&B\s*Outlet|F&B\s*Outlets|Brand)\s*:",
    re.IGNORECASE,
)


def _extract_lark_style_blocks(html: str, base_url: str) -> List[dict[str, Any]]:
    """
    Parse Lark-style portfolio HTML. Two patterns supported:
    1. Property per block: <h2> (property name) followed by <ul> with location ("City, ST") and details.
    2. Location as section header: <h2> is "City, ST"; <ul> contains one or more properties (each <li>
       may have a link to the property). We emit one block per property so names are property names, not location.
    Returns list of {"name", "location_line", "details_list", "url"}.
    """
    soup = BeautifulSoup(html, "html.parser")
    blocks = []
    for h2 in soup.find_all("h2"):
        h2_text = (h2.get_text() or "").strip()
        if not h2_text or len(h2_text) < 2:
            continue
        # Skip h2 that is a detail line (Keys: 42, F&B Outlet: 1, Brand: X) — not a property name
        if _RE_LARK_DETAIL_H2.match(h2_text):
            continue
        ul = h2.find_next_sibling("ul")
        if not ul:
            next_el = h2.find_next_sibling()
            if next_el:
                ul = next_el.find("ul") if next_el.name != "ul" else next_el
        if not ul:
            continue

        # Case: h2 is a location header ("City, ST") — page groups properties by location.
        # Extract one block per property link (or per li) so "name" is the property name, not the location.
        if _RE_CITY_ST.match(h2_text) and len(h2_text) < 50:
            location_line = h2_text
            for li in ul.find_all("li", recursive=False):
                prop_links = []
                for a in (li.find_all("a", href=True) or []):
                    href = (a.get("href") or "").strip()
                    if href and not href.startswith("#") and ("portfolio" in href or "property" in href or is_property_like(href)):
                        prop_links.append((a, href))
                li_text = (li.get_text() or "").strip()
                if "Visit Website" in li_text or "website" in li_text.lower():
                    continue
                all_li_lines = [t.strip() for t in li_text.split("\n") if t.strip()]
                if prop_links:
                    for a, href in prop_links:
                        name = (a.get_text() or "").strip()
                        if not name or len(name) < 2:
                            name = next((ln for ln in all_li_lines if not _RE_CITY_ST.match(ln) and not ln.lower().startswith("keys:") and not ln.lower().startswith("brand:")), all_li_lines[0] if all_li_lines else "Unnamed")
                        url_val = urljoin(base_url, href) if not href.startswith("http") else href
                        # Details: all li lines except the location line and the name we used
                        details_list = [ln for ln in all_li_lines if ln != h2_text and ln != name and not (len(ln) < 50 and _RE_CITY_ST.match(ln))]
                        blocks.append({
                            "name": name,
                            "location_line": location_line,
                            "details_list": details_list,
                            "url": url_val,
                        })
                elif li_text:
                    first_line = li_text.split("\n")[0].strip() if "\n" in li_text else li_text
                    if _RE_CITY_ST.match(first_line):
                        continue
                    if _RE_LARK_DETAIL_H2.match(first_line):
                        continue
                    details_list = [ln for ln in all_li_lines if ln != first_line and not (len(ln) < 50 and _RE_CITY_ST.match(ln))]
                    blocks.append({
                        "name": first_line[:100],
                        "location_line": location_line,
                        "details_list": details_list,
                        "url": None,
                    })
            continue

        # Case: h2 is the property name; ul has location line + details
        name = h2_text
        parent = h2.parent
        url_val = None
        if parent:
            a = parent.find("a", href=True) if parent.name == "a" else h2.find_previous("a", href=True)
            if not a and parent:
                a = parent.find("a", href=True)
            if a and a.get("href"):
                href = (a.get("href") or "").strip()
                if href and not href.startswith("#") and ("portfolio" in href or "property" in href or is_property_like(href)):
                    url_val = urljoin(base_url, href) if not href.startswith("http") else href
        li_texts = []
        for li in ul.find_all("li", recursive=False):
            t = (li.get_text() or "").strip()
            if t and "Visit Website" not in t and "website" not in t.lower():
                li_texts.append(t)
        if not li_texts:
            blocks.append({"name": name, "location_line": None, "details_list": [], "url": url_val})
            continue
        location_line = None
        details_list = []
        for t in li_texts:
            if _RE_CITY_ST.match(t) and len(t) < 50 and not t.lower().startswith("keys:") and not t.lower().startswith("brand:"):
                if location_line is None:
                    location_line = t
                    continue
            details_list.append(t)
        blocks.append({
            "name": name,
            "location_line": location_line,
            "details_list": details_list,
            "url": url_val,
        })
    return blocks


def _parse_city_st(location_line: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Parse 'Cambridge, MA' -> (city='Cambridge', state='Massachusetts'). Returns (city, state)."""
    if not (location_line or "").strip():
        return None, None
    m = _RE_CITY_ST.match((location_line or "").strip())
    if not m:
        return None, None
    city = (m.group(1) or "").strip()
    state_part = (m.group(2) or "").strip()
    if len(state_part) == 2:
        state = _US_STATE_ABBREV.get(state_part.lower())
    else:
        state = state_part if state_part else None
    return city or None, state or None


def _lark_blocks_to_text(blocks: List[dict[str, Any]], max_chars: int = _LLM_EXTRACT_MAX_CHARS) -> str:
    """Convert Lark-style blocks to clean text for the LLM (structure-preserving)."""
    lines = []
    for b in blocks:
        lines.append(f"Property: {b.get('name') or ''}")
        if b.get("location_line"):
            lines.append(f"Location: {b['location_line']}")
        if b.get("details_list"):
            lines.append("Details: " + "; ".join(b["details_list"]))
        if b.get("url"):
            lines.append(f"URL: {b['url']}")
        lines.append("---")
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n[... truncated]"
    return text


def _html_to_text_for_llm(html: str, max_chars: int = _LLM_EXTRACT_MAX_CHARS) -> str:
    """Reduce HTML to plain text for LLM (strip scripts, get body text, include location data-*, truncate)."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(["script", "style", "noscript"]):
        tag.decompose()
    body = soup.find("body") or soup
    # Collect location metadata from data-* and aria-label so card locations are visible to the LLM
    location_lines = []
    for el in body.find_all(True) if body else []:
        parts = []
        for attr in _LOCATION_ATTRS:
            val = el.get(attr)
            if val and isinstance(val, str) and (val := val.strip()) and len(val) < 200:
                parts.append(f"{attr}={val}")
        if parts:
            location_lines.append(" ".join(parts))
    text = body.get_text(separator="\n", strip=True)
    if location_lines:
        text = text + "\n\n[Location metadata from page]\n" + "\n".join(location_lines[:500])
    text = re.sub(r"\n{3,}", "\n\n", text)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n[... truncated]"
    return text


def _extract_properties_via_llm_from_blocks(
    blocks: List[dict[str, Any]],
    page_url: str,
) -> List[dict[str, Any]]:
    """
    Given Lark-style blocks (name, location_line, details_list, url), use LLM to return
    state, city, and a short details string for parentheses. Processes blocks in batches
    to avoid input truncation (14k char limit) and output token limits; no cap on total properties.
    Falls back to parsing location_line in code when LLM is unavailable.

    LLM iterations: blocks are batched (_LARK_BLOCKS_BATCH_SIZE); for each batch we send
    _lark_blocks_to_text(batch) and get back a JSON array of {index, state, city, details}.
    Details are a single line (e.g. "67 keys, 2 F&B outlets") for downstream key parsing.
    Final dossier display uses only a summary per location (State - N properties (M keys)), not per-property subbullets.
    """
    try:
        from ..config import get_openai_client
        client = get_openai_client()
        if not client:
            return _lark_blocks_to_properties_without_llm(blocks)
    except Exception:
        return _lark_blocks_to_properties_without_llm(blocks)

    parsed = urlparse(page_url)
    base_url = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else page_url

    system = (
        "You are given property blocks from a hotel/portfolio page. Each block has Property name, Location (e.g. City, ST), and Details (Keys, F&B Outlets, Brand, etc.). "
        "Locations will be summarized by state only. Return a JSON array with one object per block, in the same order. Each object must have: "
        '"index" (integer, 0-based), "state" (full US state name only, e.g. "Massachusetts"—no city in state), "city" (optional, e.g. "Cambridge", omit if unknown), '
        '"details" (string for display; MUST include the key count when the block shows Keys, e.g. "67 keys, 2 F&B outlets, Brand: Lark Hotels" or "Keys: 67, 2 F&B outlets"—the number before \"keys\" is required for totals). '
        "Use only information from the blocks. Use standard US state names. For anything that does not neatly fit in a specific US state (missing location, career site, non-property link, unclear) use state \"Other\" and omit city. "
        "Return only the JSON array, no markdown."
    )

    out: List[dict[str, Any]] = []
    for start in range(0, len(blocks), _LARK_BLOCKS_BATCH_SIZE):
        batch = blocks[start : start + _LARK_BLOCKS_BATCH_SIZE]
        text = _lark_blocks_to_text(batch, max_chars=50_000)
        user = f"Extract state, city, and details for each property (indices {start} to {start + len(batch) - 1}):\n\n{text}"

        try:
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                max_tokens=8000,
                temperature=0.1,
            )
            content = (resp.choices[0].message.content or "").strip()
            if content.startswith("```"):
                content = re.sub(r"^```\w*\n?", "", content).rstrip("`\n")
            raw = json.loads(content)
            if not isinstance(raw, list):
                out.extend(_lark_blocks_to_properties_without_llm(batch))
                continue
            by_index = {int(item["index"]): item for item in raw if isinstance(item, dict) and "index" in item}
            # If LLM returned fewer items than blocks, we still output one per block (use block data for missing indices).
            if len(by_index) < len(batch):
                import logging
                logging.getLogger(__name__).warning(
                    "Lark LLM extraction returned %d items for %d blocks (indices %d–%d); using block fallback for missing.",
                    len(by_index), len(batch), start, start + len(batch) - 1,
                )
            for i, b in enumerate(batch):
                name = (b.get("name") or "").strip()
                if not name:
                    continue
                url_val = b.get("url")
                if url_val and not str(url_val).startswith("http"):
                    url_val = urljoin(base_url, url_val)
                city, state = _parse_city_st(b.get("location_line"))
                details_val = None
                if i in by_index:
                    state = (by_index[i].get("state") or "").strip() or state or "Other"
                    if by_index[i].get("city"):
                        city = (by_index[i].get("city") or "").strip() or city
                    details_val = (by_index[i].get("details") or "").strip() or None
                if not details_val and b.get("details_list"):
                    details_val = "; ".join(b["details_list"])
                out.append({
                    "name": name,
                    "url": url_val or None,
                    "market": b.get("location_line"),
                    "state": state or "Other",
                    "city": city,
                    "status": None,
                    "details": details_val,
                })
        except Exception:
            out.extend(_lark_blocks_to_properties_without_llm(batch))

    return out if out else _lark_blocks_to_properties_without_llm(blocks)


def _lark_blocks_to_properties_without_llm(blocks: List[dict[str, Any]]) -> List[dict[str, Any]]:
    """Convert Lark blocks to property dicts using only code (parse City, ST; details = joined list)."""
    out = []
    for b in blocks:
        name = (b.get("name") or "").strip()
        if not name:
            continue
        city, state = _parse_city_st(b.get("location_line"))
        details_val = "; ".join(b.get("details_list") or []) if b.get("details_list") else None
        out.append({
            "name": name,
            "url": b.get("url"),
            "market": b.get("location_line"),
            "state": state or "Other",
            "city": city,
            "status": None,
            "details": details_val or None,
        })
    return out


def _extract_properties_via_llm(html: str, page_url: str) -> List[dict[str, Any]]:
    """
    Use OpenAI to extract property names (and URLs when present) from portfolio-style page content.
    Returns list of dicts with "name" and optionally "url" (absolute). Requires OPENAI_API_KEY.
    """
    try:
        from ..config import get_openai_client
        client = get_openai_client()
        if not client:
            return []
    except Exception:
        return []

    text = _html_to_text_for_llm(html)
    parsed = urlparse(page_url)
    base_url = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else page_url

    system = (
        "You are extracting a list of properties (hotels, apartments, vacation rentals, etc.) from web page text. "
        "Return a JSON array of objects. Each object must have: \"name\" (string, the property/location name). "
        "If the page provides a URL or path to that property, include \"url\" (string). "
        "If the page shows a city, state, or market for a property (e.g. in the card, address, or subheading), include \"market\" (string, e.g. \"Austin, TX\"), "
        "\"state\" (full US state name, e.g. \"Texas\"), and \"city\" (e.g. \"Austin\") when evident from the content. "
        "Use only the exact names, URLs, and locations from the content. Skip navigation, footers, and non-property items. "
        "Output only the JSON array, no markdown or explanation."
    )
    user = f"Page base URL: {base_url}\n\nExtract all properties from this page text:\n\n{text}"

    try:
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
            market_val = (item.get("market") or "").strip() or None
            state_val = (item.get("state") or "").strip() or None
            city_val = (item.get("city") or "").strip() or None
            out.append({
                "name": name,
                "url": url_val or None,
                "market": market_val,
                "state": state_val,
                "city": city_val,
                "status": None,
            })
        return out
    except Exception:
        return []


def _merge_link_properties_into(block_properties: List[dict[str, Any]], html: str, threshold: int = 25) -> List[dict[str, Any]]:
    """
    When we have few properties from Lark-style blocks (e.g. partial HTML or different page structure),
    merge in any additional properties found via link extraction so we don't cap at ~15.
    Preserves block-derived props; adds link-based ones whose URL is not already present.
    """
    if len(block_properties) > threshold or not html:
        return block_properties
    link_props = extract_properties_from_html(html)
    if not link_props:
        return block_properties

    def _url_key(url: Optional[str]) -> str:
        """Normalize URL to a path-only key for deduping (handles relative vs absolute)."""
        if not url:
            return ""
        u = (url or "").strip()
        # Drop scheme/host so '/locations/x' and 'https://site/locations/x' collapse.
        if "://" in u:
            parsed = urlparse(u.split("?", 1)[0])
            path = parsed.path or "/"
        else:
            path = u.split("?", 1)[0]
        key = (path.rstrip("/") or "/").lower()
        return key
    seen = {_url_key(p.get("url")) for p in block_properties if _url_key(p.get("url"))}
    merged = list(block_properties)
    for p in link_props:
        key = _url_key(p.get("url"))
        if key and key not in seen:
            seen.add(key)
            merged.append({
                "name": (p.get("name") or "").strip(),
                "url": p.get("url"),
                "market": p.get("market"),
                "state": None,
                "city": None,
                "status": p.get("status"),
                "details": None,
            })
    return merged


def _is_detail_line_or_junk_name(name: str) -> bool:
    """True if this looks like a metadata/detail line (Keys:, F&B:, Brand:) or nav text, not a property name."""
    n = (name or "").strip()
    if not n or len(n) < 2:
        return True
    if _RE_LARK_DETAIL_H2.match(n):
        return True
    lower = n.lower()
    if lower == "privacy policy":
        return True
    if re.match(r"^see all .+ blog", lower, re.IGNORECASE):
        return True
    # Common CTA / nav labels that should never become property names.
    if lower in {
        "book a hotel stay",
        "rent an apartment",
        "stay nightly",
        "stay longer",
        "view details",
        "view property",
        "view all properties",
    }:
        return True
    if lower.startswith(("book ", "view ", "stay ", "reserve ")):
        return True
    return False


def normalize_properties(properties: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Normalize property dicts and deduplicate by URL (and by name/market when URL is missing).

    This keeps counts aligned with the real number of distinct properties on a page
    (e.g. Placemakr's locations, which have multiple links like "All Properties", CTAs, etc.).
    """
    normalized: list[dict[str, Any]] = []
    seen_url_keys: set[str] = set()
    seen_name_market: set[tuple[str, str]] = set()

    def _url_key(url: Optional[str]) -> str:
        """Normalize URL to a path-only key for deduping (handles relative vs absolute)."""
        if not url:
            return ""
        u = (url or "").strip()
        # Drop scheme/host so '/locations/x' and 'https://site/locations/x' collapse.
        if "://" in u:
            parsed = urlparse(u.split("?", 1)[0])
            path = parsed.path or "/"
        else:
            path = u.split("?", 1)[0]
        key = (path.rstrip("/") or "/").lower()
        return key

    for prop in properties:
        name = (prop.get("name") or "").strip()
        if _is_detail_line_or_junk_name(name):
            continue

        raw_url = prop.get("url")
        market_val = (prop.get("market") or "").strip() or None
        key = _url_key(raw_url)
        if key:
            if key in seen_url_keys:
                continue
            seen_url_keys.add(key)
        else:
            nm = (name, market_val or "")
            if nm in seen_name_market:
                continue
            seen_name_market.add(nm)

        normalized.append(
            {
                "url": raw_url,
                "name": name,
                "market": market_val,
                "state": (prop.get("state") or "").strip() or None,
                "city": (prop.get("city") or "").strip() or None,
                "status": (prop.get("status") or "").strip() or None,
                "details": (prop.get("details") or "").strip() or None,
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


# Default minimum properties to "accept" a strategy result when using strategy_chain.
# If a strategy returns fewer than this, we try the next in the chain (avoids "succeeding" with wrong process).
_ASSET_CHAIN_MIN_PROPERTIES = 5

# When a source has no strategy_chain and no explicit strategy, we use this chain so new competitors
# (name + URL only) are tried with all three methods; we only accept when one returns >= min_properties_accept.
# Order: sitemap first (works for many property/vacation-rental sites), then JS exhaust (Lark-style), then HTML.
_DEFAULT_ASSET_STRATEGY_CHAIN = ["sitemap_first", "js_exhaust", "html"]


def collect_asset_snapshot(
    source_url: str,
    js_required: bool = False,
    use_sitemap_first: bool = False,
    extra_options: Optional[Dict[str, Any]] = None,
) -> dict[str, Any]:
    opts = extra_options or {}

    def _infer_strategy() -> str:
        s = opts.get("strategy")
        if s:
            return s
        if js_required and opts.get("load_more"):
            return "js_exhaust"
        if use_sitemap_first:
            return "sitemap_first"
        if js_required:
            return "js"
        return "html"

    def fetch_from_sitemap() -> Optional[Dict[str, Any]]:
        """
        Crawl sitemap.xml (and any nested sitemap indexes) for property-like URLs.

        Many competitors (including AvantStay) expose properties only via secondary
        sitemap files (e.g. /sitemap-0.xml). The previous implementation only
        inspected the top-level sitemap and could therefore return a heavily
        truncated property list. Here we walk nested sitemap URLs on the same
        origin and aggregate all links that look like property/location pages.
        """

        for root_sitemap_url in discover_sitemap(source_url):
            visited: set[str] = set()
            property_urls: set[str] = set()
            stack: list[str] = [root_sitemap_url]

            while stack:
                sitemap_url = stack.pop()
                if sitemap_url in visited:
                    continue
                visited.add(sitemap_url)

                urls = expand_sitemap(sitemap_url)
                if not urls:
                    continue

                for url in urls:
                    if not url:
                        continue
                    # If this looks like a property/location URL, keep it.
                    if is_property_like(url):
                        property_urls.add(url)
                        continue

                    # Otherwise, if it's another sitemap (same origin), enqueue it so we
                    # can pull property URLs from section-specific sitemaps as well.
                    if url.endswith(".xml") or url.endswith(".xml.gz"):
                        parsed_child = urlparse(url)
                        parsed_root = urlparse(root_sitemap_url)
                        if parsed_child.netloc and parsed_child.netloc == parsed_root.netloc and url not in visited:
                            stack.append(url)

            # Only return sitemap result when we found property-like URLs so caller can fall back to HTML.
            if property_urls:
                return {
                    "source_url": root_sitemap_url,
                    "raw_content": None,
                    "raw_hash": None,
                    "properties": normalize_properties(
                        [{"url": url, "name": url} for url in sorted(property_urls)]
                    ),
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
        When llm_extract is True, tries Lark-style block extraction first so we get the initial
        batch (~6 properties) from the first HTML; then falls back to generic LLM or link extraction."""
        sitemap_snapshot = fetch_from_sitemap()
        if sitemap_snapshot and sitemap_snapshot.get("properties"):
            return {**sitemap_snapshot, "note": "sitemap_first"}
        fetched = fetch_url(source_url)
        if fetched.status_code != 200:
            raise RuntimeError(
                f"Asset fetch failed: {fetched.url} returned HTTP {fetched.status_code}. "
                "Refusing to parse or persist; check Runs for this error."
            )
        parsed = urlparse(source_url)
        base_url = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else source_url

        # Try Lark-style h2/ul blocks first (works on initial HTML without Load more; gives ~6 properties).
        lark_blocks = _extract_lark_style_blocks(fetched.text, base_url)
        if lark_blocks and opts.get("llm_extract"):
            properties = _extract_properties_via_llm_from_blocks(lark_blocks, source_url)
            properties = _merge_link_properties_into(properties, fetched.text, threshold=999)
            normalized = normalize_properties(properties)
            if not normalized and properties:
                normalized = normalize_properties(_lark_blocks_to_properties_without_llm(lark_blocks))
            if normalized:
                return {
                    "source_url": fetched.url,
                    "raw_content": fetched.text,
                    "raw_hash": fetched.raw_hash,
                    "properties": normalized,
                    "note": "html_lark_blocks",
                }
            # else fall through to generic LLM/link path
        elif lark_blocks and not opts.get("llm_extract"):
            properties = _lark_blocks_to_properties_without_llm(lark_blocks)
            properties = _merge_link_properties_into(properties, fetched.text, threshold=999)
            normalized = normalize_properties(properties)
            if normalized:
                return {
                    "source_url": fetched.url,
                    "raw_content": fetched.text,
                    "raw_hash": fetched.raw_hash,
                    "properties": normalized,
                    "note": "html_lark_blocks",
                }

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

    def run_one_strategy(strategy: str) -> dict[str, Any]:
        """Run a single strategy by name; returns snapshot dict. Used for both chain and single-strategy."""
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
        if strategy == "js_exhaust":
            if _playwright_available():
                load_more = opts.get("load_more") or {}
                fetched = fetch_url_js_exhaust(source_url, load_more)
                if opts.get("llm_extract"):
                    parsed = urlparse(source_url)
                    base_url = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else source_url
                    lark_blocks = _extract_lark_style_blocks(fetched.text, base_url)
                    if lark_blocks:
                        properties = _extract_properties_via_llm_from_blocks(lark_blocks, source_url)
                        properties = _merge_link_properties_into(properties, fetched.text)
                        normalized = normalize_properties(properties)
                        # If normalization dropped everything (e.g. name/key mismatch), keep block-based list
                        if not normalized and properties:
                            normalized = normalize_properties(_lark_blocks_to_properties_without_llm(lark_blocks))
                        return {
                            "source_url": fetched.url,
                            "raw_content": fetched.text,
                            "raw_hash": fetched.raw_hash,
                            "properties": normalized,
                            "note": "js_exhaust_lark_blocks",
                        }
                    # No Lark-style h2/ul blocks (e.g. DOM changed or partial HTML): still use full HTML
                    # and merge link-based properties so we don't undercount (e.g. ~69 from links).
                    properties = _extract_properties_via_llm(fetched.text, source_url)
                    if not properties:
                        properties = extract_properties_from_html(fetched.text)
                    properties = _merge_link_properties_into(properties or [], fetched.text, threshold=999)
                    return {
                        "source_url": fetched.url,
                        "raw_content": fetched.text,
                        "raw_hash": fetched.raw_hash,
                        "properties": normalize_properties(properties),
                        "note": "js_exhaust" if not lark_blocks else "js_exhaust_lark_blocks",
                    }
                properties = extract_properties_from_html(fetched.text)
                return {
                    "source_url": fetched.url,
                    "raw_content": fetched.text,
                    "raw_hash": fetched.raw_hash,
                    "properties": normalize_properties(properties),
                    "note": "js_exhaust",
                }
            return _fetch_without_browser()
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
                    return {
                        "source_url": fetched.url,
                        "raw_content": fetched.text,
                        "raw_hash": fetched.raw_hash,
                        "properties": normalize_properties(llm_props),
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
            properties = extract_properties_from_html(fetched.text)
            return {
                "source_url": fetched.url,
                "raw_content": fetched.text,
                "raw_hash": fetched.raw_hash,
                "properties": normalize_properties(properties),
                "note": "html",
            }
        # "html" or default: HTML first, then sitemap fallback
        html_snapshot = fetch_from_html()
        if html_snapshot.get("properties"):
            return html_snapshot
        sitemap_snapshot = fetch_from_sitemap()
        return sitemap_snapshot or html_snapshot

    # --- Strategy chain: try strategies in order until one returns >= min_properties ---
    # Use explicit chain, or default chain for unknown sources (no strategy_chain and no explicit strategy).
    # That way new competitors (name + URL only) get sitemap → js_exhaust → html and never "succeed" with 0–4 from HTML.
    # When a source has an explicit single strategy (e.g. Lark "js_exhaust" + load_more + llm_extract), use that only—
    # do not use strategy_chain so we get full block extraction and ~69 properties, not chain fallback with fewer.
    chain = opts.get("strategy_chain")
    if opts.get("strategy") is not None:
        chain = None  # Single strategy takes precedence (Lark, etc.)
    if opts.get("strategy") is None:
        if chain is None or (isinstance(chain, list) and len(chain) == 0):
            chain = _DEFAULT_ASSET_STRATEGY_CHAIN
    min_accept = opts.get("min_properties_accept", _ASSET_CHAIN_MIN_PROPERTIES)
    if chain:
        last_snapshot: Optional[Dict[str, Any]] = None
        for strategy_name in chain:
            try:
                snapshot = run_one_strategy(strategy_name)
                if not snapshot:
                    continue
                n = len(snapshot.get("properties") or [])
                if n >= min_accept:
                    note = snapshot.get("note") or strategy_name
                    return {**snapshot, "note": f"{note}_chain_ok"}
                if snapshot:
                    last_snapshot = snapshot
            except Exception:
                continue
        if last_snapshot:
            return last_snapshot
        raise RuntimeError(
            f"Asset strategy_chain exhausted with no result: tried {chain!r} for {source_url}"
        )

    # --- Single strategy (explicit strategy in opts, no chain) ---
    strategy = _infer_strategy()
    return run_one_strategy(strategy)


def build_structured_json(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_url": snapshot.get("source_url"),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "properties": snapshot.get("properties", []),
        "note": snapshot.get("note"),
    }
