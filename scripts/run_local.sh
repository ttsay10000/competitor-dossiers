#!/usr/bin/env bash
# Run migrations then start the app locally. Usage: ./scripts/run_local.sh
# Requires: DATABASE_URL in .env or environment. Use .venv (pip install -r requirements.txt).

set -e
cd "$(dirname "$0")/.."

if [ -f .env ]; then
  set -a
  source .env
  set +a
fi

if [ -z "$DATABASE_URL" ]; then
  echo "Error: DATABASE_URL is not set. Create a .env file with:"
  echo "  DATABASE_URL=postgresql://USER:PASSWORD@HOST/DATABASE"
  echo "  (e.g. your Render External URL: ...@dpg-xxx.ohio-postgres.render.com/DATABASE)"
  exit 1
fi

# Use venv Python so we have psycopg etc. (avoid 'No module named psycopg' from system Python)
if [ -d .venv ]; then
  PYTHON=".venv/bin/python"
else
  PYTHON="python3"
fi

if ! "$PYTHON" -c "import psycopg" 2>/dev/null; then
  echo "Error: psycopg not found. Activate the project venv and install deps:"
  echo "  source .venv/bin/activate && pip install -r requirements.txt"
  exit 1
fi

echo "Running migrations..."
"$PYTHON" -m alembic upgrade head
echo "Starting server at http://127.0.0.1:8000"
exec "$PYTHON" -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
