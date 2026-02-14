#!/usr/bin/env python3
"""
Run seed (seed_data.json → DB) using internal Postgres URL from .env.

Uses DATABASE_URL_INTERNAL if set (run-seed = file → DB), else DATABASE_URL.
On Render set DATABASE_URL=internal so UI Run seed uses internal. For this script
from your machine, set DATABASE_URL to the external URL and leave DATABASE_URL_INTERNAL
unset so it can reach Render's DB. Run from project root:

  .venv/bin/python scripts/run_seed_with_env.py
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

# Run seed (file → DB): use DATABASE_URL (from local use external; on Render the app uses INTERNAL via env).
# From your machine, internal host is unreachable — set DATABASE_URL to external in .env.
run_seed_url = os.environ.get("DATABASE_URL")
if not run_seed_url:
    print("Set DATABASE_URL in .env (use external URL to reach Render from your machine).", file=sys.stderr)
    sys.exit(1)
os.environ["DATABASE_URL_INTERNAL"] = run_seed_url  # so app config uses this for main engine when seed runs

from app.seed import run_seed

if __name__ == "__main__":
    run_seed()
