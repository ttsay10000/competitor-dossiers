#!/usr/bin/env python3
"""
Run export-seed (DB → seed_data.json) using DB URL from .env.

Use when Export seed fails in the UI (e.g. on Render's read-only filesystem).
Uses DATABASE_URL_EXTERNAL if set (Render External URL), else DATABASE_URL. Put the
URL you want to export from in .env, then run:

  .venv/bin/python scripts/export_seed_with_env.py

from the project root. Commit the updated seed_data.json and app/seed.py.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

def _load_dotenv():
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip("'\"").replace("\\n", "\n")
                if key:
                    os.environ.setdefault(key, value)

_load_dotenv()

# Export to seed: prefer external URL (for pulling from Render when local)
export_url = os.environ.get("DATABASE_URL_EXTERNAL") or os.environ.get("DATABASE_URL")
if not export_url:
    print("No .env found or DATABASE_URL/DATABASE_URL_EXTERNAL set. Set DATABASE_URL_EXTERNAL (Render External) or DATABASE_URL in .env (see .env.example).", file=sys.stderr)
    sys.exit(1)
os.environ["DATABASE_URL"] = export_url

from app.seed import export_seed_to_file

if __name__ == "__main__":
    export_seed_to_file()
