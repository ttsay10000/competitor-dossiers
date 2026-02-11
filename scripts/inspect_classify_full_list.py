#!/usr/bin/env python3
"""
Print the full list of collected press items with classification for review.
Shows all items (before dropping topic=irrelevant) with: index, title, outlet, topic, irrelevant (YES/NO).

Usage:
  python3 scripts/inspect_classify_full_list.py [competitor]
  python3 scripts/inspect_classify_full_list.py Lark

Requires OPENAI_API_KEY. Uses Google News + PR Newswire (same as pipeline).
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
    from app.llm_structured import _classify_press_headlines_with_llm

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
    max_raw = 120

    print("=" * 95)
    print(f"FULL CLASSIFY LIST FOR REVIEW: {competitor_name} (search: {press_search!r})")
    print("After classify (drop topic=irrelevant), only non-irrelevant items are kept.")
    print("This list shows ALL items and their topic so you can verify irrelevant tagging.")
    print("=" * 95)

    raw_items = []
    if getattr(settings, "press_enable_google_news", True):
        try:
            gn_items = collect_google_news_items(press_search, max_items=50, window_days=window_days)
            raw_items.extend(gn_items)
            print(f"\nGoogle News: {len(gn_items)} items")
        except Exception as e:
            print(f"Google News failed: {e}")
    try:
        prn_items = collect_prnewswire_items(press_search, max_items=100, window_days=window_days)
        raw_items.extend(prn_items)
        print(f"PR Newswire: {len(prn_items)} items")
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

    print(f"Raw total (90d + cap {max_raw}): {len(filtered)}\n")
    if not filtered:
        print("No items.")
        return

    classified = _classify_press_headlines_with_llm(competitor_name, filtered)
    irrelevant_count = sum(1 for c in classified if (c.get("topic") or "").strip().lower() == "irrelevant")
    kept_count = len(classified) - irrelevant_count

    print(f"After classify: {len(classified)} -> {kept_count} kept (dropped {irrelevant_count} as topic=irrelevant)\n")
    print("-" * 95)
    print(f"{'#':>3}  {'IRRELEVANT':^10}  {'TOPIC':<28}  TITLE")
    print("-" * 95)

    for i, c in enumerate(classified, 1):
        topic = (c.get("topic") or "").strip() or "(none)"
        is_irrelevant = (topic.lower() == "irrelevant")
        irr_label = "YES" if is_irrelevant else "no"
        title = (c.get("title") or "Untitled")[:70]
        outlet = (c.get("outlet") or c.get("source") or "")[:24]
        print(f"{i:3}  {irr_label:^10}  {topic:<28}  {title}")
        if outlet:
            print(f"       outlet: {outlet}")
        url = (c.get("url") or c.get("link") or "").strip()
        if url:
            print(f"       {url[:88]}{'...' if len(url) > 88 else ''}")
        print()

    print("-" * 95)
    print(f"Summary: {irrelevant_count} irrelevant, {kept_count} kept (non-irrelevant)")


if __name__ == "__main__":
    main()
