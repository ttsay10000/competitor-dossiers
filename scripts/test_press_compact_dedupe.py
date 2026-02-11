#!/usr/bin/env python3
"""
Test the press grouping pipeline. Fetches Google News + PR Newswire for Lark,
runs full enrichment (classify, filter, group LLM, Press releases group), prints groups and articles.

No DB required. Needs OPENAI_API_KEY in env or .env.

Usage:
  python scripts/test_press_compact_dedupe.py
"""
import os
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone

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


def _parse_press_date(value):
    if value is None:
        return None
    if hasattr(value, "year"):
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


def main():
    from app.config import settings
    from app.collectors.global_press import collect_google_news_items, collect_prnewswire_items
    from app.llm_structured import enrich_press_items_with_llm

    if not settings.openai_api_key:
        print("OPENAI_API_KEY required. Set in .env or environment.")
        sys.exit(1)

    competitor_name = "Lark Hotels"
    press_search_name = "Lark Hotels"
    window_days = 90
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    max_raw = 120

    print("=" * 80)
    print(f"PRESS GROUPING TEST: {competitor_name}")
    print("=" * 80)

    raw_items = []
    if getattr(settings, "press_enable_google_news", True):
        try:
            gn_items = collect_google_news_items(press_search_name, max_items=50, window_days=window_days)
            raw_items.extend(gn_items)
            print(f"Google News: {len(gn_items)} items")
        except Exception as e:
            print(f"Google News failed: {e}")
    try:
        prn_items = collect_prnewswire_items(press_search_name, max_items=100, window_days=window_days)
        raw_items.extend(prn_items)
        print(f"PR Newswire: {len(prn_items)} items")
    except Exception as e:
        print(f"PR Newswire failed: {e}")

    # 90d filter (same as runner - PR Newswire not exempt in this simple test)
    filtered = []
    for item in raw_items:
        dt = _parse_press_date(item.get("date"))
        if dt is not None and dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if dt is not None and dt < cutoff:
            continue
        filtered.append(item)
    if len(filtered) > max_raw:
        filtered = filtered[:max_raw]

    print(f"Raw total: {len(raw_items)} | After 90d + cap: {len(filtered)}")
    if not filtered:
        print("No items. Exiting.")
        return

    print("\nRunning enrichment (classify, filter, group LLM, Press releases group)...")
    if "DEBUG_PRESS_DEDUPE" in os.environ or "--debug-dedupe" in sys.argv:
        print("(Watch stderr for [press] Group LLM and pipeline logs)\n")

    try:
        press_groups = enrich_press_items_with_llm(
            competitor_name,
            filtered,
            max_articles_to_summarize=min(10, getattr(settings, "press_max_articles_to_summarize", 40)),
            company_domains=["larkhotels.com", "larkhospitality.com"],
        )
    except Exception as e:
        print(f"Enrichment failed: {e}")
        import traceback
        traceback.print_exc()
        return

    canonical = [art for g in press_groups for art in (g.get("articles") or [])]
    n_articles = len(canonical)
    print("=" * 80)
    print(f"FINAL OUTPUT: {len(press_groups)} groups, {n_articles} articles")
    print("=" * 80)
    for gi, group in enumerate(press_groups, 1):
        print(f"\n  Group {gi}: {group.get('group_title') or 'News'}")
        if group.get("one_line_summary"):
            print(f"    Summary: {group['one_line_summary'][:120]}")
        for art in group.get("articles") or []:
            date_str = (art.get("date") or "no date")[:10] if art.get("date") else "no date"
            title = (art.get("title") or "Untitled")[:75]
            url = art.get("url") or art.get("link") or ""
            outlet = art.get("outlet") or ""
            print(f"    - [{date_str}] {title}")
            if outlet:
                print(f"      Outlet: {outlet}")
            if url:
                print(f"      {url[:90]}{'...' if len(url) > 90 else ''}")


if __name__ == "__main__":
    main()
