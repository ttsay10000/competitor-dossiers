#!/usr/bin/env python3
"""
Run asset collection for competitor AKA and print raw property data BEFORE LLM enrichment.
Shows exactly what is fed into enrich_properties_with_llm (state/classification).

Usage:
  # From DB (requires DB + AKA competitor with asset endpoint):
  python scripts/inspect_aka_asset_raw.py
  python scripts/inspect_aka_asset_raw.py --competitor AKA

  # With explicit URL (no DB needed):
  python scripts/inspect_aka_asset_raw.py --url "https://example.com/locations" [--js] [--sitemap-first]
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.collectors.asset import collect_asset_snapshot, build_structured_json as build_asset_structured


def _endpoints_ordered(endpoints):
    return sorted(endpoints, key=lambda e: e.id)


def run_with_url(url: str, js_required: bool = False, use_sitemap_first: bool = False, extra_options: dict = None) -> None:
    """Run collector with explicit URL (no DB)."""
    print("=" * 60)
    print("ASSET — RAW DATA (before LLM enrichment)")
    print("=" * 60)
    print(f"\nAsset URL:  {url}")
    print(f"Options:    js_required={js_required}, use_sitemap_first={use_sitemap_first}")
    print(f"            extra_options={extra_options or {}}")
    snapshot = collect_asset_snapshot(
        url,
        js_required=js_required,
        use_sitemap_first=use_sitemap_first,
        extra_options=extra_options or {},
    )
    structured = build_asset_structured(snapshot)
    _print_raw_properties(structured, snapshot.get("note"), len(snapshot.get("raw_content") or ""))


def run_with_db(competitor_name: str) -> None:
    """Load competitor from DB and run first asset endpoint."""
    from sqlalchemy.orm import selectinload
    from app.db import get_session
    from app.models import Competitor

    print("=" * 60)
    print(f"{competitor_name.upper()} ASSET — RAW DATA (before LLM enrichment)")
    print("=" * 60)

    with get_session() as session:
        competitors = (
            session.query(Competitor)
            .options(selectinload(Competitor.source_endpoints))
            .order_by(Competitor.name.asc())
            .all()
        )
        want = competitor_name.strip().lower()
        matches = [c for c in competitors if (c.name or "").strip().lower() == want
                   or ((c.name or "").strip().lower().split() and (c.name or "").strip().lower().split()[0] == want)]
        if not matches:
            print(f"\nNo competitor found matching {competitor_name!r}. Existing names:")
            for c in competitors:
                print(f"  - {c.name}")
            return
        competitor = matches[0]
        endpoints = _endpoints_ordered([
            e for e in (competitor.source_endpoints or []) if e.channel == "asset"
        ])
        if not endpoints:
            print(f"\nNo asset endpoint for {competitor.name}.")
            return
        endpoint = endpoints[0]
        url = endpoint.url
        opts = getattr(endpoint, "extra_options", None) or {}
        js_required = getattr(endpoint, "js_required", False)
        use_sitemap_first = getattr(endpoint, "use_sitemap_first", False)
        print(f"\nCompetitor: {competitor.name}")
        print(f"Asset URL:  {url}")
        print(f"Options:    js_required={js_required}, use_sitemap_first={use_sitemap_first}")
        print(f"            extra_options={opts}")

    snapshot = collect_asset_snapshot(url, js_required=js_required, use_sitemap_first=use_sitemap_first, extra_options=opts)
    structured = build_asset_structured(snapshot)
    _print_raw_properties(structured, snapshot.get("note"), len(snapshot.get("raw_content") or ""))


def _print_raw_properties(structured: dict, note: str, raw_content_len: int) -> None:
    """Print raw property summary and samples."""
    props = structured.get("properties") or []
    print("\n--- Collect + build_asset_structured (BEFORE enrich_properties_with_llm) ---")
    print(f"  Strategy: {note}")
    print(f"  Properties count: {len(props)}")
    print(f"  raw_content length: {raw_content_len} chars")

    if not props:
        print("  No properties to show.")
        return

    keys_seen = set()
    for p in props:
        keys_seen.update(k for k, v in p.items() if v is not None and v != "")
    print(f"  Keys present across props: {sorted(keys_seen)}")

    state_counts = {}
    market_samples = {}
    url_prefixes = {}
    for p in props:
        s = (p.get("state") or "").strip() or "(empty)"
        state_counts[s] = state_counts.get(s, 0) + 1
        m = (p.get("market") or "").strip() or "(empty)"
        market_samples[m] = market_samples.get(m, 0) + 1
        u = (p.get("url") or "").strip()
        if u:
            path = u.split("?", 1)[0].rstrip("/")
            prefix = path[:60] + "..." if len(path) > 60 else path
            url_prefixes[prefix] = url_prefixes.get(prefix, 0) + 1

    print("\n  State distribution (raw, before LLM):")
    for s, n in sorted(state_counts.items(), key=lambda x: (-x[1], x[0])):
        print(f"    {s!r}: {n}")

    print("\n  Market/location distribution (top 20):")
    for m, n in sorted(market_samples.items(), key=lambda x: (-x[1], x[0]))[:20]:
        label = (m[:50] + "...") if len(m) > 50 else m
        print(f"    {label!r}: {n}")

    print("\n  Sample URL path patterns (first 10 by frequency):")
    for i, prefix in enumerate(sorted(url_prefixes.keys(), key=lambda x: -url_prefixes[x])[:10]):
        print(f"    [{url_prefixes[prefix]}x] {prefix}")

    print("\n--- First 15 properties (raw dict, before LLM) ---")
    for i, p in enumerate(props[:15]):
        copy = {k: (v[:120] + "..." if isinstance(v, str) and len(v) > 120 else v) for k, v in p.items()}
        print(f"\n  [{i}] {json.dumps(copy, default=str)}")
    print("\n--- Done ---")


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect raw asset data before LLM (AKA or URL)")
    parser.add_argument("--competitor", "-c", default="AKA", help="Competitor name (default: AKA)")
    parser.add_argument("--url", "-u", help="Asset URL (if set, skip DB and use this URL)")
    parser.add_argument("--js", action="store_true", help="js_required (when using --url)")
    parser.add_argument("--sitemap-first", action="store_true", help="use_sitemap_first (when using --url)")
    parser.add_argument("--options", type=str, help="JSON object for extra_options (when using --url)")
    args = parser.parse_args()

    if args.url:
        opts = {}
        if args.options:
            try:
                opts = json.loads(args.options)
            except json.JSONDecodeError:
                print("Invalid --options JSON", file=sys.stderr)
                sys.exit(1)
        run_with_url(args.url, js_required=args.js, use_sitemap_first=args.sitemap_first, extra_options=opts)
    else:
        run_with_db(args.competitor)


if __name__ == "__main__":
    main()
