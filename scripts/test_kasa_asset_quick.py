#!/usr/bin/env python3
"""Quick Kasa parser test: fetch HTML with requests (no Playwright) and run Kasa extractor."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.collectors.asset import _extract_kasa_locations_html, _is_kasa_locations_url

URL = "https://kasa.com/locations"
BASE = "https://kasa.com"


def main() -> None:
    print("Quick Kasa parser test (requests only, no Playwright)")
    print("URL:", URL)
    assert _is_kasa_locations_url(URL), "URL should be detected as Kasa locations"
    import requests
    r = requests.get(URL, timeout=15, headers={"User-Agent": "Mozilla/5.0 (compatible; KasaTest/1.0)"})
    r.raise_for_status()
    html = r.text
    print(f"Fetched {len(html)} bytes HTML")
    props = _extract_kasa_locations_html(html, URL, BASE)
    print("Property count:", len(props))
    if props:
        print("\nFirst 5:")
        for p in props[:5]:
            print(f"  - {p.get('name')!r}  {p.get('market')!r}")
    else:
        print("(Parser returned 0 — page may be JS-rendered; run full test with PLAYWRIGHT_ENABLED=true)")


if __name__ == "__main__":
    main()
