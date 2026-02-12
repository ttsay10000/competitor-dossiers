#!/usr/bin/env bash
# Run app commands using the project venv and .env. Use this so you don't get
# "python not found" or "No module named 'psycopg'" from system Python.
#
# Usage:
#   ./scripts/run.sh                    # run all channels (talent, asset, press, ...)
#   ./scripts/run.sh talent             # run talent only
#   ./scripts/run.sh press              # run press only
#   ./scripts/run.sh asset              # run asset only
#   ./scripts/run.sh asset Lark         # run asset only for Lark (separate process; use if Lark overlaps AvantStay)
#   ./scripts/run.sh asset AvantStay    # run asset only for AvantStay
#   ./scripts/run.sh seed               # run seed (competitors + sources from seed_data.json)
#   ./scripts/run.sh export-seed        # export DB → seed_data.json (run after adding competitors in UI)
#   ./scripts/run.sh serve              # start web server (uvicorn)
#   ./scripts/run.sh migrate             # run alembic upgrade head
#
# Requires: .env with DATABASE_URL (and PLAYWRIGHT_ENABLED=true for talent/asset).
# Create .venv and deps first:  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# Load .env so DATABASE_URL and PLAYWRIGHT_ENABLED are set
if [ -f .env ]; then
  set -a
  source .env
  set +a
fi

# Always use venv Python so psycopg and other deps are available
if [ ! -d .venv ]; then
  echo "Error: .venv not found. Create it and install deps:"
  echo "  cd \"$ROOT\" && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  exit 1
fi
PYTHON="$ROOT/.venv/bin/python"

if [ -z "$DATABASE_URL" ] && [ "$1" != "serve" ]; then
  echo "Error: DATABASE_URL is not set. Add it to .env or export it."
  echo "  Example: DATABASE_URL=postgresql://user:pass@host/dbname"
  exit 1
fi

export PLAYWRIGHT_ENABLED="${PLAYWRIGHT_ENABLED:-true}"
CMD="${1:-all}"

case "$CMD" in
  serve)
    "$PYTHON" -m alembic upgrade head 2>/dev/null || true
    exec "$PYTHON" -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
    ;;
  migrate)
    exec "$PYTHON" -m alembic upgrade head
    ;;
  seed)
    exec "$PYTHON" -m app.seed
    ;;
  export-seed)
    exec "$PYTHON" -m app.cli --export-seed
    ;;
  talent|asset|press|homepage|public_records|all)
    EXTRA=""
    if [ "${2:-}" = "--force" ]; then
      EXTRA="--force"
    elif [ -n "${2:-}" ]; then
      EXTRA="--competitor $2"
      [ "${3:-}" = "--force" ] && EXTRA="$EXTRA --force"
    fi
    exec "$PYTHON" -m app.cli --channel "$CMD" $EXTRA
    ;;
  *)
    echo "Usage: $0 [talent|asset|press|homepage|public_records|all|seed|export-seed|serve|migrate] [competitor_name|--force]"
    echo "  default: all (run all channels)"
    echo "  --force: clear latest snapshots so run does not skip (enrichment re-runs). E.g. ./scripts/run.sh press --force"
    exit 1
    ;;
esac
