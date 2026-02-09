import argparse
import sys

from .db import check_db_connection
from .runner import run
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
    args = parser.parse_args()

    try:
        check_db_connection()
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)

    if args.digest:
        print_weekly_digest()
        return

    channel = None if args.channel == "all" else args.channel
    run(channel=channel)


if __name__ == "__main__":
    main()
