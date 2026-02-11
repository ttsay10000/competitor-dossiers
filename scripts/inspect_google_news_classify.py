#!/usr/bin/env python3
"""
Inspect Google News articles for a competitor and show what the classifier
assigns: is_about_company (true/false) and topic.

Usage:
  python scripts/inspect_google_news_classify.py [competitor_name]
  python scripts/inspect_google_news_classify.py Lark

Requires OPENAI_API_KEY in .env or environment.
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
    from app.collectors.global_press import collect_google_news_items
    from app.llm_structured import _classify_press_headlines_with_llm

    if not settings.openai_api_key:
        print("OPENAI_API_KEY required. Set in .env or environment.")
        sys.exit(1)

    competitor_name = (sys.argv[1] if len(sys.argv) > 1 else "Lark Hotels").strip()
    if competitor_name.lower() == "lark":
        competitor_name = "Lark Hotels"
        press_search = "Lark Hotels"
    else:
        press_search = competitor_name

    window_days = 90
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)

    print("=" * 90)
    print(f"GOOGLE NEWS CLASSIFICATION: {competitor_name} (search: {press_search!r})")
    print("=" * 90)

    gn_items = []
    if getattr(settings, "press_enable_google_news", True):
        try:
            gn_items = collect_google_news_items(press_search, max_items=50, window_days=window_days)
        except Exception as e:
            print(f"Google News failed: {e}")
            sys.exit(1)

    # 90d filter
    filtered = []
    for item in gn_items:
        dt = _parse_press_date(item.get("date"))
        if dt is not None and dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if dt is not None and dt < cutoff:
            continue
        filtered.append(item)

    print(f"Google News raw: {len(gn_items)}  |  After 90d: {len(filtered)}\n")
    if not filtered:
        print("No items. Exiting.")
        return

    classified = _classify_press_headlines_with_llm(competitor_name, filtered)

    # Summary counts
    about = sum(1 for c in classified if c.get("is_about_company"))
    not_about = len(classified) - about
    print(f"is_about_company=True:  {about}")
    print(f"is_about_company=False: {not_about}\n")

    # Topic distribution
    by_topic = {}
    for c in classified:
        t = (c.get("topic") or "other").strip()
        by_topic[t] = by_topic.get(t, 0) + 1
    print("Topic distribution:")
    for t, n in sorted(by_topic.items(), key=lambda x: -x[1]):
        print(f"  {t}: {n}")

    print("\n" + "-" * 90)
    print("ALL ITEMS (title | is_about_company | topic)")
    print("-" * 90)

    for i, c in enumerate(classified, 1):
        title = (c.get("title") or "Untitled")[:65]
        ab = "yes" if c.get("is_about_company") else "no "
        topic = (c.get("topic") or "").strip()
        outlet = (c.get("outlet") or c.get("source") or "")[:25]
        print(f"{i:3}. [{ab}] [{topic:25}] {title}")
        if outlet:
            print(f"      Outlet: {outlet}")


if __name__ == "__main__":
    main()
