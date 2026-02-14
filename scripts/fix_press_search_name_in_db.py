#!/usr/bin/env python3
"""
One-time fix: set press_search_name to company name only and google_news_search_phrases
to ["furnished rentals"] for Rove, Landing, AKA press endpoints that still have the
long phrase in press_search_name (e.g. "Rove furnished rentals").

Google News RSS uses the quoted part as exact phrase; only the company name should be
quoted. Keywords go in google_news_search_phrases and are sent unquoted.

Usage (from project root). Uses DATABASE_URL_EXTERNAL from .env if set, else DATABASE_URL:
  .venv/bin/python scripts/fix_press_search_name_in_db.py
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

# This script only: use external URL for this run (no .env edits). Process env is set so
# that when we import app.db below, config uses this URL instead of INTERNAL.
_external_url = os.environ.get("DATABASE_URL_EXTERNAL") or os.environ.get("DATABASE_URL")
if _external_url:
    os.environ["DATABASE_URL"] = _external_url
    os.environ.pop("DATABASE_URL_INTERNAL", None)

# Maps old (wrong) press_search_name -> (company_name_only, keywords_list)
FIXES = {
    "Rove furnished rentals": ("Rove", ["furnished rentals"]),
    "Landing furnished rentals": ("Landing", ["furnished rentals"]),
    "AKA furnished rentals": ("AKA", ["furnished rentals"]),
}


def main() -> int:
    if not os.getenv("DATABASE_URL"):
        print("ERROR: DATABASE_URL not set.", file=sys.stderr)
        return 1
    try:
        from app.db import get_session
        from app.models import Competitor
        from sqlalchemy.orm import selectinload
    except Exception as e:
        print(f"ERROR: import failed: {e}", file=sys.stderr)
        return 1

    updated = 0
    with get_session() as session:
        competitors = (
            session.query(Competitor)
            .options(selectinload(Competitor.source_endpoints))
            .all()
        )
        for c in competitors:
            for ep in c.source_endpoints or []:
                if (ep.channel or "").strip().lower() != "press":
                    continue
                opts = ep.extra_options or {}
                current_name = (opts.get("press_search_name") or "").strip()
                if current_name not in FIXES:
                    continue
                company_only, keywords = FIXES[current_name]
                new_opts = {**opts, "press_search_name": company_only, "google_news_search_phrases": keywords}
                ep.extra_options = new_opts
                updated += 1
                print(f"Fixed {c.name} press: press_search_name={current_name!r} -> {company_only!r}, phrases={keywords}")

        # Cleanup: for Rove, AKA, Landing (name already short), set phrases to exactly ["furnished rentals"]
        for c in competitors:
            if (c.name or "").strip() not in ("Rove", "AKA", "Landing"):
                continue
            for ep in c.source_endpoints or []:
                if (ep.channel or "").strip().lower() != "press":
                    continue
                opts = ep.extra_options or {}
                if opts.get("press_search_name") not in ("Rove", "AKA", "Landing"):
                    continue
                current_phrases = opts.get("google_news_search_phrases")
                if current_phrases == ["furnished rentals"]:
                    continue
                new_opts = {**opts, "google_news_search_phrases": ["furnished rentals"]}
                ep.extra_options = new_opts
                updated += 1
                print(f"Cleaned {c.name} press: phrases -> ['furnished rentals']")

    print(f"Updated {updated} press endpoint(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
