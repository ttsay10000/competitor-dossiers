#!/usr/bin/env python3
"""
Run the full press pipeline (classify + filter + grouping) and print the
resulting groups so you can see how articles are clustered.

Usage (from project root):
  python3 scripts/inspect_press_grouping.py Lark
  python3 scripts/inspect_press_grouping.py Placemakr
  python3 scripts/inspect_press_grouping.py AvantStay

Uses same sources as --local press. Requires OPENAI_API_KEY (set in env or in .env).
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Load .env from project root so OPENAI_API_KEY is available for grouping (config reads os.getenv at import time).
# We overwrite so that .env is the source of truth when running this script.
_env_file = ROOT / ".env"
if _env_file.is_file():
    with open(_env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                key = key.strip()
                if key:
                    os.environ[key] = value.strip().strip("'\"").replace("\\n", "\n")

from datetime import datetime, timedelta, timezone

from app.config import settings
from app.collectors.global_press import collect_google_news_items, collect_prnewswire_items
from app.llm_structured import enrich_press_items_with_llm

LOCAL_PRESS_COMPETITORS = [
    ("Lark", "Lark Hotels"),
    ("AvantStay", "AvantStay"),
    ("Placemakr", "Placemakr"),
]


def _parse_press_date(value):
    if value is None:
        return None
    if isinstance(value, datetime):
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
    competitor_name = (sys.argv[1] if len(sys.argv) > 1 else "").strip()
    name_lower = competitor_name.lower()
    competitors = [
        (disp, search) for disp, search in LOCAL_PRESS_COMPETITORS
        if not name_lower or name_lower in disp.lower() or name_lower in search.lower()
    ]
    if not competitors:
        print("No competitor matching {!r}. Options: Lark, AvantStay, Placemakr.".format(competitor_name or "''"))
        sys.exit(1)

    window_days = 90
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    max_per_source = getattr(settings, "press_max_items_per_source", 30)
    max_raw = getattr(settings, "press_max_raw_items_per_competitor", 120)

    for display_name, press_search_name in competitors:
        print("\n" + "=" * 80)
        print("PRESS GROUPING: {} (search: {})".format(display_name, press_search_name))
        print("=" * 80)

        raw_items = []
        if getattr(settings, "press_enable_google_news", True):
            try:
                gn = collect_google_news_items(
                    press_search_name, max_items=min(50, max_per_source * 2), window_days=window_days
                )
                raw_items.extend(gn)
            except Exception as e:
                print("Google News failed: {}".format(e))
        try:
            prn = collect_prnewswire_items(press_search_name, max_items=100, window_days=window_days)
            raw_items.extend(prn)
        except Exception as e:
            print("PR Newswire failed: {}".format(e))

        filtered = []
        for it in raw_items:
            if (it.get("provider") or "").strip().lower() == "prnewswire":
                filtered.append(it)
                continue
            dt = _parse_press_date(it.get("date"))
            if dt and dt < cutoff:
                continue
            filtered.append(it)
        if len(filtered) > max_raw:
            filtered = filtered[:max_raw]

        if not filtered:
            print("No items.")
            continue

        # Full pipeline: classify, filter, group
        press_groups = enrich_press_items_with_llm(
            display_name,
            filtered,
            max_articles_to_summarize=getattr(settings, "press_max_articles_to_summarize", 40),
            company_domains=[],
            previous_items=None,
            previous_canonical=None,
        )

        print("\nGroups ({} total):\n".format(len(press_groups)))
        for gidx, g in enumerate(press_groups, 1):
            title = (g.get("group_title") or "Untitled").strip()
            summary = (g.get("one_line_summary") or "").strip()
            articles = g.get("articles") or []
            print("--- Group {}: {} ({} article(s)) ---".format(gidx, title, len(articles)))
            if summary:
                print("  Summary: {}".format(summary))
            for aidx, art in enumerate(articles, 1):
                t = (art.get("title") or "—").strip()
                if len(t) > 72:
                    t = t[:69] + "..."
                print("  {}. [{}] {} | {}".format(
                    aidx,
                    (art.get("date") or "no date")[:10] if art.get("date") else "no date",
                    t,
                    (art.get("outlet") or "").strip() or "—",
                ))
            print()
        print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
