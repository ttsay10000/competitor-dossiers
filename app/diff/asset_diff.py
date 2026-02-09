import hashlib
import re
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


def infer_location_for_property(prop: dict) -> str:
    """
    Derive a location label (prefer state) for grouping so dossier can show counts by state/city.
    Uses market/location if set; else parses URL for city-state (e.g. austin-tx -> Texas) or path segment.
    """
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
        if city_slug and city_slug != "search":
            return city_slug.replace("-", " ").title()
    if len(parts) >= 1 and parts[0] and parts[0].lower() not in ("search", "portfolio", "brands"):
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
    return {prop.get("market") for prop in properties if prop.get("market")}


def location_key(prop: dict) -> str:
    """Location label for grouping (state/city when derivable from URL, else market/location or Unspecified)."""
    return infer_location_for_property(prop)


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
