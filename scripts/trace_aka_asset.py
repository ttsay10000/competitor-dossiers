#!/usr/bin/env python3
"""
Trace AKA asset collection step-by-step to find why 0 properties are returned
when ~22 were previously scraped.

Shows:
- Which strategy/chain is used
- Sitemap: URLs found, how many pass is_property_like
- HTML: links found, how many pass is_property_like / junk filter / normalize
- Lark-style blocks (if any)
- Final collect_asset_snapshot result

Usage:
  .venv/bin/python scripts/trace_aka_asset.py                    # from DB (competitor AKA)
  .venv/bin/python scripts/trace_aka_asset.py --url "https://..." [--js] [--sitemap-first]
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Import collector internals for step-by-step tracing
from app.collectors.asset import (
    discover_sitemap,
    expand_sitemap,
    is_property_like,
    extract_properties_from_html,
    normalize_properties,
    _extract_lark_style_blocks,
    collect_asset_snapshot,
    build_structured_json,
)
from app.collectors.http import fetch_url, fetch_url_js

_DEFAULT_ASSET_STRATEGY_CHAIN = ["sitemap_first", "js_exhaust", "html"]


def trace_with_url(url: str, js_required: bool = False, use_sitemap_first: bool = False, extra_options: dict = None) -> None:
    opts = extra_options or {}
    chain = opts.get("strategy_chain")
    if opts.get("strategy") is not None:
        chain = None
    if chain is None and opts.get("strategy") is None:
        chain = _DEFAULT_ASSET_STRATEGY_CHAIN

    print("=" * 70)
    print("AKA ASSET TRACE — where do the 22 properties go?")
    print("=" * 70)
    print(f"\nURL: {url}")
    print(f"js_required={js_required}, use_sitemap_first={use_sitemap_first}")
    print(f"extra_options: {json.dumps(opts, default=str)}")
    print(f"Effective strategy: single={opts.get('strategy')!r}, chain={chain}")

    # --- Step 1: Sitemap ---
    print("\n--- 1. SITEMAP ---")
    sitemap_urls = discover_sitemap(url)
    print(f"  discover_sitemap: {sitemap_urls}")
    all_sitemap_links = []
    for sm_url in sitemap_urls:
        expanded = expand_sitemap(sm_url)
        print(f"  expand_sitemap({sm_url}): {len(expanded)} URLs")
        for u in expanded[:500]:  # cap for display
            if u and u not in all_sitemap_links:
                all_sitemap_links.append(u)
        if len(expanded) > 500:
            print(f"  (only counting first 500 of {len(expanded)} for property-like check)")
    property_like_from_sitemap = [u for u in all_sitemap_links if is_property_like(u)]
    print(f"  Total sitemap URLs (capped): {len(all_sitemap_links)}")
    print(f"  URLs passing is_property_like: {len(property_like_from_sitemap)}")
    if property_like_from_sitemap:
        for u in property_like_from_sitemap[:5]:
            print(f"    - {u[:80]}...")
    elif all_sitemap_links:
        print("  Sample URLs that did NOT pass is_property_like:")
        for u in all_sitemap_links[:5]:
            print(f"    - {u[:80]}...")

    # --- Step 2: HTML fetch ---
    print("\n--- 2. HTML FETCH ---")
    try:
        if js_required:
            fetched = fetch_url_js(url)
        else:
            fetched = fetch_url(url)
        html = fetched.text or ""
        status = getattr(fetched, "status_code", None)
        print(f"  status_code={status}, body length={len(html)} chars")
    except Exception as e:
        print(f"  FETCH FAILED: {e}")
        import traceback
        traceback.print_exc()
        return

    # --- Step 3: Links in HTML ---
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    all_links = soup.find_all("a")
    with_href = [a for a in all_links if a.get("href") and len((a.get_text() or "").strip()) >= 3]
    property_like_links = [a for a in with_href if is_property_like((a.get("href") or ""))]
    print(f"  <a> total: {len(all_links)}")
    print(f"  <a> with href and text>=3: {len(with_href)}")
    print(f"  of those passing is_property_like(href): {len(property_like_links)}")
    if with_href and not property_like_links:
        print("  Sample hrefs that did NOT pass is_property_like:")
        seen = set()
        for a in with_href[:30]:
            h = (a.get("href") or "").strip()
            if h and h not in seen:
                seen.add(h)
                print(f"    - {h[:90]}")
            if len(seen) >= 5:
                break

    # --- Step 4: extract_properties_from_html (includes junk filter) ---
    print("\n--- 3. EXTRACT_PROPERTIES_FROM_HTML ---")
    from app.collectors.asset import _is_junk_property_link
    after_junk = []
    for a in property_like_links:
        href = (a.get("href") or "").strip()
        text = (a.get_text() or "").strip()
        if _is_junk_property_link(text, href):
            continue
        after_junk.append((href, text))
    raw_props = extract_properties_from_html(html, url)
    print(f"  After junk filter (manual count): {len(after_junk)}")
    print(f"  extract_properties_from_html result: {len(raw_props)}")

    # --- Step 5: normalize_properties ---
    print("\n--- 4. NORMALIZE_PROPERTIES ---")
    normalized = normalize_properties(raw_props)
    print(f"  Before normalize: {len(raw_props)}")
    print(f"  After normalize: {len(normalized)}")
    if raw_props and not normalized:
        print("  Names that might be filtered (first 5):")
        for p in raw_props[:5]:
            print(f"    name={p.get('name')!r} url={p.get('url')!r}")

    # --- Step 6: Lark-style blocks (if any) ---
    from urllib.parse import urlparse
    parsed = urlparse(url)
    base_url = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else url
    lark_blocks = _extract_lark_style_blocks(html, base_url)
    print(f"\n--- 5. LARK-STYLE BLOCKS ---")
    print(f"  _extract_lark_style_blocks: {len(lark_blocks)} blocks")
    if lark_blocks:
        for i, b in enumerate(lark_blocks[:3]):
            print(f"    [{i}] name={b.get('name')!r} location_line={b.get('location_line')!r} url={b.get('url')!r}")

    # --- Step 7: Full collect_asset_snapshot ---
    print("\n--- 6. FULL collect_asset_snapshot ---")
    try:
        snapshot = collect_asset_snapshot(
            url,
            js_required=js_required,
            use_sitemap_first=use_sitemap_first,
            extra_options=opts,
        )
        props = snapshot.get("properties") or []
        note = snapshot.get("note") or "?"
        print(f"  note: {note}")
        print(f"  properties count: {len(props)}")
        if props:
            for i, p in enumerate(props[:5]):
                print(f"    [{i}] {p.get('name')!r} | {p.get('url')!r}")
    except Exception as e:
        print(f"  FAILED: {e}")
        import traceback
        traceback.print_exc()

    print("\n" + "=" * 70)


def run_with_db(competitor_name: str) -> None:
    from sqlalchemy.orm import selectinload
    from app.db import get_session
    from app.models import Competitor

    with get_session() as session:
        competitors = (
            session.query(Competitor)
            .options(selectinload(Competitor.source_endpoints))
            .order_by(Competitor.name.asc())
            .all()
        )
        want = competitor_name.strip().lower()
        matches = [
            c for c in competitors
            if (c.name or "").strip().lower() == want
            or ((c.name or "").strip().lower().split() and (c.name or "").strip().lower().split()[0] == want)
        ]
        if not matches:
            print(f"No competitor named {competitor_name!r}. Names: {[c.name for c in competitors]}")
            return
        comp = matches[0]
        endpoints = sorted([e for e in (comp.source_endpoints or []) if e.channel == "asset"], key=lambda e: e.id)
        if not endpoints:
            print(f"No asset endpoint for {comp.name}")
            return
        ep = endpoints[0]
        url = ep.url
        opts = getattr(ep, "extra_options", None) or {}
        js_required = getattr(ep, "js_required", False)
        use_sitemap_first = getattr(ep, "use_sitemap_first", False)
    trace_with_url(url, js_required=js_required, use_sitemap_first=use_sitemap_first, extra_options=opts)


def main():
    parser = argparse.ArgumentParser(description="Trace AKA asset collection to find why 0 properties")
    parser.add_argument("--competitor", "-c", default="AKA", help="Competitor name")
    parser.add_argument("--url", "-u", help="Asset URL (skip DB)")
    parser.add_argument("--js", action="store_true", help="js_required")
    parser.add_argument("--sitemap-first", action="store_true", help="use_sitemap_first")
    parser.add_argument("--options", type=str, help="JSON extra_options when using --url")
    args = parser.parse_args()
    if args.url:
        opts = json.loads(args.options) if args.options else {}
        trace_with_url(args.url, js_required=args.js, use_sitemap_first=args.sitemap_first, extra_options=opts)
    else:
        run_with_db(args.competitor)


if __name__ == "__main__":
    main()
