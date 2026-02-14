import json
import re
import gzip
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse, urlencode, urlunparse, parse_qs
from typing import Any, Callable, Dict, List, Optional

import requests
from bs4 import BeautifulSoup

from .http import (
    USER_AGENT_BROWSER,
    fetch_url,
    fetch_url_js,
    fetch_url_js_exhaust,
    fetch_url_js_wait_for_spa,
)

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


def _same_site_netloc(netloc1: str, netloc2: str) -> bool:
    """True if same host or same site (e.g. vacasa.com and vacasa.ca for sitemap following)."""
    if not netloc1 or not netloc2:
        return False
    if netloc1.lower() == netloc2.lower():
        return True
    # Same "site" brand: www.vacasa.com and www.vacasa.ca -> both "vacasa"
    def site_key(n: str) -> str:
        n = (n or "").lower().strip()
        if n.startswith("www."):
            n = n[4:]
        parts = n.split(".")
        return parts[0] if parts else ""
    return site_key(netloc1) == site_key(netloc2)


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


# AKA (stayaka.com): property URLs are single-segment slugs like /hotel-aka-backbay or /aka-central-park.
# Sitemap and HTML often list location/city pages (e.g. /aka-new-york-city) where "location has properties listed below";
# only count actual property pages, not these location index pages.
_RE_AKA_PROPERTY_PATH = re.compile(
    r"^/(?:hotel-aka-|aka-)([a-z0-9-]+)(?:\?|/$|$)",
    re.IGNORECASE,
)

# AKA path slugs that are location/city index pages (one location with properties listed below), not individual properties.
_AKA_LOCATION_SLUGS = frozenset({
    "new-york", "new-york-city", "nyc", "brooklyn", "manhattan", "queens", "long-island",
    "washington-dc", "washington", "dc", "chicago", "boston", "philadelphia", "philly",
    "san-francisco", "sf", "bay-area", "los-angeles", "la", "orange-county", "san-diego",
    "miami", "miami-beach", "atlanta", "dallas", "houston", "austin", "denver", "seattle",
    "seattle-belltown", "seattle-downtown", "minneapolis", "detroit", "baltimore",
    "united-kingdom", "london", "uk", "canada", "toronto", "vancouver", "montreal",
})

# AKA slug substrings that indicate content/offer/guide pages, not actual residence properties.
_AKA_NON_PROPERTY_SUBSTRINGS = frozenset({
    "-insurance", "-offer", "-grocery", "-insider-offer", "-face-masks", "-espanol",
    "-indonesia", "-middle-east", "-student-housing", "-portuguese", "-wedding",
    "-welcome-guide", "-wifi-welcome", "-museums", "-film-festival", "-business",
    "insider-offer", "international-grocery", "off-campus", "welcome-guide",
    "face-masks", "portuguese",  # standalone content pages (e.g. /aka-face-masks, /aka-portuguese)
})


def _is_aka_source(url: str) -> bool:
    """True if URL's host suggests AKA (stayaka.com) so we apply stricter sitemap filtering."""
    if not url:
        return False
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    return "stayaka" in host or host.endswith("aka.com")


def _is_vacasa_source(url: str) -> bool:
    """True if URL's host is vacasa.com or vacasa.ca."""
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    return "vacasa.com" in host or "vacasa.ca" in host


def _is_vacasa_property_url(candidate_url: str, source_url: str) -> bool:
    """
    For Vacasa, only accept /unit/<numeric> URLs (real listings).
    Sitemap and HTML can include /about-us, /accessibility, etc.; we exclude those.
    """
    if not _is_vacasa_source(source_url):
        return True  # Not Vacasa: no extra filter
    parsed = urlparse(candidate_url)
    path = (parsed.path or "").rstrip("/") or "/"
    return bool(re.search(r"/unit/[0-9]+", path))


def _is_aka_property_url(candidate_url: str, source_url: str) -> bool:
    """
    True if candidate_url is from the same host as source_url and looks like an AKA property page.
    Used when source is AKA to avoid counting location/city index pages (e.g. /aka-new-york-city)
    where the page is "location with properties listed below" rather than an individual property.
    """
    if not _is_aka_source(source_url):
        return True  # Not AKA source: no extra filter
    parsed_c = urlparse(candidate_url)
    parsed_s = urlparse(source_url)
    if (parsed_c.netloc or "").lower() != (parsed_s.netloc or "").lower():
        return False
    path = (parsed_c.path or "").rstrip("/") or "/"
    match = _RE_AKA_PROPERTY_PATH.match(path)
    if not match:
        return False
    slug = (match.group(1) or "").strip().lower()
    # Exclude known location/city index slugs so we don't count "location with properties below" as properties.
    if slug in _AKA_LOCATION_SLUGS:
        return False
    # Also exclude slugs that look like city names (e.g. *-city, *-dc, *-downtown) to reduce false positives.
    if slug.endswith("-city") or slug.endswith("-dc") or slug.endswith("-downtown") or slug.endswith("-metro"):
        return False
    # Exclude content/offer/guide pages (insider offers, welcome guides, language pages, etc.).
    for sub in _AKA_NON_PROPERTY_SUBSTRINGS:
        if sub in slug:
            return False
    # Standalone "aka-insider" is a program page, not a property.
    if slug == "insider":
        return False
    return True


def _is_placemakr_source(url: str) -> bool:
    """True if URL's host is placemakr.com (so we apply stricter link filtering)."""
    if not url:
        return False
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    return "placemakr.com" in host


def _is_placemakr_property_url(candidate_url: str, source_url: str) -> bool:
    """
    For Placemakr, only accept URLs that are real location/property pages. The locations page
    links to (1) city-state pages like /saltlakecity-ut, /washington-dc, and (2) individual
    properties like /locations/washington-dc/dupont-circle. Exclude nav/marketing links
    that match generic patterns (e.g. /residential, /partners, /investments) which would
    otherwise end up as "Other" after state inference.
    """
    if not _is_placemakr_source(source_url):
        return True  # Not Placemakr: no extra filter
    parsed = urlparse(candidate_url)
    path = (parsed.path or "").rstrip("/") or "/"
    parts = [p for p in path.split("/") if p]
    # (1) Single-segment city-state: /{slug}-{XX} where XX is valid US state abbrev
    if len(parts) == 1:
        seg = parts[0].lower()
        if "-" in seg:
            suffix = seg.split("-")[-1]
            if len(suffix) == 2 and suffix in _US_STATE_ABBREV:
                return True
        return False
    # (2) /locations/{city}/{property} — at least two segments after /locations/
    if len(parts) >= 3 and parts[0].lower() == "locations":
        return True
    return False


def _is_rove_source(url: str) -> bool:
    """True if URL's host is rovetravel.com (so we apply stricter link filtering)."""
    if not url:
        return False
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    return "rovetravel.com" in host


def _is_rove_search_url(url: str) -> bool:
    """True if URL is Rove's search page (need js_exhaust first so we scroll to get full list, not ~10 from sitemap)."""
    if not _is_rove_source(url):
        return False
    path = (urlparse(url).path or "").rstrip("/") or "/"
    return path.lower() == "/search"


