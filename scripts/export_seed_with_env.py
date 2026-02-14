#!/usr/bin/env python3
"""
Run export-seed (DB → seed_data.json) using DATABASE_URL from .env.

Use when Export seed fails in the UI (e.g. on Render's read-only filesystem).
Put Render's External Database URL in .env as DATABASE_URL, then run:

  .venv/bin/python scripts/export_seed_with_env.py

from the project root. Commit the updated seed_data.json.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Load .env from project root so DATABASE_URL (e.g. Render external URL) is set
env_file = ROOT / ".env"
if env_file.exists():
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
else:
    print("No .env found. Set DATABASE_URL in .env (see .env.example) or in the environment.", file=sys.stderr)
    if not os.environ.get("DATABASE_URL"):
        sys.exit(1)

from app.seed import export_seed_to_file

if __name__ == "__main__":
    export_seed_to_file()
