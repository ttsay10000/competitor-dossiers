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

# Destination slug (from URLs like /{id}/{destination}/{slug}) -> US state full name.
# Used to tag locations/states from Avantstay-style URLs (e.g. coachella-valley -> California).
# Slugs are normalized to lowercase with hyphens. Include -XX suffix variants (e.g. austin-tx -> Texas).
_DESTINATION_SLUG_TO_STATE = {
    # California
    "coachella-valley": "California",
    "palm-springs": "California",
    "newport-beach": "California",
    "san-diego": "California",
    "los-angeles": "California",
    "big-bear": "California",
    "lake-tahoe": "California",
    "santa-barbara": "California",
    "malibu": "California",
    "joshua-tree": "California",
    "south-lake-tahoe": "California",
    "mammoth-lakes": "California",
    "san-francisco": "California",
    "napa": "California",
    "sonoma": "California",
    "carlsbad": "California",
    "laguna-beach": "California",
    "dana-point": "California",
    "indio": "California",
    "la-quinta": "California",
    "indian-wells": "California",
    "desert-hot-springs": "California",
    "rancho-mirage": "California",
    "idyllwild": "California",
    "temecula": "California",
    "paso-robles": "California",
    "lake-arrowhead": "California",
    "arrowhead": "California",
    "central-coast": "California",
    "monterey": "California",
    "carmel": "California",
    "santee": "California",
    "murrieta": "California",
    # Colorado
    "denver": "Colorado",
    "breckenridge": "Colorado",
    "vail": "Colorado",
    "telluride": "Colorado",
    "crested-butte": "Colorado",
    "steamboat-springs": "Colorado",
    "aspen": "Colorado",
    "winter-park": "Colorado",
    "silverthorne": "Colorado",
    "dillon": "Colorado",
    "frisco": "Colorado",
    # Texas
    "austin": "Texas",
    "austin-tx": "Texas",
    "galveston": "Texas",
    "san-antonio": "Texas",
    "houston": "Texas",
    "dallas": "Texas",
    "fredericksburg": "Texas",
    "hill-country": "Texas",
    "port-aransas": "Texas",
    "south-padre-island": "Texas",
    # Florida
    "miami": "Florida",
    "miami-beach": "Florida",
    "destin": "Florida",
    "panama-city-beach": "Florida",
    "orlando": "Florida",
    "tampa": "Florida",
    "naples": "Florida",
    "south-florida": "Florida",
    "gulf-shores": "Florida",  # AL; often grouped with FL beach
    "key-west": "Florida",
    "marco-island": "Florida",
    "fort-myers": "Florida",
    "fort-myers-beach": "Florida",
    "st-augustine": "Florida",
    "emerald-coast": "Florida",
    "30a": "Florida",
    # Hawaii
    "maui": "Hawaii",
    "oahu": "Hawaii",
    "big-island": "Hawaii",
    "kauai": "Hawaii",
    "honolulu": "Hawaii",
    "lahaina": "Hawaii",
    "kihei": "Hawaii",
    "wailea": "Hawaii",
    "kapaa": "Hawaii",
    # Tennessee
    "nashville": "Tennessee",
    "gatlinburg": "Tennessee",
    "pigeon-forge": "Tennessee",
    "smoky-mountains": "Tennessee",
    "sevierville": "Tennessee",
    # Utah
    "park-city": "Utah",
    "salt-lake-city": "Utah",
    "moab": "Utah",
    # Nevada
    "las-vegas": "Nevada",
    "lake-tahoe-nv": "Nevada",
    # Arizona
    "scottsdale": "Arizona",
    "phoenix": "Arizona",
    "sedona": "Arizona",
    # New Mexico
    "santa-fe": "New Mexico",
    "taos": "New Mexico",
    # Oregon
    "bend": "Oregon",
    "portland": "Oregon",
    "cannon-beach": "Oregon",
    "central-oregon": "Oregon",
    "sunriver": "Oregon",
    "sisters": "Oregon",
    # Washington
    "seattle": "Washington",
    "leavenworth": "Washington",
    "san-juan-islands": "Washington",
    # Idaho
    "sun-valley": "Idaho",
    "boise": "Idaho",
    # Montana
    "big-sky": "Montana",
    "whitefish": "Montana",
    # Wyoming
    "jackson-hole": "Wyoming",
    "teton-village": "Wyoming",
    # South Carolina
    "charleston": "South Carolina",
    "myrtle-beach": "South Carolina",
    "hilton-head": "South Carolina",
    "kiawah-island": "South Carolina",
    # North Carolina
    "asheville": "North Carolina",
    "outer-banks": "North Carolina",
    "charlotte": "North Carolina",
    # Georgia
    "atlanta": "Georgia",
    "savannah": "Georgia",
    "lake-ounee": "Georgia",
    # Louisiana
    "new-orleans": "Louisiana",
    # Alabama
    "gulf-shores-al": "Alabama",
    "orange-beach": "Alabama",
    # New York
    "new-york": "New York",
    "hamptons": "New York",
    "lake-placid": "New York",
    "hudson-valley": "New York",
    "catskills": "New York",
    "adirondacks": "New York",
    "berkshires": "Massachusetts",  # MA/NY; pick MA as primary
    # Pennsylvania (multi-state regions: pick one nearest state)
    "poconos": "Pennsylvania",
    # Massachusetts
    "cape-cod": "Massachusetts",
    "boston": "Massachusetts",
    # Maine
    "bar-harbor": "Maine",
    "portland-me": "Maine",
    # Vermont
    "stowe": "Vermont",
    "killington": "Vermont",
    # New Hampshire
    "white-mountains": "New Hampshire",
    "north-conway": "New Hampshire",
    # Michigan
    "traverse-city": "Michigan",
    "petoskey": "Michigan",
    # Wisconsin
    "lake-geneva": "Wisconsin",
    "door-county": "Wisconsin",
    # Minnesota
    "brainerd": "Minnesota",
    "duluth": "Minnesota",
    # Other common
    "branson": "Missouri",
    "ozarks": "Missouri",
    "arkansas": "Arkansas",
    "hot-springs": "Arkansas",
}


