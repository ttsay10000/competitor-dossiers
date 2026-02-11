# Scripts (operational only)

All debug and test scripts have been removed. The site does not depend on them.

**Remaining scripts** in `scripts/` are for local/operational use only (not deployed to Render; `scripts/` is in `.dockerignore`).

---

## How to run scripts (from a blank terminal)

1. **Open a terminal** and go to the project root:
   ```bash
   cd "/Users/tylertsay/Desktop/AI project - competitor dossiers"
   ```
   (Or wherever the repo is on your machine.)

2. **Create a virtualenv and install dependencies** (once per machine):
   ```bash
   ./scripts/setup_venv.sh
   ```
   If `.venv` already exists, use the project’s Python for the next steps (e.g. `.venv/bin/python` or activate with `source .venv/bin/activate`).

3. **Add a `.env` file** in the project root with at least:
   - `DATABASE_URL` — for run/refresh/migrate (Postgres connection string).
   - Optional: `OPENAI_API_KEY` for LLM-backed features.

4. **Run the script you need** from the project root:
   ```bash
   ./scripts/run.sh serve          # start web server (http://127.0.0.1:8000)
   ./scripts/run.sh                # run all channels (talent, asset, press, …)
   ./scripts/run.sh talent         # run talent only
   ./scripts/run.sh press          # run press only
   ./scripts/run.sh migrate        # run DB migrations
   ./scripts/refresh.sh            # refresh data (default: all)
   ./scripts/refresh.sh talent     # refresh talent only
   ./scripts/force_refresh_all.sh   # force full refresh
   ```
   The scripts load `.env` and use the project `.venv`, so you don’t need to activate the venv first.

---

| Script | Purpose |
|--------|---------|
| `run.sh` | Run the app |
| `run_local.sh` | Run the app locally |
| `refresh.sh` | Refresh data |
| `force_refresh_all.sh` | Force full refresh |
| `migrate.sh` | DB migrations (Alembic is used in Docker; this is for local) |
| `setup_venv.sh` | Set up Python virtualenv |

**Local inspection (no DB):**

| Script / command | Purpose |
|------------------|--------|
| `python scripts/inspect_press_classification.py [Lark\|AvantStay\|Placemakr]` | Full list of press articles and what is marked **irrelevant** (or promo / not_about_company) vs included. Uses Google News + PR Newswire; requires `OPENAI_API_KEY` in `.env`. |

The web service and cron on Render use only the `app` package (`uvicorn app.main:app`, `python -m app.cli`).
