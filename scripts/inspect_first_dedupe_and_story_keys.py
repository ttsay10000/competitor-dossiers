#!/usr/bin/env python3
"""
Diagnostic: run press grouping pipeline and print groups (group_title, one_line_summary, articles).

Shows how the Group LLM clusters articles by title/date/topic and how PR Newswire
is appended as a single "Press releases" group.

Usage:
  python3 scripts/inspect_first_dedupe_and_story_keys.py [competitor]

Requires OPENAI_API_KEY. Uses Google News + PR Newswire.
"""
import os
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

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
        print("OPENAI_API_KEY required.")
        sys.exit(1)

    competitor_name = (sys.argv[1] if len(sys.argv) > 1 else "Lark Hotels").strip()
    if competitor_name.lower() == "lark":
        competitor_name = "Lark Hotels"
        press_search = "Lark Hotels"
    else:
        press_search = competitor_name

    window_days = 90
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    max_raw = 80  # cap for this diagnostic

    print("=" * 90)
    print(f"PRESS GROUPING DIAGNOSTIC: {competitor_name}")
    print("=" * 90)

    raw_items = []
    if getattr(settings, "press_enable_google_news", True):
        try:
            gn = collect_google_news_items(press_search, max_items=40, window_days=window_days)
            raw_items.extend(gn)
            print(f"Google News: {len(gn)} items")
        except Exception as e:
            print(f"Google News failed: {e}")
    try:
        prn = collect_prnewswire_items(press_search, max_items=40, window_days=window_days)
        raw_items.extend(prn)
        print(f"PR Newswire: {len(prn)} items")
    except Exception as e:
        print(f"PR Newswire failed: {e}")

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

    print(f"Raw total after 90d + cap: {len(filtered)}\n")

    if not filtered:
        print("No items. Exiting.")
        return

    print("Running enrichment (classify, filter, group LLM, Press releases group)...")
    try:
        press_groups = enrich_press_items_with_llm(
            competitor_name,
            filtered,
            max_articles_to_summarize=5,
            company_domains=["larkhotels.com", "larkhospitality.com"] if "lark" in competitor_name.lower() else [],
        )
    except Exception as e:
        print(f"Enrichment failed: {e}")
        import traceback
        traceback.print_exc()
        return

    n_articles = sum(len(g.get("articles") or []) for g in press_groups)
    print("\n" + "-" * 90)
    print(f"GROUPS: {len(press_groups)} groups, {n_articles} articles")
    print("-" * 90)
    for gi, group in enumerate(press_groups, 1):
        title = group.get("group_title") or "News"
        summary = (group.get("one_line_summary") or "")[:100]
        arts = group.get("articles") or []
        print(f"\n  Group {gi}: {title}")
        if summary:
            print(f"    Summary: {summary}")
        print(f"    Articles ({len(arts)}):")
        for art in arts[:15]:
            date_str = (art.get("date") or "no date")[:10] if art.get("date") else "no date"
            t = (art.get("title") or "Untitled")[:65]
            outlet = (art.get("outlet") or "")[:25]
            print(f"      [{date_str}] [{outlet}] {t}")
        if len(arts) > 15:
            print(f"      ... and {len(arts) - 15} more")


if __name__ == "__main__":
    main()
