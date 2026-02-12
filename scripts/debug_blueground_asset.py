#!/usr/bin/env python3
"""Debug Blueground asset build: trace each step to find where it fails."""

import os
import sys

# Ensure project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("PLAYWRIGHT_ENABLED", "true")

from app.config import settings
from app.collectors.asset import (
    _fetch_blueground_destinations,
    _parse_blueground_slug_city_state,
    collect_asset_snapshot,
    normalize_properties,
    build_structured_json as build_asset_structured,
)
from app.llm_structured import enrich_properties_with_llm


def main() -> None:
    source_url = "https://www.theblueground.com/destinations"

    print("=" * 60)
    print("BLUEGROUND ASSET BUILD – DIAGNOSTIC")
    print("=" * 60)

    # Step 0: Environment
    print("\n--- Step 0: Environment ---")
    pw_enabled = getattr(settings, "playwright_enabled", False)
    print(f"  PLAYWRIGHT_ENABLED: {pw_enabled}")
    try:
        from playwright.sync_api import sync_playwright
        print("  playwright import: OK")
    except ImportError as e:
        print(f"  playwright import: FAILED - {e}")

    # Step 1: Parse slug helper
    print("\n--- Step 1: Slug parsing ---")
    for slug in ["acton-ma-usa", "agoura-hills-ca-usa", "atlanta-ga-usa"]:
        city, state = _parse_blueground_slug_city_state(slug)
        print(f"  {slug} -> city={city!r}, state={state!r}")

    # Step 2: Link discovery (simulate what _fetch_blueground_destinations does)
    print("\n--- Step 2: USA link discovery (from destinations page) ---")
    try:
        raw_html, raw_hash, properties = _fetch_blueground_destinations(source_url)
        print(f"  raw_hash: {raw_hash[:16]}...")
        print(f"  raw properties (before normalize): {len(properties)}")
        if properties:
            for p in properties[:3]:
                print(f"    - {p.get('name')!r} @ {p.get('city')},{p.get('state')}")
        else:
            print("  (no properties extracted)")
    except Exception as e:
        print(f"  _fetch_blueground_destinations FAILED: {e}")
        import traceback
        traceback.print_exc()
        return

    # Step 3: normalize_properties
    print("\n--- Step 3: normalize_properties ---")
    normalized = normalize_properties(properties)
    print(f"  After normalize: {len(normalized)} properties")
    if normalized:
        for p in normalized[:3]:
            print(f"    - {p.get('name')!r}")

    # Step 4: Full collect_asset_snapshot (single entry point)
    print("\n--- Step 4: collect_asset_snapshot (full flow) ---")
    try:
        snapshot = collect_asset_snapshot(
            source_url,
            js_required=False,
            use_sitemap_first=False,
            extra_options={"strategy": "blueground_destinations"},
        )
        raw_count = len(snapshot.get("properties") or [])
        note = snapshot.get("note") or "unknown"
        print(f"  strategy used: {note}")
        print(f"  properties in snapshot: {raw_count}")
    except Exception as e:
        print(f"  collect_asset_snapshot FAILED: {e}")
        import traceback
        traceback.print_exc()
        return

    # Step 5: build_asset_structured
    print("\n--- Step 5: build_asset_structured ---")
    structured = build_asset_structured(snapshot)
    before_enrich = len(structured.get("properties") or [])
    print(f"  properties before enrich: {before_enrich}")

    # Step 6: enrich_properties_with_llm (optional, can be slow)
    print("\n--- Step 6: enrich_properties_with_llm ---")
    print("  (skipping to avoid API cost – set RUN_ENRICH=1 to enable)")
    if os.environ.get("RUN_ENRICH") == "1":
        structured["properties"] = enrich_properties_with_llm(
            structured.get("properties") or [],
            raw_content=snapshot.get("raw_content"),
        )
        after_enrich = len(structured.get("properties") or [])
        print(f"  properties after enrich: {after_enrich}")
    else:
        print("  (unchanged)")

    print("\n" + "=" * 60)
    print(f"SUMMARY: {before_enrich} properties collected from Blueground destinations")
    print("=" * 60)


if __name__ == "__main__":
    main()
