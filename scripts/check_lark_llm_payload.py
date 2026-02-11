#!/usr/bin/env python3
"""
Show exactly what data is sent to the LLM for Lark talent enrichment.
No database required: fetches Lark's career page via the collector and builds
the same payload as enrich_jobs_with_llm.

Usage (from repo root):
  python3 scripts/check_lark_llm_payload.py

Requires: requests, beautifulsoup4 (and optionally playwright for JS-rendered page).
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Optional: load .env for PLAYWRIGHT_ENABLED etc.
env_file = ROOT / ".env"
if env_file.exists():
    import os
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def main():
    from app.collectors.talent import collect_talent_snapshot
    from app.llm_structured import TALENT_FUNCTIONAL_AREAS

    url = "https://ats.wizehire.com/career-site/lark-hospitality"
    print("Fetching Lark talent snapshot...")
    print(f"  URL: {url}\n")
    try:
        snapshot = collect_talent_snapshot(url)
    except Exception as e:
        print(f"Error collecting snapshot: {e}")
        sys.exit(1)

    jobs = snapshot.get("jobs") or []
    provider = snapshot.get("provider", "unknown")
    print(f"Collected: {len(jobs)} jobs (provider: {provider})")

    # Same format as enrich_jobs_with_llm
    batch = jobs[:150]
    lines = []
    for i, j in enumerate(batch):
        title = (j.get("title") or "").strip()
        dept = (j.get("dept") or "").strip()
        location = (j.get("location") or "").strip()
        lines.append(f"{i}: title={title!r} dept={dept!r} location={location!r}")

    # Stats
    empty_dept = sum(1 for j in jobs if not (j.get("dept") or "").strip())
    empty_loc = sum(1 for j in jobs if not (j.get("location") or "").strip())
    print(f"  Jobs with empty dept:   {empty_dept} / {len(jobs)}")
    print(f"  Jobs with empty location: {empty_loc} / {len(jobs)}")
    if len(jobs) > 150:
        print(f"  (Only first 150 of {len(jobs)} jobs are sent to the LLM.)")
    print()

    # Exact payload sent to the LLM (user message)
    user = "Jobs:\n" + "\n".join(lines)
    print("=" * 70)
    print("EXACT PAYLOAD SENT TO THE LLM (user message)")
    print("=" * 70)
    if user.strip() == "Jobs:":
        print("(No jobs in this run — Lark uses WizeHire/JS; need PLAYWRIGHT_ENABLED and playwright install for live fetch.)")
        print()
        print("When jobs are present, each line looks like:")
        print("  0: title='Front Desk Agent' dept='' location=''")
        print("  1: title='Housekeeping Supervisor' dept='' location=''")
        print("  ...")
        print("For generic/WizeHire sources, dept and location are almost always empty — only title is sent.")
    else:
        print(user)
    print()
    print("(System prompt asks for JSON array: index, functional_area, is_senior)")
    print("(Canonical areas:", ", ".join(TALENT_FUNCTIONAL_AREAS), ")")


if __name__ == "__main__":
    main()
