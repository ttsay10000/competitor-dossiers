#!/usr/bin/env python3
"""
Fetch talent data for Placemakr and AvantStay and print job titles (and dept, location)
exactly as they appear before the LLM enricher (enrich_jobs_with_llm).
This is the raw input to the functional_area / is_senior classifier.

Usage (from project root):
  python scripts/inspect_talent_titles.py
  python scripts/inspect_talent_titles.py Placemakr
  python scripts/inspect_talent_titles.py AvantStay
  python scripts/inspect_talent_titles.py Lark

AvantStay (Kula) and Lark (WizeHire) are JS-rendered; set PLAYWRIGHT_ENABLED=true for full job lists.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Load .env from project root
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

from app.collectors.talent import collect_talent_snapshot, build_structured_json as build_talent_structured

TALENT_SOURCES = [
    ("Placemakr", "https://jobs.lever.co/placemakr"),      # Lever API
    ("AvantStay", "https://careers.kula.ai/avantstay"),    # Kula (generic/JS)
    ("Lark", "https://ats.wizehire.com/career-site/lark-hospitality"),  # WizeHire (generic/JS)
]


def main():
    filter_name = (sys.argv[1] if len(sys.argv) > 1 else "").strip().lower()
    competitors = [
        (name, url) for name, url in TALENT_SOURCES
        if not filter_name or filter_name in name.lower()
    ]
    if not competitors:
        print("Usage: python scripts/inspect_talent_titles.py [Placemakr|AvantStay|Lark]")
        print("No competitor matching {!r}. Options: Placemakr, AvantStay, Lark.".format(filter_name or "''"))
        sys.exit(1)

    for name, url in competitors:
        print("\n" + "=" * 80)
        print(f"TALENT TITLES (before LLM): {name}")
        print(f"URL: {url}")
        print("=" * 80)

        try:
            snapshot = collect_talent_snapshot(url)
        except Exception as e:
            print("Error collecting: {}".format(e))
            import traceback
            traceback.print_exc()
            continue

        provider = snapshot.get("provider", "unknown")
        raw_jobs = snapshot.get("jobs", [])
        print(f"\nProvider: {provider}")
        print(f"Raw jobs collected: {len(raw_jobs)}\n")

        structured = build_talent_structured(snapshot)
        jobs = structured.get("jobs", [])

        print("Jobs (exactly as sent to enrich_jobs_with_llm):\n")
        print("  {:<6} {:<50} {:<25} {:<30}".format("INDEX", "TITLE", "DEPT", "LOCATION"))
        print("  " + "-" * 115)

        for i, j in enumerate(jobs):
            title = (j.get("title") or "").strip() or "(empty)"
            dept = (j.get("dept") or "").strip() or "(empty)"
            location = (j.get("location") or "").strip() or "(empty)"
            title_short = title[:47] + "…" if len(title) > 50 else title
            dept_short = dept[:23] + "…" if len(dept) > 25 else dept
            loc_short = location[:28] + "…" if len(location) > 30 else location
            print("  {:<6} {:<50} {:<25} {:<30}".format(i, title_short, dept_short, loc_short))

        print("\nDone ({} jobs).".format(len(jobs)))
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
