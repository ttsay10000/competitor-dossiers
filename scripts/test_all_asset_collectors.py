#!/usr/bin/env python3
"""
Run each competitor's asset collector with step-by-step diagnostics.
Shows what is pulled at each step (fetch, sitemap/blocks/links, pipeline, final)
so we can diagnose issues.

Run from repo root:
  python3 scripts/test_all_asset_collectors.py

Optional:
  PLAYWRIGHT_ENABLED=true   — use Playwright for Lark (and any js_required source)
  COMPETITOR=Placemakr      — run only this competitor (Placemakr | AvantStay | Lark)
Requires network. No DB required.
"""
import os
import sys
from urllib.parse import urlparse

sys.path.insert(0, ".")

from app.collectors.asset import (
    collect_asset_snapshot,
    discover_sitemap,
    expand_sitemap,
    extract_links_from_sitemap,
    is_property_like,
    _extract_lark_style_blocks,
    _extract_properties_via_llm_from_blocks,
    _lark_blocks_to_properties_without_llm,
    extract_properties_from_html,
    normalize_properties,
    _merge_link_properties_into,
)
from app.collectors.http import fetch_url, fetch_url_js_exhaust

# Same asset config as seed (no DB)
ASSET_SOURCES = [
    {
        "name": "Placemakr",
        "url": "https://www.placemakr.com/locations",
        "js_required": False,
        "use_sitemap_first": False,
        "extra_options": {"strategy_chain": ["html"], "min_properties_accept": 1},
    },
    {
        "name": "AvantStay",
        "url": "https://avantstay.com/search",
        "js_required": True,
        "use_sitemap_first": True,
        "extra_options": {
            "llm_extract": True,
            "strategy_chain": ["sitemap_first", "html"],
            "min_properties_accept": 5,
        },
    },
    {
        "name": "Lark",
        "url": "https://www.larkhospitality.com/portfolio/",
        "js_required": True,
        "use_sitemap_first": False,
        "extra_options": {
            "strategy": "js_exhaust",
            "load_more": {
                "click_selector": [
                    "button:has-text('Load more')",
                    "button:has-text('Load More')",
                    "a:has-text('Load more')",
                    "a:has-text('Load More')",
                    "button:has-text('View more')",
                    "a:has-text('View more')",
                    "[data-testid='load-more']",
                    "button:has-text('Show more')",
                    "a:has-text('Show more')",
                ],
                "stop_when_selector_gone": True,
                "wait_after_click_ms": 2000,
                "wait_for_selector_timeout_ms": 10000,
                "wait_after_gone_ms": 3000,
                "max_clicks": 200,
            },
            "llm_extract": True,
        },
    },
]


def _fetch_html(source: dict, use_playwright: bool) -> tuple[str, str]:
    """Return (html, method)."""
    url = source["url"]
    opts = source.get("extra_options") or {}
    if use_playwright and source.get("js_required") and opts.get("load_more"):
        try:
            result = fetch_url_js_exhaust(url, opts.get("load_more") or {})
            return result.text, "js_exhaust (Playwright + Load more)"
        except Exception as e:
            print(f"    Playwright fetch failed: {e!r}, falling back to plain HTTP")
    result = fetch_url(url)
    if result.status_code != 200:
        raise RuntimeError(f"HTTP {result.status_code} for {url}")
    return result.text, "plain HTTP"


def _run_placemakr(source: dict, use_playwright: bool) -> dict:
    """Step-by-step for Placemakr (HTML only)."""
    url = source["url"]
    print("  Step 1: Fetch HTML")
    html, method = _fetch_html(source, use_playwright)
    print(f"    Method: {method}, length: {len(html)} chars")

    print("  Step 2: Link extraction (extract_properties_from_html)")
    link_props = extract_properties_from_html(html)
    print(f"    Links count: {len(link_props)}")
    if link_props:
        for i, p in enumerate(link_props[:3]):
            print(f"      [{i}] name={p.get('name')!r} url={p.get('url')}")
        if len(link_props) > 3:
            print(f"      ... and {len(link_props) - 3} more")

    print("  Step 3: Full collect_asset_snapshot")
    try:
        snapshot = collect_asset_snapshot(
            url,
            js_required=source.get("js_required", False),
            use_sitemap_first=source.get("use_sitemap_first", False),
            extra_options=source.get("extra_options"),
        )
        props = snapshot.get("properties") or []
        note = snapshot.get("note", "")
        return {"ok": True, "count": len(props), "note": note, "snapshot": snapshot}
    except Exception as e:
        print(f"    ERROR: {e}")
        return {"ok": False, "count": 0, "note": str(e), "snapshot": None}


