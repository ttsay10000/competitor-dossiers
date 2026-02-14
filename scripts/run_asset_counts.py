#!/usr/bin/env python3
"""
Run asset collection per competitor from seed_data.json; results are written to
asset_count_results.json so each competitor can be run separately. Vacasa and
Blueground run last when using default order.

Usage:
  # Run one competitor (writes/updates scripts/asset_count_results.json)
  python scripts/run_asset_counts.py --competitor "AKA"
  python scripts/run_asset_counts.py --competitor "Lark" --competitor "Placemakr"

  # Run all in order (others first, then Vacasa, Blueground)
  python scripts/run_asset_counts.py

  # Print table from existing results only (no network)
  python scripts/run_asset_counts.py --summary-only

Requires network; set PLAYWRIGHT_ENABLED=true for JS sources (Lark, Kasa, Rove).
"""
import argparse
import json
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
RESULTS_FILE = Path(__file__).resolve().parent / "asset_count_results.json"

# Run Vacasa and Blueground last (slow / large).
ORDER_OTHERS_FIRST = ["AKA", "AvantStay", "Kasa Living", "Landing", "Lark", "Placemakr", "Rove"]
ORDER_LAST = ["Vacasa", "Blueground"]
ORDER_ALL = ORDER_OTHERS_FIRST + ORDER_LAST


def _load_seed() -> list[dict]:
    path = ROOT / "seed_data.json"
    if not path.exists():
        print("seed_data.json not found", file=sys.stderr)
        sys.exit(1)
    data = json.loads(path.read_text())
    competitors = data.get("competitors", [])
    if not competitors:
        print("No competitors in seed_data.json", file=sys.stderr)
        sys.exit(1)
    return competitors


def _competitor_by_name(competitors: list, name: str) -> Optional[dict]:
    for c in competitors:
        if (c.get("name") or "").strip() == name.strip():
            return c
    return None


def _load_results() -> dict:
    if not RESULTS_FILE.exists():
        return {}
    try:
        return json.loads(RESULTS_FILE.read_text())
    except Exception:
        return {}


def _save_results(results: dict) -> None:
    RESULTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    RESULTS_FILE.write_text(json.dumps(results, indent=2) + "\n")


def _run_one(name: str, url: str, js_required: bool, use_sitemap_first: bool, extra_options: Optional[dict]) -> tuple:
    sys.path.insert(0, str(ROOT))
    from app.collectors.asset import collect_asset_snapshot, _canonical_asset_strategy

    strategy = _canonical_asset_strategy(url) or "(chain)"
    try:
        snapshot = collect_asset_snapshot(
            url,
            js_required=js_required,
            use_sitemap_first=use_sitemap_first,
            extra_options=extra_options,
        )
        props = snapshot.get("properties") or []
        note = (snapshot.get("note") or "").strip() or ""
        return strategy, len(props), note
    except Exception as e:
        return strategy, -1, str(e)[:80]


def run_competitors(names: list[str]) -> None:
    competitors = _load_seed()
    name_to_c = {c.get("name"): c for c in competitors if c.get("name")}
    results = _load_results()

    for name in names:
        c = _competitor_by_name(competitors, name)
        if not c:
            print(f"Unknown competitor: {name}", file=sys.stderr)
            continue
        for src in c.get("sources") or []:
            if src.get("channel") != "asset":
                continue
            url = src.get("url") or ""
            strategy, count, note = _run_one(
                name,
                url,
                src.get("js_required", False),
                src.get("use_sitemap_first", False),
                src.get("extra_options"),
            )
            results[name] = {"strategy": strategy, "count": count, "note": note}
            _save_results(results)
            count_str = str(count) if count >= 0 else "ERROR"
            print(f"[{name}] {strategy} -> {count_str} properties", flush=True)
            break


def print_summary() -> None:
    results = _load_results()
    if not results:
        print("No results yet. Run with --competitor <name> first.", file=sys.stderr)
        sys.exit(1)
    # Order: others first, then Vacasa, Blueground; then any remaining by name
    order = [n for n in ORDER_ALL if n in results]
    for n in sorted(results.keys()):
        if n not in order:
            order.append(n)
    rows = [(n, results[n].get("strategy", ""), results[n].get("count", -1), results[n].get("note", "")) for n in order]
    print("\n" + "=" * 72)
    print("Asset properties per competitor (from asset_count_results.json)")
    print("=" * 72)
    print(f"{'Competitor':<18} {'Strategy':<22} {'Properties':>10}  Note")
    print("-" * 72)
    for name, strategy, count, note in rows:
        count_str = str(count) if count >= 0 else "ERROR"
        note_short = (note or "")[:32].replace("\n", " ")
        print(f"{name:<18} {strategy:<22} {count_str:>10}  {note_short}")
    print("-" * 72)
    total_ok = sum(1 for _, _, c, _ in rows if c >= 0)
    total_props = sum(c for _, _, c, _ in rows if c >= 0)
    print(f"Total: {total_ok}/{len(rows)} competitors, {total_props} properties.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run asset collection per competitor; results in scripts/asset_count_results.json")
    parser.add_argument("--competitor", action="append", dest="competitors", metavar="NAME", help="Run only this competitor (can repeat)")
    parser.add_argument("--summary-only", action="store_true", help="Print table from existing results; no network")
    args = parser.parse_args()

    if args.summary_only:
        print_summary()
        return

    competitors = _load_seed()
    if args.competitors:
        names = args.competitors
    else:
        # Default: run all in order (Vacasa and Blueground last)
        names = [c.get("name") for c in competitors if c.get("name") in ORDER_ALL]
        # Preserve ORDER_ALL order
        names = [n for n in ORDER_ALL if n in names]
        # Include any seed competitor not in ORDER_ALL
        for c in competitors:
            n = c.get("name")
            if n and n not in names:
                names.append(n)

    run_competitors(names)
    print_summary()


if __name__ == "__main__":
    main()
