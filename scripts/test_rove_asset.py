#!/usr/bin/env python3
"""Test Rove asset flow: sitemap fetch + collect_asset_snapshot."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.collectors.asset import (
    discover_sitemap,
    expand_sitemap,
    is_property_like,
    collect_asset_snapshot,
)
from app.collectors.asset import _is_rove_property_url  # noqa: F401

SOURCE_URL = "https://rovetravel.com/search"


def main() -> None:
    print("=" * 60)
    print("1. Sitemap fetch")
    print("=" * 60)
    for sitemap_url in discover_sitemap(SOURCE_URL):
        print(f"  Fetching: {sitemap_url}")
        urls = expand_sitemap(sitemap_url)
        print(f"  Total URLs in sitemap: {len(urls)}")
        listing_urls = [
            u for u in urls
            if is_property_like(u) and _is_rove_property_url(u, SOURCE_URL)
        ]
        print(f"  Rove /listing/ URLs (property-like + _is_rove_property_url): {len(listing_urls)}")
        if listing_urls:
            print("  Sample (first 10):")
            for u in listing_urls[:10]:
                print(f"    {u}")
        if urls:
            break

    print("\n" + "=" * 60)
    print("2. collect_asset_snapshot (full flow)")
    print("=" * 60)
    snapshot = collect_asset_snapshot(SOURCE_URL, extra_options={"strategy_chain": ["sitemap_first", "html"]})
    props = snapshot.get("properties") or []
    note = snapshot.get("note", "")
    print(f"  Note: {note}")
    print(f"  Properties: {len(props)}")
    if props:
        print("  Sample (first 5):")
        for p in props[:5]:
            print(f"    {p.get('url', p.get('name', ''))}")


if __name__ == "__main__":
    main()