def _run_avantstay(source: dict, use_playwright: bool) -> dict:
    """Step-by-step for AvantStay (sitemap_first then HTML)."""
    url = source["url"]
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else url

    print("  Step 1a: Sitemap discovery (discover_sitemap)")
    root_urls = discover_sitemap(url)
    print(f"    Root sitemap URLs: {root_urls}")

    print("  Step 1b: Expand sitemap(s) and count property-like URLs")
    property_urls = set()
    visited = set()
    stack = list(root_urls)
    total_locs = 0
    while stack:
        sitemap_url = stack.pop()
        if sitemap_url in visited:
            continue
        visited.add(sitemap_url)
        try:
            fetched = fetch_url(sitemap_url)
            if fetched.status_code != 200:
                print(f"    {sitemap_url} -> HTTP {fetched.status_code}")
                continue
            xml_text = fetched.text
            if sitemap_url.endswith(".gz"):
                import gzip
                xml_text = gzip.decompress(fetched.text.encode("utf-8")).decode("utf-8")
            urls = extract_links_from_sitemap(xml_text)
            total_locs += len(urls)
            for u in urls:
                if not u:
                    continue
                if is_property_like(u):
                    property_urls.add(u)
                if (u.endswith(".xml") or u.endswith(".xml.gz")) and urlparse(u).netloc == parsed.netloc and u not in visited:
                    stack.append(u)
        except Exception as e:
            print(f"    Expand {sitemap_url}: {e!r}")
    print(f"    Total <loc> seen: {total_locs}, property-like: {len(property_urls)}")
    if property_urls and len(property_urls) <= 5:
        for u in sorted(property_urls)[:5]:
            print(f"      {u}")
    elif property_urls:
        for u in sorted(property_urls)[:3]:
            print(f"      {u}")
        print(f"      ... and {len(property_urls) - 3} more")

    print("  Step 2: Fetch HTML (for fallback / comparison)")
    html, method = _fetch_html(source, use_playwright)
    print(f"    Method: {method}, length: {len(html)} chars")

    print("  Step 3: Link extraction from HTML")
    link_props = extract_properties_from_html(html)
    print(f"    Links count: {len(link_props)}")

    print("  Step 4: Full collect_asset_snapshot")
    try:
        snapshot = collect_asset_snapshot(
            url,
            js_required=source.get("js_required", False),
            use_sitemap_first=source.get("use_sitemap_first", False),
            extra_options=source.get("extra_options"),
        )
        props = snapshot.get("properties") or []
        note = snapshot.get("note", "")
        return {"ok": True, "count": len(props), "note": note, "snapshot": snapshot, "sitemap_property_count": len(property_urls)}
    except Exception as e:
        print(f"    ERROR: {e}")
        return {"ok": False, "count": 0, "note": str(e), "snapshot": None, "sitemap_property_count": len(property_urls)}


