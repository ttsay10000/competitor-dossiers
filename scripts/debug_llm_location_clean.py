#!/usr/bin/env python3
# Run: python scripts/debug_llm_location_clean.py [competitor_name]
# Example: python scripts/debug_llm_location_clean.py AvantStay
#
# Builds the same inputs the dossier sends to the LLM (location counts + sub-bullets),
# runs clean_location_display_for_dossier, and prints inputs vs output to see what
# curtails the numbers. Requires .env with DATABASE_URL and OPENAI_API_KEY.

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
    from app.routes.dossier import build_dossier_context, clear_dossier_caches_for_competitor
    from app.executive_summary import clean_location_display_for_dossier

    with get_session() as session:
        c = session.query(Competitor).filter(Competitor.name.ilike(name)).first()
        if not c:
            print(f"No competitor named {name!r} found.")
            sys.exit(1)
        competitor_id = c.id
        competitor_name = c.name
        print(f"Competitor: {competitor_name} (id={competitor_id})")
        # Get raw data (no LLM) so we have exact inputs
        context = build_dossier_context(session, competitor_id, skip_property_llm=True)
    if "error" in context:
        print("Error:", context["error"])
        sys.exit(1)

    properties_by_location = context.get("properties_by_location") or []
    asset_delta_by_city = context.get("asset_delta_by_city") or []
    other_properties_display = context.get("other_properties_display") or []
    raw_total = sum(r.get("count", 0) for r in properties_by_location)
    total_properties = context.get("total_properties", 0)

    # Same as dossier: build sub-bullets text for LLM
    other_sub_bullets_text = None
    if other_properties_display:
        other_sub_bullets_text = "\n".join(
            f"{d.get('url', '')} — {d.get('raw_location', 'Other')}" for d in other_properties_display
        )

    max_location_rows = 150
    rows_sent = properties_by_location[:max_location_rows]
    sum_sent = sum(r.get("count", 0) for r in rows_sent)
    rows_not_sent = len(properties_by_location) - len(rows_sent)
    count_not_sent = raw_total - sum_sent

    print("\n--- INPUT TO LLM ---")
    print(f"Total properties (source of truth): {total_properties}")
    print(f"Raw location rows: {len(properties_by_location)} (sum of counts = {raw_total})")
    print(f"Location rows SENT to LLM: first {len(rows_sent)} (max_location_rows={max_location_rows})")
    print(f"  Sum of counts in rows SENT: {sum_sent}")
    if rows_not_sent > 0:
        print(f"  Rows NOT sent: {rows_not_sent} (count in those rows: {count_not_sent})")
        print(f"  -> LLM never sees {count_not_sent} properties; output cannot sum to {raw_total}.")
    print(f"Other sub-bullets (URLs for 'Other' etc.): {len(other_properties_display)} lines")
    if other_sub_bullets_text:
        char_count = len(other_sub_bullets_text)
        print(f"  Total chars: {char_count}")
        lines = other_sub_bullets_text.strip().splitlines()
        print(f"  First 3 lines:")
        for line in lines[:3]:
            print(f"    {line[:100]}{'...' if len(line) > 100 else ''}")
    print(f"Asset deltas by city (first 25 sent): {len(asset_delta_by_city)} rows")

    # Clear cache so we get a fresh LLM call; ask for parsed output even when rejected
    clear_dossier_caches_for_competitor(competitor_id)
    print("\n--- CALLING LLM (clean_location_display_for_dossier) ---")
    cleaned = clean_location_display_for_dossier(
        competitor_name,
        properties_by_location,
        asset_delta_by_city,
        other_sub_bullets_text=other_sub_bullets_text,
        debug_return_parsed=True,
    )

    print("\n--- OUTPUT FROM LLM ---")
    if cleaned is None:
        print("LLM returned None (API key missing or parse error before validation).")
        print("So the dossier keeps the RAW location breakdown (no state grouping).")
        return

    rejected = cleaned.get("_rejected")
    reason = cleaned.get("_reason", "")
    counts = cleaned.get("properties_by_location") or []
    cleaned_total = cleaned.get("cleaned_total") or sum(r.get("count", 0) for r in counts)
    match = cleaned.get("location_totals_match", True)

    if rejected:
        print(f"LLM output was REJECTED (reason: {reason}). Dossier keeps raw breakdown.")
        if cleaned.get("_raw_content_preview"):
            print(f"Raw LLM response preview: {cleaned.get('_raw_content_preview')!r}")
        print(f"Parsed output from LLM (what would have curtailed the numbers):")
    print(f"Cleaned total (sum of state bullets): {cleaned_total}")
    print(f"Raw total: {raw_total}")
    print(f"Totals match: {match}")
    if raw_total and cleaned_total != raw_total:
        print(f"  -> Shortfall: {raw_total - cleaned_total} properties.")
    print(f"State rows returned: {len(counts)}")
    print("\nState breakdown (from LLM):")
    for r in counts:
        loc = r.get("location", "")
        n = r.get("count", 0)
        k = r.get("keys", 0)
        kstr = f" ({k} keys)" if k else ""
        print(f"  {loc}: {n} properties{kstr}")

if __name__ == "__main__":
    main()
