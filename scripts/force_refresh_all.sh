#!/usr/bin/env bash
# Force refresh all channels (talent, asset, press, homepage, public_records) for all competitors.
# Clears latest snapshots so every run re-collects and re-enriches.
#
# Usage: ./scripts/force_refresh_all.sh
#
# Requires: .env with DATABASE_URL; .venv with deps (see scripts/setup_venv.sh).

set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [ -f .env ]; then
  set -a
  source .env
  set +a
fi

if [ -z "$DATABASE_URL" ]; then
  echo "Error: DATABASE_URL is not set. Add it to .env or export it."
  exit 1
fi

if [ ! -d .venv ]; then
  echo "Error: .venv not found. Run: ./scripts/setup_venv.sh"
  exit 1
fi
PYTHON="$ROOT/.venv/bin/python"

export PLAYWRIGHT_ENABLED="${PLAYWRIGHT_ENABLED:-true}"

echo "Force refresh: clearing latest snapshots for all channels, then running all."
exec "$PYTHON" -m app.cli --channel all --force
