#!/usr/bin/env python3
"""
Trace Avantstay asset fetch: follow sitemap discovery → expand → property-like filter,
then run collect_asset_snapshot and compare property count (expect ~2300).

Run from repo root:
  python3 scripts/test_avantstay_asset_fetch.py

Requires network (fetches avantstay.com). No DB required.
"""
import sys
from urllib.parse import urlparse

# Run from repo root so app is importable
sys.path.insert(0, ".")

from app.collectors.asset import (
    collect_asset_snapshot,
    discover_sitemap,
    expand_sitemap,
    is_property_like,
    extract_links_from_sitemap,
)
from app.collectors.http import fetch_url

AVANTSTAY_SOURCE_URL = "https://avantstay.com/search"
# Same as seed: sitemap_first + js_required + llm_extract (strategy becomes sitemap_first)
USE_SITEMAP_FIRST = True
JS_REQUIRED = True
EXTRA_OPTIONS = {"llm_extract": True}


def trace_sitemap_flow(source_url: str) -> None:
    """Reproduce fetch_from_sitemap logic with prints to see counts at each step."""
    print("=== 1. Sitemap discovery (discover_sitemap) ===")
    root_urls = discover_sitemap(source_url)
    for u in root_urls:
        print(f"  Try root: {u}")

    parsed = urlparse(source_url)
    origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else source_url

    visited = set()
    property_urls = set()
    stack = list(root_urls)
    sitemaps_expanded = 0
    total_locs_seen = 0
    property_like_by_sitemap = []

    print("\n=== 2. Expand sitemaps (expand_sitemap) and classify URLs ===")
    while stack:
        sitemap_url = stack.pop()
        if sitemap_url in visited:
            continue
        visited.add(sitemap_url)
        sitemaps_expanded += 1

        fetched = fetch_url(sitemap_url)
        if fetched.status_code != 200:
            print(f"  [{sitemap_url}] HTTP {fetched.status_code} -> skip")
            continue

        if sitemap_url.endswith(".gz"):
            import gzip
            try:
                xml_text = gzip.decompress(fetched.text.encode("utf-8")).decode("utf-8")
            except Exception as e:
                print(f"  [{sitemap_url}] gzip error: {e} -> skip")
                continue
        else:
            xml_text = fetched.text

        urls = extract_links_from_sitemap(xml_text)
        total_locs_seen += len(urls)

        child_sitemaps = []
        property_count_this = 0
        for url in urls:
            if not url:
                continue
            if is_property_like(url):
                property_urls.add(url)
                property_count_this += 1
                continue
            if url.endswith(".xml") or url.endswith(".xml.gz"):
                p = urlparse(url)
                if p.netloc and p.netloc == urlparse(origin).netloc and url not in visited:
                    child_sitemaps.append(url)

        property_like_by_sitemap.append((sitemap_url, len(urls), property_count_this, len(child_sitemaps)))
        for u in child_sitemaps:
            if u not in visited:
                stack.append(u)

    for sm_url, num_locs, num_prop, num_children in property_like_by_sitemap:
        print(f"  {sm_url}")
        print(f"    -> locs: {num_locs}, property-like: {num_prop}, child sitemaps: {num_children}")

    print(f"\n  Total sitemaps expanded: {sitemaps_expanded}")
    print(f"  Total <loc> URLs seen: {total_locs_seen}")
    print(f"  Total property-like URLs collected: {len(property_urls)}")

    if property_urls:
        sample = sorted(property_urls)[:3]
        print(f"  Sample property URLs: {sample}")
    return len(property_urls)


def main() -> None:
    print("Avantstay asset fetch trace")
    print("Source URL:", AVANTSTAY_SOURCE_URL)
    print("Config: use_sitemap_first=%s, js_required=%s, extra_options=%s" % (
        USE_SITEMAP_FIRST, JS_REQUIRED, EXTRA_OPTIONS,
    ))
    print()

    # Step 1: trace sitemap flow (same logic as fetch_from_sitemap)
    traced_count = trace_sitemap_flow(AVANTSTAY_SOURCE_URL)

    # Step 2: run full collect_asset_snapshot (strategy = sitemap_first)
    print("\n=== 3. collect_asset_snapshot (full flow) ===")
    try:
        snapshot = collect_asset_snapshot(
            AVANTSTAY_SOURCE_URL,
            js_required=JS_REQUIRED,
            use_sitemap_first=USE_SITEMAP_FIRST,
            extra_options=EXTRA_OPTIONS,
        )
    except Exception as e:
        print(f"  ERROR: {e}")
        raise

    note = snapshot.get("note")
    properties = snapshot.get("properties") or []
    count = len(properties)

    print(f"  note: {note}")
    print(f"  properties count: {count}")

    # Compare
    print("\n=== 4. Comparison ===")
    print(f"  Traced sitemap property-like count: {traced_count}")
    print(f"  collect_asset_snapshot properties count: {count}")
    if traced_count != count:
        print(f"  -> MISMATCH (expected same when strategy is sitemap_first)")
    else:
        print(f"  -> Match OK")

    target = 2300
    print(f"\n  Expected ballpark (Avantstay): ~{target} properties")
    if count >= target * 0.8:
        print(f"  -> PASS: count {count} is in expected range")
    elif count > 0:
        print(f"  -> LOW: count {count} is below expected ~{target}")
    else:
        print(f"  -> FAIL: no properties returned")

    if properties:
        print(f"\n  First 3 properties: {[p.get('url') or p.get('name') for p in properties[:3]]}")


if __name__ == "__main__":
    main()
