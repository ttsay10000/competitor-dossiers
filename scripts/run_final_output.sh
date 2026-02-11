#!/usr/bin/env bash
# Run the press final-output script using the project venv and .env.
#
# Usage:
#   ./scripts/run_final_output.sh                    # all competitors from DB (with enrichment)
#   ./scripts/run_final_output.sh --no-enrich        # all competitors, raw links only (no LLM)
#   ./scripts/run_final_output.sh "Lark"             # one competitor by name
#   ./scripts/run_final_output.sh --local             # no DB: Google News + PR Newswire only (Lark, AvantStay, Placemakr)
#
# Requires: .venv and .env. Create venv once:
#   python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
# For --local you don't need DATABASE_URL. For DB mode you need DATABASE_URL in .env.
# For enrichment (default, no --no-enrich) you need OPENAI_API_KEY in .env.

set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# Load .env
if [ -f .env ]; then
  set -a
  source .env
  set +a
fi

# Use project venv
if [ ! -d .venv ]; then
  echo "Error: .venv not found. Create it and install deps:"
  echo "  cd \"$ROOT\" && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  exit 1
fi
PYTHON="$ROOT/.venv/bin/python"

# DATABASE_URL only required when not using --local
if [ -z "$DATABASE_URL" ] && [[ " $* " != *" --local "* ]]; then
  echo "Warning: DATABASE_URL is not set. Add it to .env for DB mode, or use --local."
fi

exec "$PYTHON" "$ROOT/scripts/test_press_final_output.py" "$@"
