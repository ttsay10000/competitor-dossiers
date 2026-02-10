#!/usr/bin/env bash
# Run Alembic migrations locally. Use this before pushing to catch migration issues (e.g. revision ID length).
# Usage: ./scripts/migrate.sh
# Requires: DATABASE_URL (set in .env or environment)

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

if [ -d .venv ]; then
  source .venv/bin/activate
fi

echo "Running: alembic upgrade head"
python -m alembic upgrade head
echo "Migrations complete."
