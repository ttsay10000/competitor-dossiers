#!/usr/bin/env python3
"""
Test script: run the full press pipeline (user endpoints + Google News 90d + PR Newswire 90d)
for each competitor and print the final canonical news links that would be displayed.

Usage:
  python scripts/test_press_final_output.py [competitor_name]
  python scripts/test_press_final_output.py --no-enrich [competitor_name]
  python scripts/test_press_final_output.py --local

  If competitor_name is omitted, runs for all competitors from DB.
  --no-enrich: skip LLM enrichment; print raw/filtered links only (fast, no API cost).
  --local: no DB; run Google News + PR Newswire only for hardcoded names (Lark Hotels, AvantStay, Placemakr). No enrichment.

Requires: .env with DATABASE_URL (unless --local); OPENAI_API_KEY for enrichment (unless --no-enrich or --local). No snapshot is persisted.
"""
import os
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Load .env
env_file = ROOT / ".env"
if env_file.exists():
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _parse_press_date(value) -> object:
    if value is None:
        return None
    if hasattr(value, "year"):  # datetime
        return value
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc)
        except Exception:
            return None
    if isinstance(value, str):
        val = value.strip()
        if not val:
            return None
        try:
            return datetime.fromisoformat(val.replace("Z", "+00:00"))
        except Exception:
            return None
    return None


def _normalize_domain(host: str) -> str:
    if not host:
        return ""
    h = (host or "").lower().strip()
    return h[4:] if h.startswith("www.") else h


def run_local_no_db():
    """Run Google News + PR Newswire only for hardcoded competitors; no DB, no enrichment."""
    from app.config import settings
    from app.collectors.global_press import collect_google_news_items, collect_prnewswire_items

    window_days = 90
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    competitors = [
        ("Lark Hotels", "Lark Hotels"),
        ("AvantStay", "AvantStay"),
        ("Placemakr", "Placemakr"),
    ]

    for name, press_search_name in competitors:
        print("\n" + "=" * 80)
        print(f"COMPETITOR: {name}")
        print(f"  Press search name: {press_search_name!r}")
        print("=" * 80)

        raw_items = []
        if getattr(settings, "press_enable_google_news", True):
            try:
                gn_items = collect_google_news_items(
                    press_search_name,
                    max_items=min(50, 60),
                    window_days=window_days,
                )
                raw_items.extend(gn_items)
                print(f"  Google News: {len(gn_items)} items")
            except Exception as e:
                print(f"  Google News failed: {e}")

        try:
            prn_items = collect_prnewswire_items(
                press_search_name,
                max_items=100,
                window_days=window_days,
            )
            raw_items.extend(prn_items)
            print(f"  PR Newswire: {len(prn_items)} items")
        except Exception as e:
            print(f"  PR Newswire failed: {e}")

        filtered = []
        for item in raw_items:
            dt = _parse_press_date(item.get("date"))
            if dt is not None:
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if dt < cutoff:
                    continue
            filtered.append(item)

        print(f"  Raw total: {len(raw_items)}  |  After 90d filter: {len(filtered)}")
        print(f"\n  FINAL LINKS (would display, {len(filtered)}):\n")
        for i, item in enumerate(filtered[:30], 1):
            date_str = (item.get("date") or "Date unknown")[:10] if item.get("date") else "Date unknown"
            title = (item.get("title") or "Untitled")[:70]
            url = item.get("url") or item.get("link") or ""
            prov = item.get("provider") or ""
            print(f"  {i}. [{date_str}] [{prov}] {title}")
            print(f"      {url}")
        if len(filtered) > 30:
            print(f"  ... and {len(filtered) - 30} more")
        print()

    print("Done (--local: no DB, no user endpoints, no enrichment).")


