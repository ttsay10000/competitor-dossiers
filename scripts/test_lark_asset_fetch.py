#!/usr/bin/env python3
"""
Test Lark portfolio asset fetch: fetch larkhospitality.com/portfolio/ (with optional
Playwright for Load more), run block extraction and link extraction, then full
collect_asset_snapshot. Shows what is being pulled out.

Run from repo root:
  python3 scripts/test_lark_asset_fetch.py

Optional: PLAYWRIGHT_ENABLED=true to use Playwright + Load more (full ~69 properties).
Requires network. No DB required.
"""
import os
import sys
from urllib.parse import urlparse

sys.path.insert(0, ".")

from app.collectors.asset import (
    collect_asset_snapshot,
    _extract_lark_style_blocks,
    _extract_properties_via_llm_from_blocks,
    _lark_blocks_to_properties_without_llm,
    extract_properties_from_html,
    normalize_properties,
    _merge_link_properties_into,
)
from app.collectors.http import fetch_url, fetch_url_js_exhaust

LARK_PORTFOLIO_URL = "https://www.larkhospitality.com/portfolio/"
LARK_EXTRA_OPTIONS = {
    "strategy": "js_exhaust",
    "load_more": {
        "button_text": "Load more hotels",
        "post_load_wait_ms": 3000,
        "click_selector": [
            "a:has-text('Load more hotels')",
            "button:has-text('Load more hotels')",
            ":text('Load more hotels')",
            "button:has-text('Load more')",
            "button:has-text('Load More')",
            "a:has-text('Load more')",
            "a:has-text('Load More')",
            "button:has-text('View more')",
            "a:has-text('View more')",
        ],
        "stop_when_selector_gone": True,
        "wait_after_click_ms": 2000,
        "wait_for_selector_timeout_ms": 10000,
        "wait_after_gone_ms": 3000,
        "wait_reappear_attempts": 5,
        "max_clicks": 200,
    },
    "llm_extract": True,
}


def fetch_html(use_playwright: bool) -> tuple[str, str]:
    """Return (html, method)."""
    if use_playwright:
        try:
            load_more = LARK_EXTRA_OPTIONS.get("load_more") or {}
            result = fetch_url_js_exhaust(LARK_PORTFOLIO_URL, load_more)
            return result.text, "js_exhaust (Playwright + Load more)"
        except Exception as e:
            print(f"  Playwright fetch failed: {e}")
            print("  Falling back to plain HTTP...")
    result = fetch_url(LARK_PORTFOLIO_URL)
    if result.status_code != 200:
        raise RuntimeError(f"HTTP {result.status_code} for {LARK_PORTFOLIO_URL}")
    return result.text, "plain HTTP (no JS)"