def _run_lark(source: dict, use_playwright: bool) -> dict:
    """Step-by-step for Lark (blocks + optional Playwright)."""
    url = source["url"]
    print("  Step 1: Fetch HTML")
    html, method = _fetch_html(source, use_playwright)
    print(f"    Method: {method}, length: {len(html)} chars")

    parsed = urlparse(url)
    base_url = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else url

    print("  Step 2: Lark-style blocks (_extract_lark_style_blocks)")
    blocks = _extract_lark_style_blocks(html, base_url)
    print(f"    Blocks count: {len(blocks)}")
    if blocks:
        for i, b in enumerate(blocks[:3]):
            print(f"      [{i}] name={b.get('name')!r} location_line={b.get('location_line')!r} url={b.get('url')}")
        if len(blocks) > 3:
            print(f"      ... and {len(blocks) - 3} more")

    print("  Step 3: Pipeline on same HTML (blocks -> LLM/fallback -> merge -> normalize)")
    pipeline_count = 0
    if blocks:
        props_from_blocks = _extract_properties_via_llm_from_blocks(blocks, url)
        merged = _merge_link_properties_into(props_from_blocks, html, threshold=999)
        normalized = normalize_properties(merged)
        pipeline_count = len(normalized)
        print(f"    From blocks: {len(props_from_blocks)} -> after merge: {len(merged)} -> after normalize: {len(normalized)}")
        if pipeline_count == 0 and len(blocks) > 0:
            fallback = _lark_blocks_to_properties_without_llm(blocks)
            nf = normalize_properties(fallback)
            print(f"    (fallback without LLM: {len(fallback)} -> normalize -> {len(nf)})")

    print("  Step 4: Link extraction from HTML")
    link_props = extract_properties_from_html(html)
    print(f"    Links count: {len(link_props)} (portfolio index excluded)")

    print("  Step 5: Full collect_asset_snapshot")
    try:
        snapshot = collect_asset_snapshot(
            url,
            js_required=source.get("js_required", False),
            use_sitemap_first=source.get("use_sitemap_first", False),
            extra_options=source.get("extra_options"),
        )
        props = snapshot.get("properties") or []
        note = snapshot.get("note", "")
        return {"ok": True, "count": len(props), "note": note, "snapshot": snapshot, "blocks": len(blocks), "pipeline_on_same_html": pipeline_count}
    except Exception as e:
        print(f"    ERROR: {e}")
        return {"ok": False, "count": 0, "note": str(e), "snapshot": None, "blocks": len(blocks), "pipeline_on_same_html": pipeline_count}


def main() -> None:
    use_playwright = os.environ.get("PLAYWRIGHT_ENABLED", "").lower() in ("1", "true", "yes")
    competitor_filter = (os.environ.get("COMPETITOR") or "").strip()
    if competitor_filter:
        sources = [s for s in ASSET_SOURCES if s["name"].lower() == competitor_filter.lower()]
        if not sources:
            print(f"No competitor named {competitor_filter!r}. Use Placemakr, AvantStay, or Lark.")
            sys.exit(1)
    else:
        sources = ASSET_SOURCES

    print("Asset collectors diagnostic (step-by-step)")
    print("PLAYWRIGHT_ENABLED:", use_playwright)
    if competitor_filter:
        print("COMPETITOR:", competitor_filter)
    print()
    results = []
    for source in sources:
        name = source["name"]
        url = source["url"]
        print("=" * 60)
        print(f"  {name}")
        print(f"  URL: {url}")
        print("=" * 60)
        if name == "Placemakr":
            r = _run_placemakr(source, use_playwright)
        elif name == "AvantStay":
            r = _run_avantstay(source, use_playwright)
        else:
            r = _run_lark(source, use_playwright)
        results.append((name, r))
        print()
        # Summary line for this competitor
        if r.get("ok"):
            print(f"  >>> {name} final: {r['count']} properties (note: {r.get('note', '')})")
        else:
            print(f"  >>> {name} FAILED: {r.get('note', '')}")
        print()

    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    for name, r in results:
        c = r.get("count", 0)
        ok = r.get("ok", False)
        note = r.get("note", "")
        extra = []
        if "sitemap_property_count" in r:
            extra.append(f"sitemap property-like: {r['sitemap_property_count']}")
        if "blocks" in r:
            extra.append(f"blocks: {r['blocks']}")
        if "pipeline_on_same_html" in r:
            extra.append(f"pipeline on same HTML: {r['pipeline_on_same_html']}")
        line = f"  {name}: {c} properties (note={note})"
        if extra:
            line += " [" + ", ".join(extra) + "]"
        if not ok:
            line += " FAILED"
        print(line)
    print()
    print("Done. Use PLAYWRIGHT_ENABLED=true for Lark full list; COMPETITOR=Lark to run only Lark.")


if __name__ == "__main__":
    main()
