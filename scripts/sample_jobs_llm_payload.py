#!/usr/bin/env python3
"""
Print a sample of:
  1) Job data that goes INTO build_structured (same as comes out of collect).
  2) The exact text fed to the LLM for jobs (step 2: enrich_jobs_with_llm).

Usage (from repo root; .env with DATABASE_URL):
  python scripts/sample_jobs_llm_payload.py [competitor_name]

Example:
  python scripts/sample_jobs_llm_payload.py Lark
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Load .env
env_file = ROOT / ".env"
if env_file.exists():
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            import os
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def main():
    name = (sys.argv[1] if len(sys.argv) > 1 else "").strip()
    from app.db import get_session
    from app.models import Competitor, Snapshot

    with get_session() as session:
        if name:
            c = session.query(Competitor).filter(Competitor.name.ilike(name)).first()
        else:
            c = session.query(Competitor).order_by(Competitor.name.asc()).first()
        if not c:
            print("No competitor found." if name else "No competitors in DB.")
            sys.exit(1)
        latest = (
            session.query(Snapshot)
            .filter(Snapshot.competitor_id == c.id, Snapshot.channel == "talent")
            .order_by(Snapshot.captured_at.desc())
            .first()
        )
        if not latest or not (latest.structured_json or {}).get("jobs"):
            print(f"Competitor: {c.name} — no talent snapshot or no jobs.")
            sys.exit(1)

        structured = latest.structured_json or {}
        jobs = [j for j in structured.get("jobs", []) if isinstance(j, dict)]
        sample_size = min(8, len(jobs))

    print("=" * 70)
    print(f"Competitor: {c.name}  |  Total jobs in snapshot: {len(jobs)}")
    print("=" * 70)

    # --- 1) Sample of data going INTO build_structured (and thus into enrich_jobs_with_llm) ---
    print("\n--- 1) Sample job data (into build_structured / into enrich_jobs_with_llm) ---")
    print("(Each job dict has: job_id, title, location, dept, posted_date, url;")
    print(" after enrichment also: functional_area, is_senior, capability_bucket, is_strategic)\n")
    for i, j in enumerate(jobs[:sample_size]):
        # Show only the fields that exist before enrichment (what collector produces)
        row = {
            "job_id": j.get("job_id"),
            "title": j.get("title"),
            "location": j.get("location"),
            "dept": j.get("dept"),
            "posted_date": j.get("posted_date"),
            "url": (j.get("url") or "")[:50] + ("..." if (j.get("url") or "") and len(j.get("url") or "") > 50 else ""),
        }
        print(f"  [{i}] {row}")
    if len(jobs) > sample_size:
        print(f"  ... and {len(jobs) - sample_size} more jobs")

    # --- 2) Exact text fed to the LLM (same format as enrich_jobs_with_llm) ---
    print("\n--- 2) Exact text fed to the LLM (Jobs: ...) ---")
    lines = []
    for i, j in enumerate(jobs[:150]):
        title = (j.get("title") or "").strip()
        dept = (j.get("dept") or "").strip()
        location = (j.get("location") or "").strip()
        lines.append(f"{i}: title={title!r} dept={dept!r} location={location!r}")
    llm_user = "Jobs:\n" + "\n".join(lines)
    # Print first 12 lines so we see several jobs
    llm_preview = "\n".join(llm_user.split("\n")[:12])
    print(llm_preview)
    if len(jobs) > 12:
        print(f"... ({len(jobs)} jobs total; only first 150 are sent to the LLM)")
    print()

    # Count how many have empty dept/location (explains weak categorization)
    empty_dept = sum(1 for j in jobs if not (j.get("dept") or "").strip())
    empty_loc = sum(1 for j in jobs if not (j.get("location") or "").strip())
    print("--- Context for categorization ---")
    print(f"  Jobs with empty dept:  {empty_dept} / {len(jobs)}")
    print(f"  Jobs with empty location: {empty_loc} / {len(jobs)}")
    print("  (When dept/location are empty, the LLM categorizes from title only.)")


if __name__ == "__main__":
    main()
