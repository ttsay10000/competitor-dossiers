#!/usr/bin/env bash
# Refresh competitor data (talent, asset, press, etc.) using local Playwright + Render DB.
# Usage: ./scripts/refresh.sh [talent|all]   (default: all)
# Requires: .env in project root with DATABASE_URL and PLAYWRIGHT_ENABLED=true

set -e
cd "$(dirname "$0")/.."

if [ -f .env ]; then
  set -a
  source .env
  set +a
fi

if [ -z "$DATABASE_URL" ]; then
  echo "Error: DATABASE_URL is not set. Add it to .env or export it."
  exit 1
fi

export PLAYWRIGHT_ENABLED="${PLAYWRIGHT_ENABLED:-true}"
CHANNEL="${1:-all}"

if [ -d .venv ]; then
  source .venv/bin/activate
fi

echo "Refreshing channel(s): $CHANNEL (PLAYWRIGHT_ENABLED=$PLAYWRIGHT_ENABLED)"
python -m app.cli --channel "$CHANNEL"
