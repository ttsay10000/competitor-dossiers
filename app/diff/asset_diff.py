import hashlib


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
    """City/market label for grouping (same logic as dossier properties_by_location)."""
    loc = (prop.get("market") or prop.get("location") or "Unspecified").strip()
    return loc or "Unspecified"


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
