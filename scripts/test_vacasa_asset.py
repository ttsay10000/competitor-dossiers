#!/usr/bin/env python3
"""
Test Vacasa asset collection: HTML-only vs sitemap_first.
No DB required. Run: python scripts/test_vacasa_asset.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.collectors.asset import (
    collect_asset_snapshot,
    discover_sitemap,
    expand_sitemap,
    is_property_like,
)

VACASA_URL = "https://www.vacasa.com/search?place=/usa/"


def _check_sitemap():
    """See what Vacasa sitemap returns and how many pass is_property_like."""
    print("=" * 60)
    print("VACASA SITEMAP CHECK (why sitemap_first might get 0)")
    print("=" * 60)
    for sm_url in discover_sitemap(VACASA_URL):
        urls = expand_sitemap(sm_url)
        print(f"  {sm_url}: {len(urls)} <loc> URLs")
        if urls:
            unit_like = [u for u in urls if "/unit/" in u and is_property_like(u)]
            print(f"    -> with /unit/ and is_property_like: {len(unit_like)}")
            if unit_like:
                for u in unit_like[:5]:
                    print(f"       {u}")
            # show sample of raw URLs
            for u in urls[:5]:
                print(f"       raw: {u}")
            if len(urls) > 5:
                print(f"       ... and {len(urls) - 5} more")
        print()
    print()


def main():
    _check_sitemap()

    print("=" * 60)
    print("VACASA ASSET — HTML only (old config)")
    print("=" * 60)
    snapshot_html = collect_asset_snapshot(
        VACASA_URL,
        js_required=False,
        use_sitemap_first=False,
        extra_options={"strategy_chain": ["html"], "min_properties_accept": 1},
    )
    n_html = len(snapshot_html.get("properties") or [])
    note_html = snapshot_html.get("note", "?")
    print(f"  Strategy used: {note_html}")
    print(f"  Properties: {n_html}")
    if n_html <= 3 and (snapshot_html.get("properties") or []):
        for p in (snapshot_html.get("properties") or [])[:5]:
            print(f"    - {p.get('url')} | {p.get('name') or '(no name)'}")
    print()

    print("=" * 60)
    print("VACASA ASSET — Sitemap first, then HTML (new config)")
    print("=" * 60)
    snapshot_sitemap = collect_asset_snapshot(
        VACASA_URL,
        js_required=False,
        use_sitemap_first=True,
        extra_options={"strategy_chain": ["sitemap_first", "html"], "min_properties_accept": 5},
    )
    n_sitemap = len(snapshot_sitemap.get("properties") or [])
    note_sitemap = snapshot_sitemap.get("note", "?")
    print(f"  Strategy used: {note_sitemap}")
    print(f"  Properties: {n_sitemap}")
    if n_sitemap > 0:
        for p in (snapshot_sitemap.get("properties") or [])[:3]:
            print(f"    - {p.get('url')} | {p.get('name') or '(no name)'}")
        if n_sitemap > 3:
            print(f"    ... and {n_sitemap - 3} more")
    print()
    print("Done.")


if __name__ == "__main__":
    main()
