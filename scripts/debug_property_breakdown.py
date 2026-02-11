#!/usr/bin/env python3
# Run: python scripts/debug_property_breakdown.py [competitor_name]
# Example: python scripts/debug_property_breakdown.py AvantStay
# Prints total property count vs state breakdown sum and list. Requires .env with DATABASE_URL.

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

env_file = ROOT / ".env"
if env_file.exists():
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

def main():
    name = (sys.argv[1] or "AvantStay").strip()
    from app.db import get_session
    from app.models import Competitor
    from app.routes.dossier import build_dossier_context

    with get_session() as session:
        c = session.query(Competitor).filter(Competitor.name.ilike(name)).first()
        if not c:
            print(f"No competitor named {name!r} found.")
            sys.exit(1)
        competitor_id = c.id
        print(f"Competitor: {c.name} (id={competitor_id})")
        context = build_dossier_context(session, competitor_id, skip_property_llm=False)
    if "error" in context:
        print("Error:", context["error"])
        sys.exit(1)

    total = context.get("total_properties", 0)
    rows = context.get("properties_by_location") or []
    breakdown_sum = sum(r.get("count", 0) for r in rows)
    match = context.get("location_totals_match", True)

    print(f"\nTotal properties (source): {total}")
    print(f"Breakdown sum (state bullets): {breakdown_sum}")
    print(f"Totals match: {match}")
    if total and breakdown_sum != total:
        print(f"  -> Difference: {breakdown_sum - total:+d}")
    print(f"\nState breakdown ({len(rows)} rows):")
    for r in rows:
        loc = r.get("location", "")
        count = r.get("count", 0)
        keys = r.get("keys", 0)
        keys_str = f" ({keys} keys)" if keys else ""
        print(f"  {loc}: {count} properties{keys_str}")

if __name__ == "__main__":
    main()
