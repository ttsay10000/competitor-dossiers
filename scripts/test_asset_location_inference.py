#!/usr/bin/env python3
"""
Test non-LLM location/state inference for Vacasa and Blueground.

Run: python scripts/test_asset_location_inference.py

Uses sample properties (no network) and runs:
  1. _assign_state_from_url (Avantstay-style URLs only)
  2. _infer_state_from_name_if_missing (rule-based name/market → state)

Shows before/after so we can see how well Vacasa and Blueground do without LLM.
"""
import sys
import os

# Project root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.llm_structured import _assign_state_from_url, _infer_state_from_name_if_missing
from app.diff.asset_diff import infer_location_for_property, infer_state_from_name_and_market


# ----- Vacasa-style: often only url + name, no state/market from HTML -----
VACASA_SAMPLES = [
    {"url": "https://www.vacasa.com/unit/12345", "name": "Beach House in Destin", "market": None, "status": None},
    {"url": "https://www.vacasa.com/unit/67890", "name": "Condo in Panama City Beach", "market": None, "status": None},
    {"url": "https://www.vacasa.com/unit/11111", "name": "Coastal Oregon Home in Cannon Beach", "market": None, "status": None},
    {"url": "https://www.vacasa.com/unit/22222", "name": "Cabin in Bend - Central Oregon", "market": None, "status": None},
    {"url": "https://www.vacasa.com/unit/33333", "name": "Lake Tahoe Ski Condo", "market": None, "status": None},
    {"url": "https://www.vacasa.com/unit/44444", "name": "Gatlinburg Cabin Near Smoky Mountains", "market": None, "status": None},
    {"url": "https://www.vacasa.com/unit/55555", "name": "Generic Downtown Loft", "market": None, "status": None},
    # Simulate data-* already on prop (as if extract_properties_from_html captured them)
    {"url": "https://www.vacasa.com/unit/99999", "name": "Mountain View", "market": None, "state": "Colorado", "city": "Breckenridge"},
]

# ----- Blueground-style: collector already sets market/state from destination page -----
BLUEGROUND_SAMPLES = [
    {"url": "https://www.theblueground.com/p/furnished-apartments/bos-1", "name": "#42A • 2BR Apartment", "market": "Acton, Massachusetts", "state": "Massachusetts", "city": "Acton"},
    {"url": "https://www.theblueground.com/p/furnished-apartments/nyc-2", "name": "#12B • Studio", "market": "New York, New York", "state": "New York", "city": "New York"},
    # Edge case: market set but state missing (should stay or get inferred)
    {"url": "https://www.theblueground.com/p/furnished-apartments/atl-3", "name": "#7C • 1BR", "market": "Atlanta, Georgia", "state": None, "city": "Atlanta"},
]


def run_pipeline(prop: dict) -> dict:
    """Apply URL then name-based inference (no LLM)."""
    p = _assign_state_from_url(prop)
    p = _infer_state_from_name_if_missing(p)
    return p


def main():
    print("=" * 60)
    print("VACASA (no LLM): url + name only → state/market from rules")
    print("=" * 60)
    for i, raw in enumerate(VACASA_SAMPLES, 1):
        out = run_pipeline(raw)
        loc_label = infer_location_for_property(out)
        print(f"\n  [{i}] name: {raw.get('name')!r}")
        print(f"      before: state={raw.get('state')!r} market={raw.get('market')!r}")
        print(f"      after:  state={out.get('state')!r} market={out.get('market')!r}")
        print(f"      → location label: {loc_label!r}")

    print("\n" + "=" * 60)
    print("BLUEGROUND (no LLM): already has market/state from collector")
    print("=" * 60)
    for i, raw in enumerate(BLUEGROUND_SAMPLES, 1):
        out = run_pipeline(raw)
        loc_label = infer_location_for_property(out)
        print(f"\n  [{i}] name: {raw.get('name')!r}  market: {raw.get('market')!r}")
        print(f"      before: state={raw.get('state')!r}")
        print(f"      after:  state={out.get('state')!r}")
        print(f"      → location label: {loc_label!r}")

    print("\n" + "=" * 60)
    print("Rule-based infer_state_from_name_and_market() quick checks")
    print("=" * 60)
    checks = [
        ("Beach House in Destin", None),
        ("Condo in Bend", None),
        ("Panama City Beach Villa", None),
        ("Something in Central Oregon", None),
        ("Random Loft", None),
    ]
    for name, market in checks:
        state = infer_state_from_name_and_market(name, market)
        print(f"  {name!r} + market={market!r} → state={state!r}")

    print("\nDone.")


if __name__ == "__main__":
    main()
