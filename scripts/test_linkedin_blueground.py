#!/usr/bin/env python3
"""
Test LinkedIn collector for Blueground.

Run: python3 -m scripts.test_linkedin_blueground

Requires:
  - PLAYWRIGHT_ENABLED=true (default)
  - LINKEDIN_STORAGE_STATE_PATH=./linkedin_state.json (from save_linkedin_session)
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Ensure env is loaded
env_file = ROOT / ".env"
if env_file.exists():
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("'\"").strip())

from app.collectors.linkedin import collect_linkedin_company_posts
from app.config import settings

BLUEGROUND_URL = "https://www.linkedin.com/company/blueground-co/posts/?feedView=all"


def main():
    storage = getattr(settings, "linkedin_storage_state_path", None) or os.getenv("LINKEDIN_STORAGE_STATE_PATH")
    if not storage:
        print("WARNING: LINKEDIN_STORAGE_STATE_PATH not set. Run save_linkedin_session first.")
        print("Attempting without auth (will likely get 0 posts)...")

    print(f"Fetching Blueground LinkedIn posts (last 30 days)...")
    print(f"URL: {BLUEGROUND_URL}")
    print()

    try:
        result = collect_linkedin_company_posts(
        BLUEGROUND_URL,
        storage_state_path=storage,
        max_posts=50,
        scroll_pauses=5,
        max_age_days=30,
    )
    except Exception as e:
        print(f"Error: {e}")
        if "Executable doesn't exist" in str(e) or "playwright" in str(e).lower():
            print("\nRun: playwright install")
        sys.exit(1)

    items = result.get("items") or []
    print(f"Posts within 30 days: {len(items)}")
    print("-" * 60)

    for i, p in enumerate(items, 1):
        text = (p.get("text") or "")[:200]
        date = p.get("published_at") or "?"
        print(f"\n[{i}] {date}")
        print(f"    {text}...")
        if p.get("url"):
            print(f"    URL: {p['url']}")

    if not items:
        print("No posts found. Possible causes:")
        print("  - No LINKEDIN_STORAGE_STATE_PATH (login required)")
        print("  - No posts in the last 30 days")
        print("  - LinkedIn DOM/API structure changed")
        print("  - Session expired (re-run save_linkedin_session)")

    print()
    print(f"raw_hash: {result.get('raw_hash', '')[:16]}...")


if __name__ == "__main__":
    main()
