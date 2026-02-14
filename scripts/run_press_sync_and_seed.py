#!/usr/bin/env python3
"""
Run all three steps together: (1) fix press search name in DB, (2) run seed, (3) compare file vs DB.

Use when you have DATABASE_URL (or DATABASE_URL_EXTERNAL) set in .env to a reachable DB
(e.g. Render external URL from your machine). Run from project root:

  .venv/bin/python scripts/run_press_sync_and_seed.py
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Load .env
_env = ROOT / ".env"
if _env.exists():
    with open(_env) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip("'\"").replace("\\n", "\n")
            if k:
                os.environ.setdefault(k, v)

# Prefer external URL when comparing/fixing from local (internal host often unreachable)
export_url = os.environ.get("DATABASE_URL_EXTERNAL") or os.environ.get("DATABASE_URL")
if export_url:
    os.environ["DATABASE_URL"] = export_url
    os.environ["DATABASE_URL_INTERNAL"] = export_url

if not os.getenv("DATABASE_URL"):
    print("ERROR: Set DATABASE_URL or DATABASE_URL_EXTERNAL in .env", file=sys.stderr)
    sys.exit(1)

def main():
    # Step 1: Fix press search name (Rove, Landing, AKA)
    print("=== Step 1: Fix press search name in DB ===")
    from scripts.fix_press_search_name_in_db import main as fix_main
    if fix_main() != 0:
        sys.exit(1)

    # Step 2: Run seed (file → DB)
    print("\n=== Step 2: Run seed (file → DB) ===")
    from app.seed import run_seed
    run_seed()
    print("Seed done.")

    # Step 3: Compare file vs DB
    print("\n=== Step 3: Compare seed file vs DB ===")
    from scripts.compare_seed_and_db import main as compare_main
    return compare_main()

if __name__ == "__main__":
    sys.exit(main())
