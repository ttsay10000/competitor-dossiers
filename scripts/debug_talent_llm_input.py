#!/usr/bin/env python3
"""
Inspect the exact data sent to the LLM for talent snapshot role classification,
and see which jobs end up in "Other" after enrichment.

Usage (from repo root, .env with DATABASE_URL; optional OPENAI_API_KEY for live enrichment):
  python scripts/debug_talent_llm_input.py [competitor_name]

Example:
  python scripts/debug_talent_llm_input.py Lark
  python scripts/debug_talent_llm_input.py   # uses first competitor in DB
"""

import os
import sys
from pathlib import Path
from collections import Counter

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

env_file = ROOT / ".env"
if env_file.exists():
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def build_llm_payload_lines(jobs: list) -> list[str]:
    """Same format as enrich_jobs_with_llm: index, title, dept, location."""
    lines = []
    for i, j in enumerate(jobs):
        title = (j.get("title") or "").strip()
        dept = (j.get("dept") or "").strip()
        location = (j.get("location") or "").strip()
        lines.append(f"{i}: title={title!r} dept={dept!r} location={location!r}")
    return lines


def main():
    name = (sys.argv[1] or "").strip()
    from app.db import get_session
    from app.models import Competitor, Snapshot
    from app.llm_structured import enrich_jobs_with_llm, TALENT_FUNCTIONAL_AREAS
    from app.rules.talent_rules import job_functional_area

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
            print(f"Competitor: {c.name} — no talent snapshot or no jobs in latest snapshot.")
            sys.exit(1)
        jobs = [j for j in (latest.structured_json or {}).get("jobs", []) if isinstance(j, dict)]
        # Use jobs without functional_area to see raw input (same as before enrich in runner)
        jobs_input = [{"title": j.get("title"), "dept": j.get("dept"), "location": j.get("location")} for j in jobs]

    print(f"Competitor: {c.name}")
    print(f"Latest talent snapshot: {len(jobs_input)} jobs")
    print()

    # Exact payload sent to the LLM (same as in enrich_jobs_with_llm)
    lines = build_llm_payload_lines(jobs_input[:150])
    print("--- Exact payload sent to LLM (Jobs: ...) ---")
    print("Jobs:")
    for line in lines:
        print(line)
    if len(jobs_input) > 150:
        print(f"... (only first 150 of {len(jobs_input)} jobs are sent to the LLM)")
    print()

    # Rule-based only (no API): what would be Other without LLM
    rule_counts = Counter()
    rule_other = []
    for j in jobs_input:
        fa = job_functional_area(j)
        rule_counts[fa] += 1
        if fa == "Other":
            rule_other.append((j.get("title") or "", j.get("dept") or "", j.get("location") or ""))

    print("--- Rule-based classification only (no LLM) ---")
    for area in TALENT_FUNCTIONAL_AREAS:
        n = rule_counts.get(area, 0)
        if n:
            print(f"  {area}: {n}")
    print(f"  -> Other: {rule_counts.get('Other', 0)}")
    if rule_other:
        print("\n  Jobs that rule-based puts in Other (title, dept, location):")
        for title, dept, loc in rule_other[:50]:
            dept_str = f" | dept={dept!r}" if dept else ""
            loc_str = f" | location={loc!r}" if loc else ""
            print(f"    {title!r}{dept_str}{loc_str}")
        if len(rule_other) > 50:
            print(f"    ... and {len(rule_other) - 50} more")
    print()

    # Live enrichment if API key set
    has_key = bool(os.environ.get("OPENAI_API_KEY"))
    if has_key:
        enriched = enrich_jobs_with_llm(jobs_input)
        llm_counts = Counter((j.get("functional_area") or "Other") for j in enriched)
        other_jobs = [(j.get("title"), j.get("dept"), j.get("location")) for j in enriched if (j.get("functional_area") or "Other") == "Other"]
        print("--- After enrich_jobs_with_llm (LLM + rule fallback for Other) ---")
        for area in TALENT_FUNCTIONAL_AREAS:
            n = llm_counts.get(area, 0)
            if n:
                print(f"  {area}: {n}")
        print(f"  -> Other: {llm_counts.get('Other', 0)}")
        if other_jobs:
            print("\n  Jobs in Other after enrichment (title, dept, location):")
            for title, dept, loc in other_jobs[:50]:
                dept_str = f" | dept={dept!r}" if dept else ""
                loc_str = f" | location={loc!r}" if loc else ""
                print(f"    {title!r}{dept_str}{loc_str}")
            if len(other_jobs) > 50:
                print(f"    ... and {len(other_jobs) - 50} more")
    else:
        print("--- Live LLM enrichment skipped (no OPENAI_API_KEY) ---")
        print("Set OPENAI_API_KEY in .env to run enrich_jobs_with_llm and see post-enrichment Other list.")


if __name__ == "__main__":
    main()