def _is_rove_property_url(candidate_url: str, source_url: str) -> bool:
    """
    For Rove (rovetravel.com), only accept /listing/<slug> URLs as individual properties.
    Exclude /search, /search?market=..., /collections/..., /locations, /list-on-rove, etc.,
    which otherwise match generic is_property_like patterns and get trapped as extra properties.
    Use _same_site_netloc so www.rovetravel.com matches rovetravel.com (sitemap uses www).
    """
    if not _is_rove_source(source_url):
        return True  # Not Rove: no extra filter
    parsed_c = urlparse(candidate_url)
    parsed_s = urlparse(source_url)
    if not _same_site_netloc(parsed_c.netloc or "", parsed_s.netloc or ""):
        return False
    path = (parsed_c.path or "").rstrip("/") or "/"
    path_lower = path.lower()
    # Only actual listing pages: /listing/<property-slug>
    if path_lower.startswith("/listing/") and len(path) > len("/listing/"):
        return True
    return False


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
    # Exclude single-segment "property-*" nav/marketing pages (e.g. Vacasa /property-management).
    # We want /property/slug (listing), not /property-management.
    if path_lower.startswith("/property-"):
        return False
    # Exclude bare search/listing index (e.g. AvantStay https://avantstay.com/search) — not a property.
    if path_lower in ("/search", "/search/"):
        return False
    if re.search(
        r"/(extended-stays?|corporate-group|corporate-stays?|business|residents|about|contact-us?|cookie-notice|faqs?|help|support)(?:/|\?|$)",
        path_lower,
    ):
        return False
    patterns = [
        r"/properties/",   # /properties/slug not /properties
        r"/property/",     # /property/slug not /property
        r"/unit/[0-9]+",   # e.g. Vacasa: .../unit/12345 (numeric ID only; exclude /unit/faq, etc.)
        r"/portfolio/.+",  # e.g. Lark: .../portfolio/property-slug (excludes /portfolio and /portfolio/)
        r"/locations/",
        r"/apartments/",
        r"/homes/",
        r"/destinations/",
        r"/listing/",
        r"/stay/",
        r"/vacation-rentals/",
        # Placemakr-style single-segment city-state paths (e.g. /saltlakecity-ut) — require trailing 2-letter state.
        r"/[a-z0-9]+-[a-z]{2}(?:\?|$|/)",
        # AvantStay-style: /{numeric_id}/{destination}/{property-slug} (e.g. /429468/newport-beach/sand-castle)
        r"/[0-9]+/[a-z0-9-]+/[a-z0-9-]+(?:\?|$|/)",
        # AKA-style (stayaka.com): single-segment property slug (e.g. /hotel-aka-backbay, /aka-central-park)
        # Match path with one segment only (? or end or single trailing /) so we don't match /locations/foo.
        r"/[a-z0-9][a-z0-9-]{9,}(?:\?|/$|$)",
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
    # Landing: "View all homes in Atlanta, GA" etc. are nav cards, not properties
    if "view all homes" in text:
        return True
    # Generic "All properties/locations" navigation links (e.g. Placemakr city-level pages) — not individual assets.
    if text in ("all properties", "all locations"):
        return True
    if text.startswith("all ") and ("properties" in text or "locations" in text):
        return True
    # Kasa: "Go to location City, ST" is the city-level link, not an individual property (properties are "View details Apartment/Hotel Name" under each city).
    if text.strip().lower().startswith("go to location "):
        return True
    # Site-wide navigation and legal links (common across competitors, including Kasa, Placemakr).
    nav_labels = {
        "extended stays",
        "extended stay",
        "corporate stays",
        "corporate stay",
        "corporate housing",
        "corporate partnerships",
        "multifamily partners",
        "hotel partners",
        "the kasa experience",
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
        "leasing",
        "groups",
    }
    if text in nav_labels:
        return True
    return False


def _link_in_empty_landing_section(link) -> bool:
    """True if link is inside a container that has 'No properties available' (Landing: skip empty locations)."""
    parent = link.parent
    for _ in range(20):
        if parent is None or parent.name in ("body", "html"):
            break
        text = (parent.get_text() or "").strip()
        if "no properties available" in text.lower():
            return True
        parent = getattr(parent, "parent", None)
    return False


def _link_in_footer(link) -> bool:
    """True if link is inside a footer (e.g. site footer with many location links — exclude from property count)."""
    parent = link.parent
    for _ in range(25):
        if parent is None or parent.name in ("body", "html"):
            break
        name = getattr(parent, "name", None) or ""
        if name == "footer":
            return True
        role = (parent.get("role") or "").strip().lower()
        if role == "contentinfo":
            return True
        cls = (parent.get("class") or [])
        if isinstance(cls, str):
            cls = [cls]
        cls_str = " ".join(c.lower() for c in cls if isinstance(c, str))
        if "footer" in cls_str or "site-footer" in cls_str or "page-footer" in cls_str:
            return True
        parent = getattr(parent, "parent", None)
    return False


def _is_landing_image_collector(text: str) -> bool:
    """True if text is the per-section nav card 'View all homes in City, ST' — not a property."""
    t = (text or "").strip().lower()
    return bool(t and "view all homes" in t)


def _is_landing_locations_url(url: str) -> bool:
    """True if URL is Landing's locations page (hellolanding.com/locations). Used to force landing_locations strategy."""
    if not url:
        return False
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    path = (parsed.path or "").rstrip("/") or "/"
    return "hellolanding.com" in host and path == "/locations"


# Pattern for Landing location headers: "City, ST" or "Washington D.C." (section headers, not property names).
_RE_LANDING_LOCATION = re.compile(
    r"^([^,]*[^\s,])\s*,\s*([A-Z]{2}|D\.C\.)$|^Washington\s+D\.C\.?$",
    re.IGNORECASE,
)


def _extract_landing_locations_html(html: str, source_url: str, base_url: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Parse Landing (hellolanding.com) locations page HTML.

    Structure: h2 = location (e.g. Atlanta, GA), h3 = property name (e.g. Park South).
    Each section has an image collector "View all homes in City, ST" — exclude from properties.
    Returns (properties, location_counts). location_counts includes markets with 0 properties
    (upcoming areas). Properties get market set from their section header.
    """
    soup = BeautifulSoup(html, "html.parser")
    properties: list[dict[str, Any]] = []
    location_counts: list[dict[str, Any]] = []
    current_market: Optional[str] = None

    # Walk all elements in document order; track location sections by h2 headers.
    for tag in soup.find_all(["h2", "h3"]):
        text = (tag.get_text() or "").strip()
        if not text:
            continue

        if tag.name == "h2":
            # Location header: "City, ST", "City, State", or "Washington D.C."
            if _is_landing_location_header(text):
                # Finalize previous location count before switching
                if current_market is not None:
                    count = sum(1 for p in properties if (p.get("market") or "").strip() == current_market)
                    location_counts.append({"market": current_market, "count": count})
                current_market = text
            continue

        if tag.name == "h3":
            # Property name — but skip image collector / nav card if it ever appears as h3.
            if _is_landing_image_collector(text):
                continue
            # Skip location headers that appear as h3 (e.g. "City, ST" or "City, ST - 5 properties" — not a property).
            if _is_landing_location_header_or_prefix(text):
                continue
            if _is_landing_state_or_coming_soon(text):
                continue
            if "no properties available" in text.lower():
                continue
            if len(text) < 2:
                continue

            # Resolve URL: property card may wrap an <a> or have sibling link.
            href = None
            a = tag.find("a", href=True) or tag.find_parent("a", href=True)
            if a and a.get("href"):
                href = (a.get("href") or "").strip()
                if href and not href.startswith("#"):
                    # Exclude "View all homes" links (city-level nav, not property).
                    if _is_landing_image_collector((a.get_text() or "").strip()):
                        href = None
                    elif "/apartments/furnished" in href and href.count("/") <= 4:
                        # City-level page like /s/atlanta-ga/apartments/furnished — not a property.
                        href = None
                    else:
                        href = urljoin(base_url, href) if not href.startswith("http") else href

            if current_market is None:
                current_market = "Unspecified"
            properties.append({
                "url": href,
                "name": text,
                "market": current_market,
                "status": None,
            })

    # Finalize last location
    if current_market is not None:
        count = sum(1 for p in properties if (p.get("market") or "").strip() == current_market)
        location_counts.append({"market": current_market, "count": count})

    # Ensure locations with 0 properties are included: walk h2s again and add any missing.
    seen_markets = {lc["market"] for lc in location_counts}
    for h2 in soup.find_all("h2"):
        text = (h2.get_text() or "").strip()
        if text and _is_landing_location_header(text) and text not in seen_markets:
            location_counts.append({"market": text, "count": 0})
            seen_markets.add(text)

    return properties, location_counts


def _is_kasa_locations_url(url: str) -> bool:
    """True if URL is Kasa's locations page (kasa.com/locations)."""
    if not url:
        return False
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    path = (parsed.path or "").rstrip("/") or "/"
    return ("kasa.com" in host or "kasaliving.com" in host) and path == "/locations"


def _extract_kasa_locations_html(html: str, source_url: str, base_url: str) -> list[dict[str, Any]]:
    """
    Parse Kasa (kasa.com/locations) page: 44 cities, each with multiple properties.
    Structure: h2 or h3 = "City, ST", then links "Go to location City, ST" (city — skip) and
    "View details Apartment/Hotel Property Name" or links to /properties/... (one per property).
    Uses body if main yields no properties (list may be outside <main>).
    """
    soup = BeautifulSoup(html, "html.parser")
    body = soup.find("body") or soup
    if not body:
        return []

    def _parse_from_root(root: Any) -> list[dict[str, Any]]:
        properties = []
        current_market: Optional[str] = None
        # City headers can be h2 or h3; property links are <a>.
        for el in root.find_all(["h2", "h3", "a"]):
            if el.name in ("h2", "h3"):
                text = (el.get_text() or "").strip()
                if _RE_LANDING_LOCATION.match(text) and len(text) < 80:
                    current_market = text
                continue

            if el.name == "a" and current_market:
                href = (el.get("href") or "").strip()
                text = (el.get_text() or "").strip()
                if not href:
                    continue
                # "Go to location City, ST" or "Go to locationCity, ST" = city link, not a property
                lower = (text or "").lower()
                if lower.startswith("go to location"):
                    continue
                # Property: "View details" in text, or href is /properties/... (name from text or inner h3)
                is_property_url = "/properties/" in href
                if "view details" in lower:
                    idx = lower.find("view details")
                    name = (text[idx + len("view details") :].strip() or text).strip()
                    # Strip leading "Apartment "/"Hotel " if present
                    if name.lower().startswith("apartment "):
                        name = name[9:].strip()
                    if name.lower().startswith("hotel "):
                        name = name[6:].strip()
                    if name.lower() in ("apartment", "hotel"):
                        h3 = el.find("h3")
                        if h3:
                            name = (h3.get_text() or "").strip()
                elif is_property_url and text and len(text.strip()) > 2:
                    name = text.strip()
                    if name.lower() in ("apartment", "hotel", "view details"):
                        h3 = el.find("h3")
                        name = (h3.get_text() or "").strip() if h3 else name
                elif is_property_url:
                    h3 = el.find("h3")
                    name = (h3.get_text() or "").strip() if h3 else None
                    if not name or len(name) < 2:
                        continue
                else:
                    continue
                if not name or len(name) < 2:
                    continue
                url_val = urljoin(base_url, href) if not href.startswith("http") else href
                properties.append({
                    "name": name,
                    "url": url_val,
                    "market": current_market,
                    "state": None,
                    "city": None,
                    "status": None,
                })
        return properties

    root = _main_content_root(soup)
    properties = _parse_from_root(root) if root else []
    # If list is outside <main> (e.g. in a sibling section), retry on body
    if not properties and body is not root:
        properties = _parse_from_root(body)
    return properties


def _location_attrs_from_element(el) -> dict[str, str]:
    """Extract data-city, data-state, data-region, data-market, data-location from element if present."""
    out: dict[str, str] = {}
    for attr in _LOCATION_ATTRS:
        if not attr.startswith("data-") and attr != "aria-label":
            continue
        val = el.get(attr) if hasattr(el, "get") else None
        if val and isinstance(val, str) and (v := val.strip()) and len(v) < 200:
            out[attr] = v
    return out


def _merge_location_from_link_and_parents(link) -> dict[str, Any]:
    """Check link and its parents for data-state, data-city, data-region, data-market; return dict of set fields."""
    merged: dict[str, Any] = {}
    el = link
    for _ in range(10):  # limit ancestor walk
        if el is None:
            break
        attrs = _location_attrs_from_element(el) if hasattr(el, "get") else {}
        if attrs.get("data-state") and "state" not in merged:
            merged["state"] = attrs["data-state"].strip()
        if attrs.get("data-city") and "city" not in merged:
            merged["city"] = attrs["data-city"].strip()
        if attrs.get("data-region") and "market" not in merged:
            merged["market"] = attrs["data-region"].strip()
        if attrs.get("data-market") and "market" not in merged:
            merged["market"] = attrs["data-market"].strip()
        if attrs.get("data-location") and "market" not in merged:
            merged["market"] = attrs["data-location"].strip()
        parent = getattr(el, "parent", None)
        el = parent if parent is not None and parent != el else None
    return merged


def extract_properties_from_html(html: str, source_url: str = "") -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    is_landing = "hellolanding.com" in (source_url or "").lower()
    properties = []
    parsed_source = urlparse(source_url or "")
    base_url = f"{parsed_source.scheme}://{parsed_source.netloc}" if parsed_source.scheme and parsed_source.netloc else (source_url or "")
    for link in soup.find_all("a"):
        href = link.get("href") or ""
        text = (link.get_text() or "").strip()
        if not href or len(text) < 3:
            continue
        if not is_property_like(href):
            continue
        if _is_junk_property_link(text, href):
            continue
        if is_landing and _link_in_empty_landing_section(link):
            continue
        # Landing: exclude city-level pages (e.g. /s/atlanta-ga/apartments/furnished) — not individual properties.
        if is_landing and href:
            abs_url = urljoin(base_url, href) if not href.startswith("http") else href
            path = (urlparse(abs_url).path or "").strip()
            if "/apartments/furnished" in abs_url and path.count("/") <= 4:
                continue
        if _link_in_footer(link):
            continue
        # Vacasa: only count /unit/<id> listings, not /about-us, /accessibility, etc.
        if _is_vacasa_source(source_url):
            abs_url = urljoin(base_url, href) if href and not href.startswith("http") else href
            if not _is_vacasa_property_url(abs_url, source_url):
                continue
        # AKA: only count real property slugs (/hotel-aka-*, /aka-*), not /locations/*, /exclusive-offers, /live-it, etc.
        if _is_aka_source(source_url):
            abs_url = urljoin(base_url, href) if href and not href.startswith("http") else href
            if not _is_aka_property_url(abs_url, source_url):
                continue
        # Placemakr: only count city-state pages (e.g. /saltlakecity-ut) and /locations/city/property — exclude nav links like /residential, /partners that would become "Other".
        if _is_placemakr_source(source_url):
            abs_url = urljoin(base_url, href) if href and not href.startswith("http") else href
            if not _is_placemakr_property_url(abs_url, source_url):
                continue
        # Rove: only count /listing/<slug> pages; exclude /search, /collections, /locations, and other nav links that otherwise match generic patterns.
        if _is_rove_source(source_url):
            abs_url = urljoin(base_url, href) if href and not href.startswith("http") else href
            if not _is_rove_property_url(abs_url, source_url):
                continue
        prop: dict[str, Any] = {
            "url": href,
            "name": text,
            "market": None,
            "status": None,
        }
        # Capture data-state, data-city, data-market from link or parent (e.g. Vacasa cards)
        loc = _merge_location_from_link_and_parents(link)
        if loc.get("state"):
            prop["state"] = loc["state"]
        if loc.get("city"):
            prop["city"] = loc["city"]
        if loc.get("market"):
            prop["market"] = loc["market"]
        properties.append(prop)
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

# Set of full US state names (lowercase) for Landing location-header detection.
_US_STATE_NAMES_LOWER = {v.lower() for v in _US_STATE_ABBREV.values()}


def _is_landing_location_header(text: str) -> bool:
    """
    True if text is a Landing location header (City, ST or City, State or Washington D.C.),
    not an individual property name. Used to skip such entries in extraction and in normalize_properties.
    """
    if not text or len(text) >= 80:
        return False
    t = text.strip()
    if _RE_LANDING_LOCATION.match(t):
        return True
    # "City, State" with full state name (e.g. Atlanta, Georgia) — not matched by _RE_LANDING_LOCATION.
    m = _RE_CITY_ST.match(t)
    if m:
        state_part = (m.group(2) or "").strip().lower()
        if state_part in _US_STATE_ABBREV or state_part in _US_STATE_NAMES_LOWER:
            return True
    if re.match(r"^Washington\s+D\.?C\.?$", t, re.IGNORECASE):
        return True
    return False


def _is_landing_location_header_or_prefix(text: str) -> bool:
    """
    True if text is or starts with a Landing location header (e.g. "Austin, TX" or "Dallas, TX - View all").
    Used so we filter out location-style h3s that have trailing suffix (property count, "View all", etc.).
    """
    if not text or len(text) >= 120:
        return False
    t = (text or "").strip()
    # Take leading segment before common suffixes so "City, ST - 5 properties" still matches.
    for sep in (" - ", " — ", " | ", " ("):
        if sep in t:
            t = t.split(sep)[0].strip()
            break
    return _is_landing_location_header(t)


def _is_landing_state_or_coming_soon(text: str) -> bool:
    """
    True if text is a state-only header, "Coming Soon", or similar section label (Landing) — not a property.
    Keeps property count at 198 by excluding these from extraction and normalize_properties.
    """
    if not text:
        return False
    t = (text or "").strip().lower()
    if len(t) <= 3 and t in _US_STATE_ABBREV:
        return True
    if t in _US_STATE_NAMES_LOWER:
        return True
    if re.match(r"^coming\s+soon", t) or re.match(r"^opening\s+soon", t):
        return True
    if re.match(r"^coming\s+\d{4}", t) or re.match(r"^opening\s+\d{4}", t):
        return True
    if t in ("coming soon", "opening soon", "coming 2025", "opening 2025"):
        return True
    return False


# Lark: h2 that is a detail line (Keys, F&B, Brand) not a property name — skip so we don't count as separate property
_RE_LARK_DETAIL_H2 = re.compile(
    r"^(Keys|F&B\s*Outlet|F&B\s*Outlets|Brand)\s*:",
    re.IGNORECASE,
)


def _main_content_root(soup: Any) -> Optional[Any]:
    """Return the main content element (main, [role=main], or body) so we avoid parsing footer/nav."""
    body = soup.find("body") or soup
    if not body:
        return None
    main = body.find("main")
    if main:
        return main
    for el in body.find_all(attrs={"role": "main"}):
        if el:
            return el
    return body


def _extract_lark_style_blocks(html: str, base_url: str) -> List[dict[str, Any]]:
    """
    Parse Lark-style portfolio HTML. Two patterns supported:
    1. Property per block: <h2> (property name) followed by <ul> with location ("City, ST") and details.
    2. Location as section header: <h2> is "City, ST"; <ul> contains one or more properties (each <li>
       may have a link to the property). We emit one block per property so names are property names, not location.
    Only parses within main content (main or body) to avoid counting footer location lists (e.g. Kasa).
    Returns list of {"name", "location_line", "details_list", "url"}.
    """
    soup = BeautifulSoup(html, "html.parser")
    root = _main_content_root(soup)
    if not root:
        return []
    blocks = []
    for h2 in root.find_all("h2"):
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
    """Reduce HTML to plain text for LLM (strip scripts, main content only to skip footer, include location data-*, truncate)."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(["script", "style", "noscript"]):
        tag.decompose()
    root = _main_content_root(soup) or soup.find("body") or soup
    # Collect location metadata from data-* and aria-label so card locations are visible to the LLM
    location_lines = []
    for el in (root.find_all(True) if root else []):
        parts = []
        for attr in _LOCATION_ATTRS:
            val = el.get(attr)
            if val and isinstance(val, str) and (val := val.strip()) and len(val) < 200:
                parts.append(f"{attr}={val}")
        if parts:
            location_lines.append(" ".join(parts))
    text = root.get_text(separator="\n", strip=True) if root else ""
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


def _merge_link_properties_into(block_properties: List[dict[str, Any]], html: str, threshold: int = 25, source_url: str = "") -> List[dict[str, Any]]:
    """
    When we have few properties from Lark-style blocks (e.g. partial HTML or different page structure),
    merge in any additional properties found via link extraction so we don't cap at ~15.
    Preserves block-derived props; adds link-based ones whose URL is not already present.
    """
    if len(block_properties) > threshold or not html:
        return block_properties
    link_props = extract_properties_from_html(html, source_url)
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
    # "View details Apartment/Hotel Property Name" is a valid property name (Kasa locations page); only drop bare "View ..." CTAs.
    if lower.startswith("view details ") and len(n) > 20:
        return False
    if lower.startswith(("book ", "view ", "stay ", "reserve ")):
        return True
    # Landing: "View all homes in City, ST" image collector — never a property.
    if "view all homes" in lower:
        return True
    return False


def _location_tuple(prop: dict[str, Any]) -> tuple[str, str, str]:
    """Normalized (market, city, state) for dedupe key — same name/URL in different cities stay separate."""
    market = ((prop.get("market") or "").strip() or "").lower()
    city = ((prop.get("city") or "").strip() or "").lower()
    state = ((prop.get("state") or "").strip() or "").lower()
    return (market, city, state)


def normalize_properties(properties: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Normalize property dicts and deduplicate by URL+location (and by name+location when URL is missing).

    Same property name or same URL in different cities/markets are NOT combined — only dedupe when
    both identifier and location match (e.g. cross-city listings like "The 55 Elm Club" in Hartford
    vs New Haven remain separate).
    """
    normalized: list[dict[str, Any]] = []
    seen_keys: set[tuple[Any, ...]] = set()

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
        # Landing (and any source): "City, ST" / "City, State" / "Washington D.C." (or with suffix like " - View all") are location headers, not properties.
        if _is_landing_location_header_or_prefix(name):
            continue
        if _is_landing_state_or_coming_soon(name):
            continue
        # Kasa: "View details Apartment/Hotel Property Name" -> store as "Apartment/Hotel Property Name"
        if name.lower().startswith("view details ") and len(name) > 13:
            name = name[13:].strip()

        raw_url = prop.get("url")
        market_val = (prop.get("market") or "").strip() or None
        url_key = _url_key(raw_url)
        loc = _location_tuple(prop)
        if url_key:
            dedupe_key = ("url", url_key, loc)
        else:
            dedupe_key = ("name", name.lower(), loc)
        if dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)

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


def _extract_location_from_json_ld(html: str) -> Optional[Dict[str, str]]:
    """
    Parse script type="application/ld+json" and return city, state, market from
    Place or Product with address (addressLocality, addressRegion).
    """
    soup = BeautifulSoup(html, "html.parser")
    for script in soup.find_all("script", type=re.compile(r"application/ld\+json", re.I)):
        raw = (script.string or "").strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            continue
        for item in data:
            if not isinstance(item, dict):
                continue
            kind = (item.get("@type") or "").strip()
            if isinstance(kind, list):
                kind = (kind[0] or "").strip()
            if kind and "Place" not in kind and "Product" not in kind and "Accommodation" not in kind:
                continue
            addr = item.get("address")
            if not isinstance(addr, dict):
                continue
            locality = (addr.get("addressLocality") or "").strip()
            region = (addr.get("addressRegion") or "").strip()
            if not region and not locality:
                continue
            state = _US_STATE_ABBREV.get(region.lower(), region) if len(region) == 2 else region
            city = locality or None
            market = f"{city}, {region}" if (city and region) else (state or region or "")
            return {"city": city or "", "state": state or "", "market": market or ""}
    return None


def _extract_location_from_text(html: str, max_chars: int = 8000) -> Optional[Dict[str, str]]:
    """Look for 'City, ST' or 'City, State' in visible text; return city, state, market."""
    text = BeautifulSoup(html, "html.parser").get_text(separator=" ", strip=True)
    if len(text) > max_chars:
        text = text[:max_chars]
    match = re.search(_RE_CITY_ST, text)
    if not match:
        return None
    city = (match.group(1) or "").strip()
    state_part = (match.group(2) or "").strip()
    if len(state_part) == 2:
        state = _US_STATE_ABBREV.get(state_part.lower(), state_part)
    else:
        state = state_part
    market = f"{city}, {state_part}" if city and state_part else (state or "")
    return {"city": city or "", "state": state or "", "market": market or ""}


def extract_location_from_property_page(html: str, page_url: str = "") -> Optional[Dict[str, Any]]:
    """
    Extract location (city, state, market) from a single property page HTML.
    Tries JSON-LD first, then regex for 'City, ST' in text. Returns dict with
    city, state, market (or None if nothing found).
    """
    out = _extract_location_from_json_ld(html)
    if out and (out.get("state") or out.get("market")):
        return out
    out = _extract_location_from_text(html)
    if out and (out.get("state") or out.get("market")):
        return out
    return None


def enrich_sitemap_properties_with_locations(
    properties: List[dict[str, Any]],
    max_fetches: int = 300,
    delay_sec: float = 0.3,
    fetch_fn: Optional[Callable[[str], Any]] = None,
) -> List[dict[str, Any]]:
    """
    For sitemap-sourced properties that have url but no market/state/city, fetch
    each property page (up to max_fetches), extract location from JSON-LD or
    page text, and merge back. Returns a new list with location fields filled where
    we successfully extracted.
    """
    if not properties or max_fetches <= 0:
        return list(properties)
    fetch_fn = fetch_fn or (lambda u: fetch_url(u, timeout=15))
    need_enrich = [
        (i, p) for i, p in enumerate(properties)
        if (p.get("url") and not (p.get("market") or (p.get("state") or "").strip()))
    ]
    if not need_enrich:
        return list(properties)
    result = [dict(p) for p in properties]
    fetched = 0
    for i, prop in need_enrich:
        if fetched >= max_fetches:
            break
        url = (prop.get("url") or "").strip()
        if not url:
            continue
        try:
            resp = fetch_fn(url)
            if getattr(resp, "status_code", 0) != 200:
                continue
            text = getattr(resp, "text", None) or getattr(resp, "content", "") or ""
            if isinstance(text, bytes):
                text = text.decode("utf-8", errors="replace")
            loc = extract_location_from_property_page(text, url)
            if loc and (loc.get("state") or loc.get("market")):
                result[i]["state"] = (loc.get("state") or "").strip() or None
                result[i]["city"] = (loc.get("city") or "").strip() or None
                result[i]["market"] = (loc.get("market") or "").strip() or None
            fetched += 1
        except Exception:
            continue
        if delay_sec > 0:
            time.sleep(delay_sec)
    return result


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


def _parse_blueground_slug_city_state(slug: str) -> tuple[str, str]:
    """Parse Blueground destination slug (e.g. agoura-hills-ca-usa) into (city, state)."""
    parts = (slug or "").strip().lower().split("-")
    if len(parts) < 3 or parts[-1] != "usa":
        return ("", "")
    state_abbrev = parts[-2]
    if len(state_abbrev) != 2:
        return ("", "")
    city_parts = parts[:-2]
    city = " ".join(p.capitalize() for p in city_parts)
    state = state_abbrev.upper()
    return (city, state)


def _fetch_blueground_destinations(
    source_url: str, extra_options: Optional[Dict[str, Any]] = None
) -> tuple[str, str, List[dict[str, Any]]]:
    """
    Blueground-specific: destinations page -> each North America USA destination page
    (/m/furnished-apartments/acton-ma-usa) has property links (/p/furnished-apartments/bos-XXX).
    Scrape the destination page directly for those links (no Search click needed).
    extra_options.max_destinations: optional limit (e.g. 2 or 30). If key is absent, defaults to 30
    so UI/scheduled refresh stays ~2–10 min; set explicitly to null for full run of all USA destinations.
    Returns (raw_html, raw_hash, properties).
    """
    opts = extra_options or {}
    max_dest = opts.get("max_destinations")
    # Default cap when key is missing so refresh doesn't run 50+ destinations (15–45+ min).
    if max_dest is None and "max_destinations" not in opts:
        max_dest = 30
    if max_dest is not None:
        max_dest = int(max_dest)

    parsed = urlparse(source_url)
    base = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else source_url
    properties: List[dict[str, Any]] = []
    raw_html_parts: List[str] = []

    def _parse_usa_links(html: str) -> List[tuple[str, str, str]]:
        s = BeautifulSoup(html, "html.parser")
        out: List[tuple[str, str, str]] = []
        seen_slugs: set[str] = set()
        for a in s.find_all("a", href=True):
            href = (a.get("href") or "").strip()
            if "/m/furnished-apartments/" not in href:
                continue
            match = re.search(r"/m/furnished-apartments/([a-z0-9-]+)", href, re.IGNORECASE)
            if not match:
                continue
            slug = match.group(1).lower()
            if not slug.endswith("-usa"):
                continue
            if "canada" in slug or slug.endswith("-on") or slug.endswith("-bc") or slug.endswith("-ab"):
                continue
            if slug in seen_slugs:
                continue
            seen_slugs.add(slug)
            full_url = urljoin(base, href)
            city, state = _parse_blueground_slug_city_state(slug)
            out.append((full_url, city, state))
        return out

    # Step 1: get USA destination links from /destinations.
    # Blueground uses SSR; plain HTTP returns links. Try HTTP first (fast, reliable), fall back to
    # Playwright only if 0 USA links (e.g. if they change to client-only render).
    print(f"[asset] Blueground: fetching destinations page...", flush=True)
    fetched = fetch_url(source_url)
    if fetched.status_code != 200 or not fetched.text:
        raise RuntimeError(f"Blueground destinations fetch failed: {fetched.status_code}")
    raw_html_parts.append(fetched.text)
    usa_links = _parse_usa_links(fetched.text)
    if not usa_links and _playwright_available():
        print(f"[asset] Blueground: HTTP had no USA links; trying Playwright for destinations", flush=True)
        try:
            alt = fetch_url_js_wait_for_spa(
                source_url,
                wait_after_load_sec=5.0,
                wait_until="domcontentloaded",
            )
            if alt.text:
                usa_links = _parse_usa_links(alt.text)
                raw_html_parts[0] = alt.text
        except Exception:
            pass
    if not usa_links:
        print(f"[asset] Blueground: HTTP fetch had no USA links; using destinations page HTML as-is", flush=True)

    if max_dest is not None:
        usa_links = usa_links[:max_dest]
        print(f"[asset] Blueground: using {max_dest} destinations (set extra_options.max_destinations to null for full USA run)", flush=True)
    else:
        print(f"[asset] Blueground: full North America USA run (all destinations)", flush=True)

    n_dest = len(usa_links)
    print(f"[asset] Blueground: found {n_dest} North America USA destinations to scrape", flush=True)
    if n_dest > 50:
        print(f"[asset] Blueground: this may take 15–45+ min ({n_dest} pages); progress below every destination.", flush=True)
    seen_units: set[tuple[str, str]] = set()

    def _extract_properties_from_dest_page(html: str, dest_url: str, city: str, state: str) -> List[dict[str, Any]]:
        """Extract property links from destination page. Links to /p/furnished-apartments/ are individual apartments."""
        s = BeautifulSoup(html, "html.parser")
        out: List[dict[str, Any]] = []
        for a in s.find_all("a", href=re.compile(r"/p/furnished-apartments/")):
            href = (a.get("href") or "").strip()
            prop_url = urljoin(base, href)
            raw = (a.get("title") or a.get_text() or "").strip()
            name = None
            m = re.search(r"(#\d+[A-Z]?\s*•\s*[^\n]+)", raw)
            if m:
                name = m.group(1).strip().rstrip("•").strip()
            elif raw and len(raw) >= 5 and "Add dates" not in raw and "See all" not in raw and "Explore" not in raw:
                name = raw
            if not name or len(name) < 5:
                continue
            key = (name[:100], city, state)
            if key in seen_units:
                continue
            seen_units.add(key)
            market = f"{city}, {state}" if city and state else None
            out.append({
                "url": prop_url,
                "name": name,
                "market": market,
                "status": None,
                "state": state or None,
                "city": city or None,
            })
        return out

    use_js_for_dest = _playwright_available()
    for i, (dest_url, city, state) in enumerate(usa_links):
        print(f"[asset] Blueground: destination {i + 1}/{len(usa_links)} — {city}, {state} ({dest_url})", flush=True)
        try:
            dest_fetched = fetch_url(dest_url)
            if dest_fetched.status_code != 200 or not dest_fetched.text:
                print(f"[asset] Blueground:   HTTP {dest_fetched.status_code}, skipping", flush=True)
                continue
            raw_html_parts.append(dest_fetched.text)
            props = _extract_properties_from_dest_page(dest_fetched.text, dest_url, city, state)
            if not props and use_js_for_dest:
                try:
                    dest_fetched = fetch_url_js_wait_for_spa(
                        dest_url,
                        wait_after_load_sec=5.0,
                        wait_until="domcontentloaded",
                    )
                    if dest_fetched.status_code == 200 and dest_fetched.text:
                        raw_html_parts[-1] = dest_fetched.text
                        props = _extract_properties_from_dest_page(dest_fetched.text, dest_url, city, state)
                except Exception:
                    pass
            properties.extend(props)
            print(f"[asset] Blueground:   found {len(props)} properties (total so far: {len(properties)})", flush=True)
        except Exception as e:
            print(f"[asset] Blueground:   error: {e}", flush=True)
            continue

    print(f"[asset] Blueground: collected {len(properties)} properties from {len(usa_links)} destinations", flush=True)
    import hashlib
    combined = "\n".join(raw_html_parts)
    raw_hash = hashlib.sha256(combined.encode("utf-8")).hexdigest()
    return (combined, raw_hash, properties)


# When a source explicitly sets strategy_chain in extra_options, we only "accept" a strategy result
# if it has at least this many properties; otherwise we try the next in the chain (avoids accepting
# a wrong/partial strategy, e.g. HTML returning 2 links when sitemap would return thousands).
# Sources with no extra_options use the default chain with min_accept=1 (see below).
_ASSET_CHAIN_MIN_PROPERTIES = 11

# When a source has no strategy_chain and no explicit strategy, we try this chain and accept the
# first result with >= 1 property (so seed-added competitors are never excluded by a strict minimum).
_DEFAULT_ASSET_STRATEGY_CHAIN = ["sitemap_first", "js_exhaust", "html"]

# Canonical URL -> single strategy for known competitors. No chain, no fallback; one path per competitor.
# Format: (host_lower_without_www, path_rstrip_slash) -> strategy name. Ensures prioritization changes don't break runs.
# Matches seed_data.json asset URLs: AKA uses homepage /; others use paths below.
_ASSET_CANONICAL_STRATEGY: list[tuple[str, str, str]] = [
    ("stayaka.com", "/", "sitemap_first"),
    ("placemakr.com", "/locations", "html"),
    ("avantstay.com", "/search", "sitemap_first"),
    ("larkhospitality.com", "/portfolio", "js_exhaust"),
    ("theblueground.com", "/destinations", "blueground_destinations"),
    ("hellolanding.com", "/locations", "landing_locations"),
    ("rovetravel.com", "/search", "sitemap_first"),
    ("vacasa.com", "/search", "sitemap_first"),
    ("kasa.com", "/locations", "kasa_locations"),
    ("kasaliving.com", "/locations", "kasa_locations"),
]


def _canonical_asset_strategy(source_url: str) -> Optional[str]:
    """Return the single strategy for this asset URL if it's a known competitor; else None (use chain/infer)."""
    if not source_url:
        return None
    parsed = urlparse(source_url)
    host = (parsed.netloc or "").lower().strip()
    if host.startswith("www."):
        host = host[4:]
    path = (parsed.path or "/").rstrip("/") or "/"
    for h, p, strategy in _ASSET_CANONICAL_STRATEGY:
        if h in host and path == p:
            return strategy
    return None


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
        # Landing locations page: always use dedicated extractor so cities are not treated as properties.
        if _is_landing_locations_url(source_url):
            return "landing_locations"
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

        For Vacasa (~32k /unit/ URLs), we process all root sitemaps (sitemap.xml
        and sitemap.xml.gz) in one pass so the full set is collected; otherwise
        returning after the first root can yield only ~25k from one index.
        """
        roots = discover_sitemap(source_url)
        vacasa_full_pull = _is_vacasa_source(source_url)
        # Vacasa: one pass with all roots so we aggregate from both sitemap.xml and sitemap.xml.gz (full ~32k).
        root_iter: list[str] = [roots[0]] if (vacasa_full_pull and roots) else (roots if roots else [])
        ref_root = roots[0] if roots else ""

        for root_sitemap_url in root_iter:
            visited: set[str] = set()
            property_urls: set[str] = set()
            stack: list[str] = list(roots) if vacasa_full_pull else [root_sitemap_url]

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
                    if not is_property_like(url):
                        pass
                    elif not _is_vacasa_property_url(url, source_url):
                        # Vacasa sitemaps list many pages; only count /unit/<id> listings.
                        pass
                    elif not _is_aka_property_url(url, source_url):
                        # AKA sitemaps list 200+ single-segment URLs; only count real property slugs (/hotel-aka-*, /aka-*).
                        pass
                    elif not _is_rove_property_url(url, source_url):
                        # Rove sitemaps/listings: only count /listing/<slug>; exclude /search, /collections, /locations, etc.
                        pass
                    else:
                        property_urls.add(url)
                        continue

                    # Otherwise, if it's another sitemap (same origin or same-site, e.g. vacasa.com → vacasa.ca),
                    # enqueue it so we can pull property URLs from section-specific sitemaps.
                    # Use path to handle paginated sitemaps like sitemap-units.xml?p=2 (Vacasa).
                    path = (urlparse(url).path or "").lower()
                    if path.endswith(".xml") or path.endswith(".xml.gz"):
                        parsed_child = urlparse(url)
                        parsed_root = urlparse(ref_root)
                        if (
                            parsed_child.netloc
                            and _same_site_netloc(parsed_child.netloc, parsed_root.netloc)
                            and url not in visited
                        ):
                            stack.append(url)

            # Only return sitemap result when we found property-like URLs so caller can fall back to HTML.
            if property_urls:
                props = normalize_properties(
                    [{"url": url, "name": url} for url in sorted(property_urls)]
                )
                if opts.get("enrich_sitemap_locations") and props:
                    max_fetches = opts.get("enrich_sitemap_max_fetches", 300)
                    delay = opts.get("enrich_sitemap_delay_sec", 0.3)
                    props = enrich_sitemap_properties_with_locations(
                        props, max_fetches=max_fetches, delay_sec=delay
                    )
                return {
                    "source_url": ref_root,
                    "raw_content": None,
                    "raw_hash": None,
                    "properties": props,
                }
            if vacasa_full_pull:
                break

        return None

    def fetch_from_html() -> dict[str, Any]:
        fetched = fetch_url(source_url)
        if fetched.status_code != 200:
            raise RuntimeError(
                f"Asset fetch failed: {fetched.url} returned HTTP {fetched.status_code}. "
                "Refusing to parse or persist; check Runs for this error."
            )
        properties = extract_properties_from_html(fetched.text, source_url)
        return {
            "source_url": fetched.url,
            "raw_content": fetched.text,
            "raw_hash": fetched.raw_hash,
            "properties": normalize_properties(properties),
            "note": "html",
        }

    def _fetch_without_browser() -> dict[str, Any]:
        """Sitemap first, then HTML; used when JS/Playwright is not available (e.g. Render cron).
        When llm_extract is True, tries Lark-style block extraction first so we get the initial
        batch (~6 properties) from the first HTML; then falls back to generic LLM or link extraction."""
        sitemap_snapshot = fetch_from_sitemap()
        if sitemap_snapshot and sitemap_snapshot.get("properties"):
            return {**sitemap_snapshot, "note": "sitemap_first"}
        # Kasa (and similar) often return minimal/JS shell for bot User-Agent; use browser UA so we get full HTML.
        fetch_headers = {"User-Agent": USER_AGENT_BROWSER} if _is_kasa_locations_url(source_url) else None
        fetched = fetch_url(source_url, headers=fetch_headers)
        if fetched.status_code != 200:
            raise RuntimeError(
                f"Asset fetch failed: {fetched.url} returned HTTP {fetched.status_code}. "
                "Refusing to parse or persist; check Runs for this error."
            )
        parsed = urlparse(source_url)
        base_url = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else source_url

        # Kasa locations: same parser works on plain HTML when page is server-rendered (no Playwright needed).
        if _is_kasa_locations_url(source_url):
            kasa_props = _extract_kasa_locations_html(fetched.text, source_url, base_url)
            if kasa_props:
                return {
                    "source_url": fetched.url,
                    "raw_content": fetched.text,
                    "raw_hash": fetched.raw_hash,
                    "properties": normalize_properties(kasa_props),
                    "note": "html_kasa_locations",
                }

        # Try Lark-style h2/ul blocks first (works on initial HTML without Load more; gives ~6 properties).
        lark_blocks = _extract_lark_style_blocks(fetched.text, base_url)
        if lark_blocks and opts.get("llm_extract"):
            properties = _extract_properties_via_llm_from_blocks(lark_blocks, source_url)
            properties = _merge_link_properties_into(properties, fetched.text, threshold=999, source_url=source_url)
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
            properties = _merge_link_properties_into(properties, fetched.text, threshold=999, source_url=source_url)
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
                properties = extract_properties_from_html(fetched.text, source_url)
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
        properties = extract_properties_from_html(fetched.text, source_url)
        return {
            "source_url": fetched.url,
            "raw_content": fetched.text,
            "raw_hash": fetched.raw_hash,
            "properties": normalize_properties(properties),
            "note": "html",
        }

    def run_one_strategy(strategy: str) -> dict[str, Any]:
        """Run a single strategy by name; returns snapshot dict. Used for both chain and single-strategy."""
        if strategy == "kasa_locations":
            # Kasa only: one fetch + parser. No Playwright, no load_more, no link merge — yields 77 properties.
            fetch_headers = {"User-Agent": USER_AGENT_BROWSER}
            fetched = fetch_url(source_url, headers=fetch_headers)
            if fetched.status_code != 200:
                raise RuntimeError(
                    f"Asset fetch failed: {fetched.url} returned HTTP {fetched.status_code}. "
                    "Refusing to parse or persist; check Runs for this error."
                )
            parsed = urlparse(source_url)
            base_url = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else source_url
            kasa_props = _extract_kasa_locations_html(fetched.text, source_url, base_url)
            return {
                "source_url": fetched.url,
                "raw_content": fetched.text,
                "raw_hash": fetched.raw_hash,
                "properties": normalize_properties(kasa_props),
                "note": "kasa_locations",
            }
        if strategy == "landing_locations":
            parsed = urlparse(source_url)
            base_url = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else source_url
            # Landing locations page has full SSR content; plain HTTP fetch returns complete HTML.
            # Playwright can return pre-hydration HTML with fewer elements.
            fetched = fetch_url(source_url)
            if fetched.status_code != 200:
                raise RuntimeError(
                    f"Asset fetch failed: {fetched.url} returned HTTP {fetched.status_code}. "
                    "Refusing to parse or persist; check Runs for this error."
                )
            properties, location_counts = _extract_landing_locations_html(
                fetched.text, source_url, base_url
            )
            return {
                "source_url": fetched.url,
                "raw_content": fetched.text,
                "raw_hash": fetched.raw_hash,
                "properties": normalize_properties(properties),
                "location_counts": location_counts,
                "note": "landing_locations",
            }
        if strategy == "blueground_destinations":
            raw_html, raw_hash, properties = _fetch_blueground_destinations(source_url, opts)
            return {
                "source_url": source_url,
                "raw_content": raw_html,
                "raw_hash": raw_hash,
                "properties": normalize_properties(properties),
                "note": "blueground_destinations",
            }
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
                try:
                    fetched = fetch_url_js_exhaust(source_url, load_more)
                except Exception:
                    # Playwright failed (timeout, crash, etc.); for Kasa we can fall back to HTML-only.
                    if _is_kasa_locations_url(source_url):
                        return _fetch_without_browser()
                    raise
                parsed = urlparse(source_url)
                base_url = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else source_url
                # Kasa locations: parse "View details Apartment/Hotel Name" under each city (77 properties, not 44 city links).
                if _is_kasa_locations_url(source_url):
                    kasa_props = _extract_kasa_locations_html(fetched.text, source_url, base_url)
                    if kasa_props:
                        return {
                            "source_url": fetched.url,
                            "raw_content": fetched.text,
                            "raw_hash": fetched.raw_hash,
                            "properties": normalize_properties(kasa_props),
                            "note": "js_exhaust_kasa_locations",
                        }
                if opts.get("llm_extract"):
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
                        properties = extract_properties_from_html(fetched.text, source_url)
                    properties = _merge_link_properties_into(properties or [], fetched.text, threshold=999, source_url=source_url)
                    return {
                        "source_url": fetched.url,
                        "raw_content": fetched.text,
                        "raw_hash": fetched.raw_hash,
                        "properties": normalize_properties(properties),
                        "note": "js_exhaust" if not lark_blocks else "js_exhaust_lark_blocks",
                    }
                properties = extract_properties_from_html(fetched.text, source_url)
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
                properties = extract_properties_from_html(fetched.text, source_url)
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
            # Landing (hellolanding.com) locations page is JS-rendered; use Playwright to get full property list.
            if "hellolanding.com" in source_url.lower() and _playwright_available():
                try:
                    fetched = fetch_url_js(source_url)
                except Exception:
                    fetched = fetch_url(source_url)
            else:
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
                properties = extract_properties_from_html(fetched.text, source_url)
                return {
                    "source_url": fetched.url,
                    "raw_content": fetched.text,
                    "raw_hash": fetched.raw_hash,
                    "properties": normalize_properties(properties),
                    "note": "html",
                }
            properties = extract_properties_from_html(fetched.text, source_url)
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

    # Known competitors: run exactly one strategy (no chain, no infer). Avoids prioritization/fallback issues.
    canonical = _canonical_asset_strategy(source_url)
    if canonical is not None:
        return run_one_strategy(canonical)

    # --- Strategy chain: try strategies in order until one returns >= min_properties ---
    # Use explicit chain, or default chain for unknown sources (no strategy_chain and no explicit strategy).
    # When a source has an explicit single strategy (e.g. Lark "js_exhaust" + load_more), use that only.
    chain = opts.get("strategy_chain")
    if opts.get("strategy") is not None:
        chain = None  # Single strategy takes precedence (Lark, Blueground, etc.)
    if opts.get("strategy") is None:
        if chain is None or (isinstance(chain, list) and len(chain) == 0):
            chain = _DEFAULT_ASSET_STRATEGY_CHAIN
        # Landing locations page: try landing_locations first so we never run generic HTML/link extraction (which can let cities through).
        if chain and _is_landing_locations_url(source_url) and (chain[0] if chain else None) != "landing_locations":
            chain = ["landing_locations"] + list(chain)
    # Seed-added competitors often have no extra_options; accept any non-empty result (min_accept=1).
    # Explicit strategy_chain in opts keeps stricter min (5) unless they set min_properties_accept.
    using_default_chain = chain == _DEFAULT_ASSET_STRATEGY_CHAIN
    if "min_properties_accept" in opts:
        min_accept = opts["min_properties_accept"]
    else:
        min_accept = 1 if using_default_chain else _ASSET_CHAIN_MIN_PROPERTIES
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
        # Return best we got (even 0) so the run persists and logs instead of raising.
        if last_snapshot:
            return last_snapshot
        return {
            "source_url": source_url,
            "raw_content": "",
            "raw_hash": None,
            "properties": [],
            "note": "chain_exhausted",
        }

    # --- Single strategy (explicit strategy in opts, no chain) ---
    strategy = _infer_strategy()
    return run_one_strategy(strategy)


def build_structured_json(snapshot: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "source_url": snapshot.get("source_url"),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "properties": snapshot.get("properties", []),
        "note": snapshot.get("note"),
    }
    if snapshot.get("location_counts"):
        out["location_counts"] = snapshot["location_counts"]
    return out
