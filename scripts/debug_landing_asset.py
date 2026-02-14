#!/usr/bin/env python3
"""
Fetch Landing locations page, run extractor + normalize, and print raw vs normalized
so we can see what aggregates to 221 and what should be filtered to 198.
"""
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.collectors.asset import (
    fetch_url,
    _extract_landing_locations_html,
    normalize_properties,
    _is_landing_location_header_or_prefix,
    _is_landing_state_or_coming_soon,
)

SOURCE_URL = "https://www.hellolanding.com/locations"


def main():
    parsed = urlparse(SOURCE_URL)
    base_url = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else SOURCE_URL

    print("Fetching", SOURCE_URL, "...")
    fetched = fetch_url(SOURCE_URL)
    if fetched.status_code != 200:
        print(f"HTTP {fetched.status_code}")
        return
    html = fetched.text
    print(f"HTML length: {len(html)} chars\n")

    properties, location_counts = _extract_landing_locations_html(html, SOURCE_URL, base_url)
    print(f"Raw from _extract_landing_locations_html: {len(properties)} properties")
    print(f"location_counts entries: {len(location_counts)}")

    # Show which names would be dropped by normalize (location header or state/coming soon)
    dropped_by_header = [p["name"] for p in properties if _is_landing_location_header_or_prefix(p["name"])]
    dropped_by_state = [p["name"] for p in properties if _is_landing_state_or_coming_soon(p["name"])]
    print(f"\nWould drop by _is_landing_location_header_or_prefix: {len(dropped_by_header)}")
    if dropped_by_header:
        for n in dropped_by_header[:30]:
            print(f"  - {n!r}")
        if len(dropped_by_header) > 30:
            print(f"  ... and {len(dropped_by_header) - 30} more")
    print(f"\nWould drop by _is_landing_state_or_coming_soon: {len(dropped_by_state)}")
    if dropped_by_state:
        for n in dropped_by_state[:30]:
            print(f"  - {n!r}")

    normalized = normalize_properties(properties)
    print(f"\nAfter normalize_properties: {len(normalized)} properties")

    # List all raw property names (first 80) to see patterns
    print("\n--- First 80 raw property names (name | market) ---")
    for i, p in enumerate(properties[:80]):
        print(f"  {i+1:3}. {p['name']!r}  |  {p.get('market', '')!r}")
    if len(properties) > 80:
        print(f"  ... and {len(properties) - 80} more")

    # Names that look like location/state (not caught by current filters)
    print("\n--- Raw names that look like City/State/region (potential false positives) ---")
    city_st = re.compile(r"^[^,]+,\s*[A-Za-z]{2,}", re.IGNORECASE)
    for p in properties:
        n = (p.get("name") or "").strip()
        if not n:
            continue
        if city_st.match(n) and not _is_landing_location_header_or_prefix(n):
            print(f"  {n!r}  (market: {p.get('market')!r})")
        if len(n) < 25 and n in ("Texas", "Florida", "Georgia", "California", "Colorado", "Arizona", "Washington", "Coming Soon", "Opening Soon"):
            print(f"  {n!r}  (market: {p.get('market')!r})")

    # Dedup analysis: same name in multiple markets? Or same name+url?
    from collections import defaultdict
    by_name = defaultdict(list)
    for p in properties:
        by_name[(p.get("name") or "").strip()].append(p.get("market"))
    dupes = {k: v for k, v in by_name.items() if len(v) > 1}
    print(f"\n--- Duplicate property names (same name in multiple markets): {len(dupes)} names ---")
    if dupes:
        for name, markets in sorted(dupes.items(), key=lambda x: -len(x[1]))[:25]:
            print(f"  {name!r} -> {markets}")
        total_dupe_entries = sum(len(m) for m in dupes.values())
        unique_if_deduped = len(properties) - total_dupe_entries + len(dupes)
        print(f"  If we count each name once: {len(by_name)} unique names (would be {unique_if_deduped} if deduping by name only)")
    print(f"\n  Total unique names (by name only): {len(by_name)}")
    print(f"  Total entries (current): {len(properties)}")
    print(f"  Difference: {len(properties) - len(by_name)} (duplicate name across markets)")


if __name__ == "__main__":
    main()
