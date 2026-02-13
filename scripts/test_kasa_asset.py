#!/usr/bin/env python3
"""Run a one-off Kasa Living asset pull. Expect ~77-90 properties; footer locations are excluded."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("PLAYWRIGHT_ENABLED", "true")

from app.collectors.asset import collect_asset_snapshot, normalize_properties

KASA_OPTS = {
    "strategy": "js_exhaust",
    "load_more": {
        "button_text": "Load more",
        "post_load_wait_ms": 3000,
        "click_selector": [
            "a:has-text('Load more')",
            "button:has-text('Load more')",
            ":text('Load more')",
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
        "wait_reappear_attempts": 5,
        "max_clicks": 200,
    },
    "llm_extract": True,
}


def main() -> None:
    url = "https://kasa.com/locations"
    print("Kasa Living asset pull test")
    print("URL:", url)
    print("Expected property count: 77–90 (footer locations excluded)")
    print()

    try:
        snapshot = collect_asset_snapshot(
            url,
            js_required=True,
            use_sitemap_first=False,
            extra_options=KASA_OPTS,
        )
    except Exception as e:
        print("FAILED:", e)
        import traceback
        traceback.print_exc()
        sys.exit(1)

    props = snapshot.get("properties") or []
    note = snapshot.get("note") or "unknown"
    print("Strategy/note:", note)
    print("Property count:", len(props))

    if len(props) > 90:
        print("\n⚠️  Count is above 90 — possible footer locations still included.")
    elif 77 <= len(props) <= 90:
        print("\n✓ Count in expected range 77–90.")
    elif len(props) < 77:
        print("\n⚠️  Count below 77 — some properties may be missing.")

    if props:
        print("\nFirst 10 properties:")
        for p in props[:10]:
            print(f"  - {p.get('name')!r}  {p.get('url') or p.get('market') or ''}")
        if len(props) > 10:
            print("  ...")
            print("\nLast 3 properties:")
            for p in props[-3:]:
                print(f"  - {p.get('name')!r}  {p.get('url') or p.get('market') or ''}")


if __name__ == "__main__":
    main()
