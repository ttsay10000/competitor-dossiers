#!/usr/bin/env python3
"""
Test press collection for Placemakr only: Google News + PR Newswire.
No DB or OpenAI. Run from project root: python3 scripts/test_press_placemakr.py
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Minimal env load so config works
_env = ROOT / ".env"
if _env.is_file():
    with open(_env) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                if k.strip():
                    os.environ[k.strip()] = (v.strip().strip("'\"").replace("\\n", "\n"))

from app.config import settings
from app.collectors.global_press import collect_google_news_items, collect_prnewswire_items

def main():
    name = "Placemakr"
    window_days = 90
    max_gn = min(50, getattr(settings, "press_max_items_per_source", 30) * 2)
    print(f"Press collection test: {name}")
    print(f"  press_enable_google_news = {getattr(settings, 'press_enable_google_news', True)}")
    print()

    # 1) Google News (Step 1 in runner)
    gn_items = []
    try:
        print(f"Step 1 — Google News: \"{name}\" when:{window_days}d ...")
        gn_items = collect_google_news_items(name, max_items=max_gn, window_days=window_days, search_phrases=None)
        print(f"Step 1 — Google News returned {len(gn_items)} items")
    except Exception as e:
        print(f"Step 1 — Google News failed: {e}")
        import traceback
        traceback.print_exc()

    # 2) PR Newswire (Step 2 in runner)
    pr_items = []
    try:
        print(f"Step 2 — PR Newswire: {name} (90d) ...")
        pr_items = collect_prnewswire_items(name, max_items=100, window_days=window_days)
        print(f"Step 2 — PR Newswire returned {len(pr_items)} items")
    except Exception as e:
        print(f"Step 2 — PR Newswire failed: {e}")
        import traceback
        traceback.print_exc()

    print()
    print("By provider:", {"google_news": len(gn_items), "prnewswire": len(pr_items)})
    print("Total raw:", len(gn_items) + len(pr_items))
    if gn_items:
        print("Sample Google News:", gn_items[0].get("title", "")[:60], gn_items[0].get("url", "")[:50])
    if pr_items:
        print("Sample PR Newswire:", pr_items[0].get("title", "")[:60], pr_items[0].get("url", "")[:50])

if __name__ == "__main__":
    main()
