#!/usr/bin/env python3
"""
Run the press pipeline: fetch, classify, filter, group (LLM), add Press releases group.
Prints groups and articles per group.

Shows:
- After classify + business filter: how many items
- After grouping: list of groups with group_title, one_line_summary, and articles

Usage:
  python3 scripts/inspect_press_after_classify.py [competitor]
  python3 scripts/inspect_press_after_classify.py Lark

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
    max_raw = 80

    print("=" * 80)
    print(f"PRESS PIPELINE: {competitor_name} — after classify through final dedupe")
    print("=" * 80)

    raw_items = []
    if getattr(settings, "press_enable_google_news", True):
        try:
            gn = collect_google_news_items(press_search, max_items=50, window_days=window_days)
            raw_items.extend(gn)
            print(f"\nGoogle News: {len(gn)} items")
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

    print(f"Raw (90d + cap): {len(filtered)}")

    if not filtered:
        print("No items. Exiting.")
        return

    print("\n--- Running full enrichment (classify, filter, group, Press releases) ---")
    try:
        press_groups = enrich_press_items_with_llm(
            competitor_name,
            filtered,
            max_articles_to_summarize=min(5, getattr(settings, "press_max_articles_to_summarize", 40)),
            company_domains=["larkhotels.com", "larkhospitality.com"] if "lark" in competitor_name.lower() else [],
        )
    except Exception as e:
        print(f"Enrichment failed: {e}")
        import traceback
        traceback.print_exc()
        return

    n_articles = sum(len(g.get("articles") or []) for g in press_groups)
    print(f"\n--- Final output: {len(press_groups)} groups, {n_articles} articles ---")
    for gi, group in enumerate(press_groups, 1):
        title = (group.get("group_title") or "News")[:60]
        summary = (group.get("one_line_summary") or "")[:80]
        arts = group.get("articles") or []
        print(f"\n  Group {gi}: {title}")
        if summary:
            print(f"    Summary: {summary}")
        for art in arts[:10]:
            date_str = (art.get("date") or "no date")[:10] if art.get("date") else "no date"
            t = (art.get("title") or "Untitled")[:60]
            print(f"    - [{date_str}] {t}")
        if len(arts) > 10:
            print(f"    ... and {len(arts) - 10} more")
    print("\nDone.")


if __name__ == "__main__":
    main()
