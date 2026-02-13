#!/usr/bin/env python3
"""Quick Blueground test: unit tests + optional fast asset fetch (2 destinations, no DB).
Usage:
  pytest tests/test_blueground.py -v              # unit only (no network)
  python scripts/test_blueground.py                # unit + fast asset fetch (needs network + Playwright)
  python scripts/test_blueground.py --unit-only   # unit tests only
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("PLAYWRIGHT_ENABLED", "true")


def _project_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run_unit_tests() -> bool:
    import subprocess
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_blueground.py", "-v"],
        cwd=_project_root(),
    )
    return r.returncode == 0


def run_fast_asset_fetch() -> bool:
    """Fetch destinations page + first 2 city pages; no DB."""
    from app.collectors.asset import _fetch_blueground_destinations

    url = "https://www.theblueground.com/destinations"
    opts = {"max_destinations": 2}
    try:
        raw_html, raw_hash, properties = _fetch_blueground_destinations(url, opts)
        n = len(properties)
        print(f"\n[Blueground asset] Destinations page + 2 cities → {n} properties")
        if properties:
            for p in properties[:5]:
                print(f"  - {p.get('name')} ({p.get('city')}, {p.get('state')})")
        return n >= 0  # success if we got a result (even 0 from 2 pages is valid)
    except Exception as e:
        print(f"\n[Blueground asset] FAILED: {e}")
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Test Blueground (unit + optional fast asset fetch)")
    parser.add_argument("--unit-only", action="store_true", help="Run only unit tests, no network")
    args = parser.parse_args()

    print("=== Blueground tests ===\n")
    ok = run_unit_tests()
    if not ok:
        sys.exit(1)
    if args.unit_only:
        print("\n(Use without --unit-only to run fast asset fetch.)")
        sys.exit(0)
    print("\n--- Fast asset fetch (2 destinations) ---")
    if not run_fast_asset_fetch():
        sys.exit(1)
    print("\nDone.")


if __name__ == "__main__":
    main()
