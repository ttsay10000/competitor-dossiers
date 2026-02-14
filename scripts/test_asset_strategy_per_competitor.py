#!/usr/bin/env python3
"""Print which asset strategy runs per competitor (canonical map). Run after code changes to verify refresh behavior."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.collectors.asset import _canonical_asset_strategy


def main() -> None:
    path = ROOT / "seed_data.json"
    if not path.exists():
        print("seed_data.json not found")
        sys.exit(1)
    data = json.loads(path.read_text())
    competitors = data.get("competitors", data) if isinstance(data, dict) else data
    print("Asset strategy per competitor (canonical; no chain/fallback)\n")
    print(f"{'Competitor':<20} {'Strategy':<28} URL")
    print("-" * 90)
    for c in competitors:
        name = c.get("name") or "?"
        for src in c.get("sources") or []:
            if src.get("channel") != "asset":
                continue
            url = src.get("url") or ""
            strategy = _canonical_asset_strategy(url)
            if strategy is None:
                strategy = "(chain/infer from opts)"
            print(f"{name:<20} {strategy:<28} {url}")
    print("-" * 90)
    print("\nIf strategy is (chain/infer from opts), that URL is not in the canonical map and will use DB/opts.")


if __name__ == "__main__":
    main()