def main() -> None:
    use_playwright = os.environ.get("PLAYWRIGHT_ENABLED", "").lower() in ("1", "true", "yes")
    print("Lark portfolio asset fetch test")
    print("URL:", LARK_PORTFOLIO_URL)
    print("Playwright:", use_playwright)
    print()

    # 1) Fetch HTML
    print("=== 1. Fetch HTML ===")
    html, method = fetch_html(use_playwright)
    print(f"  Method: {method}")
    print(f"  HTML length: {len(html)} chars")
    if os.environ.get("SAVE_HTML"):
        path = "lark_portfolio_snapshot.html"
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"  Saved to {path} (SAVE_HTML=1)")
    print()

    parsed = urlparse(LARK_PORTFOLIO_URL)
    base_url = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else LARK_PORTFOLIO_URL

    # 2) Lark-style blocks
    print("=== 2. Lark-style blocks (_extract_lark_style_blocks) ===")
    blocks = _extract_lark_style_blocks(html, base_url)
    print(f"  Blocks count: {len(blocks)}")
    if blocks:
        for i, b in enumerate(blocks[:5]):
            print(f"    [{i}] name={b.get('name')!r} location_line={b.get('location_line')!r} url={b.get('url')}")
        if len(blocks) > 5:
            print(f"    ... and {len(blocks) - 5} more")
    else:
        print("  (no h2/ul blocks found — DOM may differ or content not in HTML)")
    print()

    # 2b) Pipeline on same HTML (blocks -> properties -> normalize) to see if we lose count here
    print("=== 2b. Pipeline on same HTML (blocks -> LLM/fallback -> merge -> normalize) ===")
    pipeline_count = 0
    if blocks:
        props_from_blocks = _extract_properties_via_llm_from_blocks(blocks, LARK_PORTFOLIO_URL)
        merged = _merge_link_properties_into(props_from_blocks, html, threshold=999)
        normalized = normalize_properties(merged)
        pipeline_count = len(normalized)
        print(f"  From {len(blocks)} blocks -> {len(props_from_blocks)} from LLM/fallback -> {len(merged)} after merge -> {len(normalized)} after normalize")
        if pipeline_count == 0 and len(blocks) > 0:
            fallback = _lark_blocks_to_properties_without_llm(blocks)
            norm_fallback = normalize_properties(fallback)
            print(f"  (fallback without LLM: {len(fallback)} -> normalize -> {len(norm_fallback)})")
    else:
        print("  (no blocks, skip)")
    print()

    # 3) Link extraction
    print("=== 3. Link extraction (extract_properties_from_html) ===")
    link_props = extract_properties_from_html(html)
    print(f"  Link-based properties count: {len(link_props)}")
    if link_props:
        for i, p in enumerate(link_props[:5]):
            print(f"    [{i}] name={p.get('name')!r} url={p.get('url')}")
        if len(link_props) > 5:
            print(f"    ... and {len(link_props) - 5} more")
    print()

    # 4) Full collect_asset_snapshot (may use Playwright if enabled)
    print("=== 4. collect_asset_snapshot (full pipeline) ===")
    snapshot = None
    try:
        snapshot = collect_asset_snapshot(
            LARK_PORTFOLIO_URL,
            js_required=True,
            use_sitemap_first=False,
            extra_options=LARK_EXTRA_OPTIONS,
        )
    except Exception as e:
        print(f"  ERROR: {e}")
        import traceback
        traceback.print_exc()
        print("  (Steps 1–3 above used the initial fetch; full pipeline failed.)")

    count = 0
    if snapshot:
        note = snapshot.get("note")
        properties = snapshot.get("properties") or []
        count = len(properties)
        print(f"  note: {note}")
        print(f"  properties count: {count}")
        if properties:
            print("  First 5:")
            for i, p in enumerate(properties[:5]):
                print(f"    [{i}] name={p.get('name')!r} state={p.get('state')} city={p.get('city')} url={p.get('url')}")
            if count > 5:
                print(f"  ... and {count - 5} more")
    print()

    # Summary
    print("=== Summary ===")
    print(f"  Blocks: {len(blocks)} | Pipeline on same HTML: {pipeline_count} | Step 4 (full fetch): {count} | Links: {len(link_props)}")
    if count >= 60:
        print("  -> Expected ballpark ~69: OK")
    elif pipeline_count >= 60 and count == 0:
        print("  -> Pipeline on same HTML has ~69 but step 4 got 0: step 4 uses a second fetch (likely no Playwright or timeout).")
    elif pipeline_count == 0 and len(blocks) > 0:
        print("  -> Blocks present but pipeline on same HTML returned 0: check normalize_properties or LLM/fallback.")
    elif count > 0:
        print("  -> Fewer than expected; try PLAYWRIGHT_ENABLED=true for full list")
    elif len(blocks) <= 10 and not use_playwright:
        print("  -> Plain HTTP only gets first batch (~6 in initial HTML); use PLAYWRIGHT_ENABLED=true + playwright install to get ~69")
    else:
        print("  -> No properties; check DOM or run with Playwright")


if __name__ == "__main__":
    main()
