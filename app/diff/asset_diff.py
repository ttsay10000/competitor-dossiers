import hashlib
import re
from typing import Optional
from urllib.parse import urlparse

# US state abbreviation -> full name for "by state" summary on dossier
US_STATE_ABBREV = {
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

# URL path segments that are not geographic locations (treat as Unspecified).
NON_LOCATION_PATH_SEGMENTS = frozenset({
    "search",
    "portfolio",
    "brands",
    "career-site",
    "careers",
    "jobs",
    "about",
    "contact",
    "blog",
    "press",
    "news",
    "legal",
    "terms",
    "privacy-policy",
    "privacy",
    "login",
    "signup",
    "cdn-cgi",
    "hotels",
    "api",
    "admin",
    "assets",
    "static",
    "www",
    "en",
    "us",
    # Marketing / category pages that should never be treated as geographic locations
    # (e.g. Placemakr: /extended-stays, /corporate-group, /business, /residents).
    "extended-stays",
    "extended-stay",
    "corporate-group",
    "corporate-stays",
    "business",
    "residents",
})

# Raw location labels that are merged into "Other" and expanded as subbullets (include location for quick check).
LOCATIONS_TREATED_AS_OTHER = frozenset({
    "Unspecified", "Other", "Career Site", "Career site", "Cdn Cgi", "Hotels", "Privacy Policy",
})


def is_location_treated_as_other(loc: str) -> bool:
    """True if this raw location is non-state and should be shown under 'Other' with subbullets."""
    return (loc or "").strip() in LOCATIONS_TREATED_AS_OTHER


def infer_location_for_property(prop: dict) -> str:
    """
    Derive a location label (prefer state) for grouping so dossier can show counts by state/city.
    Prefers LLM-set state/city; then market/location; else parses URL for city-state or path segment.
    """
    state = (prop.get("state") or "").strip()
    city = (prop.get("city") or "").strip()
    if state:
        if city:
            return f"{state} - {city}"
        return state

    loc = (prop.get("market") or prop.get("location") or "").strip()
    if loc:
        return loc

    url = (prop.get("url") or "").strip()
    if not url:
        return "Unspecified"

    # Normalize to path only (relative or absolute)
    if "://" in url:
        parsed = urlparse(url.split("?")[0])
        path = parsed.path or ""
    else:
        path = url.split("?")[0]
    path = path.rstrip("/") or "/"

    # Match trailing -XX (state abbrev), e.g. /saltlakecity-ut, /austin-tx
    match = re.search(r"[-/]([a-z0-9]+)-([a-z]{2})(?:/|\?|$)", path, re.IGNORECASE)
    if match:
        state_abbrev = match.group(2).lower()
        if state_abbrev in US_STATE_ABBREV:
            return US_STATE_ABBREV[state_abbrev]

    # Match /locations/city/... or /city/... -> use city (title-case)
    parts = [p for p in path.split("/") if p]
    if len(parts) >= 2 and parts[0].lower() in ("locations", "properties", "homes", "destinations"):
        city_slug = parts[1]
        if city_slug and city_slug != "search" and city_slug.lower() not in NON_LOCATION_PATH_SEGMENTS:
            return city_slug.replace("-", " ").title()
    if len(parts) >= 1 and parts[0]:
        first = parts[0].lower()
        if first not in NON_LOCATION_PATH_SEGMENTS:
            slug = parts[0].replace("-", " ").title()
            if len(slug) > 1:
                return slug

    return "Unspecified"


def property_identity(prop: dict) -> str:
    if prop.get("url"):
        return prop["url"]
    key = f"{prop.get('name','')}|{prop.get('market','')}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def diff_properties(previous: list[dict], current: list[dict]) -> dict[str, list[dict]]:
    prev_map = {property_identity(prop): prop for prop in previous}
    curr_map = {property_identity(prop): prop for prop in current}

    added = [prop for key, prop in curr_map.items() if key not in prev_map]
    removed = [prop for key, prop in prev_map.items() if key not in curr_map]

    return {"added": added, "removed": removed}


def extract_markets(properties: list[dict]) -> set[str]:
    """Market/location labels for event detection (new market, market exit). Uses same label as dossier grouping."""
    return {infer_location_for_property(prop) for prop in properties}


def location_key(prop: dict) -> str:
    """Location label for grouping (state/city when derivable from URL, else market/location or Unspecified)."""
    return infer_location_for_property(prop)


def parse_keys_from_details(details: Optional[str]) -> int:
    """Extract total key count from a property details string (e.g. '67 keys' or 'Keys: 67'). Returns 0 if missing."""
    if not details or not isinstance(details, str):
        return 0
    m = re.search(r"(?:^|\s|;|,)\s*(\d+)\s*keys?\s*(?:\s|;|,|$)", details.strip(), re.IGNORECASE)
    if m:
        return int(m.group(1))
    m = re.search(r"keys?\s*[:\-]\s*(\d+)", details.strip(), re.IGNORECASE)
    if m:
        return int(m.group(1))
    return 0


def delta_by_city(added: list[dict], removed: list[dict]) -> list[dict]:
    """Return per-city added/removed counts: list of {location, added, removed}."""
    by_loc: dict[str, dict[str, int]] = {}
    for prop in added:
        loc = location_key(prop)
        if loc not in by_loc:
            by_loc[loc] = {"location": loc, "added": 0, "removed": 0}
        by_loc[loc]["added"] += 1
    for prop in removed:
        loc = location_key(prop)
        if loc not in by_loc:
            by_loc[loc] = {"location": loc, "added": 0, "removed": 0}
        by_loc[loc]["removed"] += 1
    return sorted(by_loc.values(), key=lambda x: (-x["added"] - x["removed"], x["location"]))