def _parse_avantstay_style_path(path: str) -> Optional[str]:
    """Extract destination slug from Avantstay-style path.
    Supports /{numeric_id}/{destination_slug}/{property_slug} (3+ parts) and
    /{numeric_id}/{destination_slug} (2 parts) so state can be resolved from sitemap URLs."""
    parts = [p for p in path.split("/") if p]
    if len(parts) >= 3 and parts[0].isdigit():
        return parts[1]
    if len(parts) == 2 and parts[0].isdigit():
        return parts[1]
    return None


def resolve_destination_slug_to_state(slug: str) -> Optional[str]:
    """
    Resolve a destination slug (e.g. coachella-valley, palm-springs, austin-tx) to US state full name.
    Uses static map; if slug ends with -XX (2-letter state abbrev), uses US_STATE_ABBREV.
    """
    if not (slug or "").strip():
        return None
    s = (slug or "").strip().lower()
    # e.g. austin-tx -> Texas
    if re.match(r"^[a-z0-9-]+-[a-z]{2}$", s):
        state_abbrev = s[-2:]
        if state_abbrev in US_STATE_ABBREV:
            return US_STATE_ABBREV[state_abbrev]
    return _DESTINATION_SLUG_TO_STATE.get(s)


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

    loc = (prop.get("market") or prop.get("location") or prop.get("region") or "").strip()
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

    # Avantstay-style (and similar): segment right after the numbers is the location.
    # /{numeric_id}/{location_slug}/... or /{numeric_id}/{location_slug} -> use that slug as location.
    dest_slug = _parse_avantstay_style_path(path)
    if dest_slug and dest_slug.lower() not in NON_LOCATION_PATH_SEGMENTS:
        state = resolve_destination_slug_to_state(dest_slug)
        if state:
            return state
        # Not in our state map: use the slug as the location label (e.g. "newport-beach" -> "Newport Beach").
        return dest_slug.replace("-", " ").title()

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