def main():
    if "--local" in sys.argv:
        run_local_no_db()
        return

    from app.db import get_session
    from app.models import Competitor
    from app.config import settings
    from app.collectors.press import collect_press_snapshot
    from app.collectors.global_press import collect_google_news_items, collect_prnewswire_items
    from app.llm_structured import enrich_press_items_with_llm

    args = [a for a in sys.argv[1:] if a and not a.startswith("-")]
    no_enrich = "--no-enrich" in sys.argv
    competitor_filter = args[0].strip() if args else None

    window_days = 90
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    max_raw = getattr(settings, "press_max_raw_items_per_competitor", 120)
    max_summarize = min(2, getattr(settings, "press_max_articles_to_summarize", 40))  # cap LLM cost for test

    with get_session() as session:
        competitors = session.query(Competitor).order_by(Competitor.name.asc()).all()
        if competitor_filter:
            competitors = [c for c in competitors if competitor_filter.lower() in (c.name or "").lower()]
            if not competitors:
                print(f"No competitor matching {competitor_filter!r}")
                return

        for competitor in competitors:
            endpoints = [ep for ep in competitor.source_endpoints if ep.channel == "press"]
            press_search_name = competitor.name
            from_endpoint = False
            for ep in endpoints:
                opts = getattr(ep, "extra_options", None) if ep else None
                if isinstance(opts, dict) and opts.get("press_search_name"):
                    press_search_name = (opts.get("press_search_name") or "").strip() or press_search_name
                    from_endpoint = True
                    break
            if not from_endpoint and press_search_name:
                key = press_search_name.strip().lower()
                if key == "lark":
                    press_search_name = "Lark Hotels"

            print("\n" + "=" * 80)
            print(f"COMPETITOR: {competitor.name}")
            print(f"  Press search name: {press_search_name!r}  |  Endpoints: {len(endpoints)}")
            print("=" * 80)

            raw_items = []
            for endpoint in endpoints:
                try:
                    snapshot = collect_press_snapshot(endpoint.url)
                except Exception as e:
                    print(f"  [press_endpoint] Error {endpoint.url}: {e}")
                    continue
                items = snapshot.get("items") or []
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    url = (item.get("url") or item.get("link") or "").strip()
                    if url and not url.startswith("http"):
                        from urllib.parse import urljoin
                        url = urljoin(snapshot.get("source_url") or endpoint.url, url)
                    raw_items.append({
                        "title": item.get("title"),
                        "url": url,
                        "date": item.get("date"),
                        "source": item.get("source") or snapshot.get("source_url"),
                        "provider": "press_endpoint",
                    })

            if getattr(settings, "press_enable_google_news", True) and press_search_name:
                try:
                    gn_items = collect_google_news_items(
                        press_search_name,
                        max_items=min(50, getattr(settings, "press_max_items_per_source", 30) * 2),
                        window_days=window_days,
                    )
                    raw_items.extend(gn_items)
                    print(f"  Google News: {len(gn_items)} items")
                except Exception as e:
                    print(f"  Google News failed: {e}")

            try:
                prn_items = collect_prnewswire_items(
                    press_search_name,
                    max_items=100,
                    window_days=window_days,
                )
                raw_items.extend(prn_items)
                print(f"  PR Newswire: {len(prn_items)} items")
            except Exception as e:
                print(f"  PR Newswire failed: {e}")

            filtered_items = []
            for item in raw_items:
                dt = _parse_press_date(item.get("date"))
                if dt is not None:
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    if dt < cutoff:
                        continue
                filtered_items.append(item)
            if len(filtered_items) > max_raw:
                filtered_items = filtered_items[:max_raw]

            print(f"  Raw total: {len(raw_items)}  |  After 90d filter + cap: {len(filtered_items)}")

            if not filtered_items:
                print("  -> No items in window.")
                if not no_enrich:
                    print("  Skipping enrichment.")
                continue

            if no_enrich:
                # Print raw/filtered links only (no LLM).
                print(f"\n  FILTERED LINKS (no dedupe/enrichment) ({len(filtered_items)}):\n")
                for i, item in enumerate(filtered_items[:50], 1):
                    date_str = (item.get("date") or "Date unknown")[:10] if item.get("date") else "Date unknown"
                    title = (item.get("title") or "Untitled")[:70]
                    url = item.get("url") or item.get("link") or ""
                    prov = item.get("provider") or ""
                    print(f"  {i}. [{date_str}] [{prov}] {title}")
                    print(f"      {url}")
                if len(filtered_items) > 50:
                    print(f"  ... and {len(filtered_items) - 50} more")
                print()
                continue

            company_domains = []
            if getattr(competitor, "primary_domain", None):
                company_domains.append(competitor.primary_domain)
            for ep in endpoints:
                try:
                    netloc = urlparse(ep.url).netloc
                    if netloc:
                        company_domains.append(netloc)
                except Exception:
                    pass
            company_domains = list({_normalize_domain(d) for d in company_domains if d})

            try:
                canonical = enrich_press_items_with_llm(
                    competitor.name,
                    filtered_items,
                    max_articles_to_summarize=max_summarize,
                    company_domains=company_domains,
                )
            except Exception as e:
                print(f"  Enrichment failed: {e}")
                import traceback
                traceback.print_exc()
                continue

            # Exclude irrelevant/promo for display (same as dossier)
            EXCLUDED = {"irrelevant", "promo_or_brand_marketing"}
            display = [c for c in canonical if (c.get("topic") or "").strip().lower() not in EXCLUDED]
            display.sort(key=lambda x: (x.get("date") or ""), reverse=True)

            print(f"\n  FINAL NEWS LINKS ({len(display)}):\n")
            for i, item in enumerate(display, 1):
                date_str = (item.get("date") or "Date unknown")[:10] if item.get("date") else "Date unknown"
                title = (item.get("title") or "Untitled")[:70]
                url = item.get("url") or item.get("link") or ""
                outlet = item.get("outlet") or ""
                sec = item.get("secondary_urls") or []
                print(f"  {i}. [{date_str}] {title}")
                print(f"      {url}")
                if outlet:
                    print(f"      Outlet: {outlet}")
                if sec:
                    print(f"      Also: {sec[0][:80]}{'...' if len(sec[0]) > 80 else ''}" + (f" (+{len(sec)-1} more)" if len(sec) > 1 else ""))
                print()

    print("\nDone.")


if __name__ == "__main__":
    main()
