import argparse
import os
import sys
import time

# Load .env and prefer DATABASE_URL_EXTERNAL so --local press can reach DB and get phrases
_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_env = os.path.join(_root, ".env")
if os.path.isfile(_env):
    with open(_env) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip("'\"").replace("\\n", "\n")
            if k:
                os.environ.setdefault(k, v)
_ext = os.environ.get("DATABASE_URL_EXTERNAL")
if _ext:
    os.environ["DATABASE_URL"] = _ext

from .db import check_db_connection, get_session
from .models import Competitor
from .runner import run, advance_baseline_after_full_refresh, clear_latest_snapshots
from .digest import print_weekly_digest
from .routes.dossier import build_dossier_context, format_dossier_preview_text


def main() -> None:
    parser = argparse.ArgumentParser(description="Competitor Signals Runner")
    parser.add_argument(
        "--channel",
        choices=["talent", "asset", "press", "homepage", "public_records", "reviews", "social", "all"],
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
        help="Run seed (update competitors/sources from seed_data.json) before channels.",
    )
    parser.add_argument(
        "--export-seed",
        action="store_true",
        help="Export current DB competitors and sources to seed_data.json. Run after adding competitors in the UI, then commit the file.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Clear latest snapshots for the channel(s) so the run does not skip (enrichment/dedupe re-runs). Global across all competitors.",
    )
    parser.add_argument(
        "--local",
        action="store_true",
        help="Run press only, no database (Google News + PR Newswire + enrichment). Use with --channel press. Competitors: Lark, AvantStay, Placemakr.",
    )
    args = parser.parse_args()

    from .config import settings
    print(f"[cli] PLAYWRIGHT_ENABLED={getattr(settings, 'playwright_enabled', False)}", flush=True)

    if not args.local:
        # On Render, cron jobs can hit "Connection refused" to internal Postgres because the
        # private network may not be ready in the first few seconds after the job starts.
        if os.getenv("RENDER"):
            delay = int(os.getenv("CRON_DB_STARTUP_DELAY", "5"))
            if delay > 0:
                print(f"[cli] Waiting {delay}s for private network before DB connect...", flush=True)
                time.sleep(delay)
        try:
            check_db_connection()
        except RuntimeError as e:
            print(str(e), file=sys.stderr)
            sys.exit(1)
    elif args.local and args.channel != "press":
        print("When using --local, --channel is set to press (no DB).", file=sys.stderr)

    if args.digest:
        print_weekly_digest()
        return

    if args.export_seed and not args.local:
        from .seed import export_seed_to_file
        export_seed_to_file()
        return

    if args.seed and not args.local:
        from .seed import run_seed
        run_seed()

    channel = None if args.channel == "all" else args.channel
    if args.local:
        channel = "press"

    if not args.local and args.force:
        with get_session() as session:
            channel_to_clear = channel  # None = all channels
            deleted = clear_latest_snapshots(session, channel_to_clear)
        print(f"[force] Cleared {deleted} latest snapshot(s) — run will not skip on hash.")
        if channel:
            print(f"[force] Channel: {channel}")
        else:
            print(f"[force] All channels (talent, asset, press, homepage, public_records, reviews, social)")

    run(channel=channel, competitor_name=args.competitor, local=args.local)

    if not args.local:
        # Print what will be displayed on the site for each competitor (for debugging).
        with get_session() as session:
            if args.competitor:
                competitors = session.query(Competitor).filter(Competitor.name == args.competitor).all()
            else:
                competitors = session.query(Competitor).order_by(Competitor.name).all()
            if competitors:
                print("\n" + "=" * 60, flush=True)
                print("  SITE PREVIEW — what will be displayed on each dossier page", flush=True)
                print("=" * 60, flush=True)
                for c in competitors:
                    context = build_dossier_context(session, c.id, skip_property_llm=True)
                    print(format_dossier_preview_text(context), flush=True)

        # After a full refresh (all channels, all competitors), advance comparison baseline.
        if channel is None and args.competitor is None:
            advance_baseline_after_full_refresh()


if __name__ == "__main__":
    main()
