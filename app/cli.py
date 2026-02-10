import argparse
import sys

from .db import check_db_connection
from .runner import run, advance_baseline_after_full_refresh
from .digest import print_weekly_digest


def main() -> None:
    parser = argparse.ArgumentParser(description="Competitor Signals Runner")
    parser.add_argument(
        "--channel",
        choices=["talent", "asset", "press", "homepage", "public_records", "all"],
        default="all",
        help="Which channel to run",
    )
    parser.add_argument(
        "--digest",
        action="store_true",
        help="Print weekly digest (last 7 days)",
    )
    parser.add_argument(
        "--competitor",
        type=str,
        default=None,
        metavar="NAME",
        help="Run only for this competitor (e.g. Lark, AvantStay, Placemakr). Use with --channel to avoid overlap issues.",
    )
    parser.add_argument(
        "--seed",
        action="store_true",
        help="Run seed (update competitors/sources from seed.py) before channels. Use after changing seed.py.",
    )
    args = parser.parse_args()

    try:
        check_db_connection()
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)

    if args.digest:
        print_weekly_digest()
        return

    if args.seed:
        from .seed import run_seed
        run_seed()

    channel = None if args.channel == "all" else args.channel
    run(channel=channel, competitor_name=args.competitor)
    # After a full refresh (all channels, all competitors), advance comparison baseline.
    if channel is None and args.competitor is None:
        advance_baseline_after_full_refresh()


if __name__ == "__main__":
    main()
