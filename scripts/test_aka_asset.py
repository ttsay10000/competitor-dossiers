#!/usr/bin/env python3
"""Test AKA asset collector: run and print property count + sample URLs to spot location vs property."""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.collectors.asset import (
    collect_asset_snapshot,
    normalize_properties,
    _is_aka_source,
    _is_aka_property_url,
)
from app.diff.asset_diff import infer_location_for_property, is_location_treated_as_other

SOURCE = "https://www.stayaka.com/locations"


def main() -> None:
    print("=" * 60)
    print("AKA ASSET COLLECTOR TEST")
    print("=" * 60)
    print(f"  Source: {SOURCE}")
    print(f"  _is_aka_source: {_is_aka_source(SOURCE)}")

    # Test sitemap-first (what production often uses)
    print("\n--- Strategy: sitemap_first ---")
    snap_sitemap = collect_asset_snapshot(
        SOURCE,
        js_required=False,
        use_sitemap_first=True,
        extra_options={},
    )
    props_sitemap = (snap_sitemap.get("properties") or [])
    note_s = snap_sitemap.get("note") or "unknown"
    print(f"  note: {note_s}")
    print(f"  property count: {len(props_sitemap)}")
    if props_sitemap:
        by_loc = {}
        other_count = 0
        for p in props_sitemap:
            loc = infer_location_for_property(p)
            by_loc[loc] = by_loc.get(loc, 0) + 1
            if is_location_treated_as_other(loc):
                other_count += 1
        print(f"  locations (inferred): {dict(sorted(by_loc.items(), key=lambda x: -x[1]))}")
        print(f"  in 'Other' (Unspecified etc.): {other_count}")
        print("  sample URLs (first 25):")
        for p in props_sitemap[:25]:
            url = (p.get("url") or "").strip()
            name = (p.get("name") or "").strip() or "(no name)"
            loc = infer_location_for_property(p)
            print(f"    {loc}: {name[:40]} | {url[:70]}")
        if len(props_sitemap) > 25:
            print(f"    ... and {len(props_sitemap) - 25} more")

    # Test HTML-only (no sitemap)
    print("\n--- Strategy: html (no sitemap) ---")
    snap_html = collect_asset_snapshot(
        SOURCE,
        js_required=False,
        use_sitemap_first=False,
        extra_options={},
    )
    props_html = (snap_html.get("properties") or [])
    note_h = snap_html.get("note") or "unknown"
    print(f"  note: {note_h}")
    print(f"  property count: {len(props_html)}")
    if props_html:
        by_loc_h = {}
        for p in props_html:
            loc = infer_location_for_property(p)
            by_loc_h[loc] = by_loc_h.get(loc, 0) + 1
        print(f"  locations (inferred): {dict(sorted(by_loc_h.items(), key=lambda x: -x[1]))}")
        print("  all URLs:")
        for p in props_html:
            url = (p.get("url") or "").strip()
            name = (p.get("name") or "").strip() or "(no name)"
            print(f"    {name[:45]} | {url}")

    print("\n" + "=" * 60)
    print("Expected: ~15–16 real properties (excludes offers, guides, language pages).")
    print("If count >> 16, non-property slugs may still be included.")
    print("=" * 60)


if __name__ == "__main__":
    main()
