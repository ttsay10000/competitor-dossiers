#!/usr/bin/env bash
# Create .venv and install dependencies so you can run the app and CLI.
# Run once from repo root:  ./scripts/setup_venv.sh

set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [ -d .venv ]; then
  echo ".venv already exists. To reinstall deps: .venv/bin/pip install -r requirements.txt"
  echo "To activate: source .venv/bin/activate"
  exit 0
fi

echo "Creating .venv and installing requirements..."
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
echo "Done. Activate with:  source .venv/bin/activate"
echo "Or run the app:  ./scripts/run.sh serve"
