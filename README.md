# Competitor Signals Platform

MVP system for monitoring strategic shifts among flexible-use hospitality competitors.

Initial focus:
- Talent Radar (Lever, Greenhouse, Ashby, and generic career pages)
- Asset Watch (properties/locations, sitemap or HTML)
- Press/Public Narrative (RSS, blog/press pages)
- Homepage / digital footprint (cache URL, detect meaningful changes)
- Public records (trademark/regulatory filings via configurable URLs; RSS or HTML)

Primary users: Executive and Strategy leadership.

Dashboard features:
- **Feed:** Filterable event timeline with source links; “Data last refreshed” in header.
- **Digest:** Weekly digest (last 7 days, top events per competitor).
- **Executive summary:** Per-competitor weekly view at `/dossier/{id}/summary` with high-signal updates and recommended next actions.
- **Dossier:** Per-competitor view with takeaways, recommendations, “New this week” (properties/footprint), and source links.

## Local Setup (MVP)

**If you see "python: command not found" or "No module named 'psycopg'"** — use the project venv and the `run.sh` script so the correct Python and env are used:

```bash
# One-time: create venv and install deps
cd "/Users/tylertsay/Desktop/AI project - competitor dossiers"
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
python3 -m playwright install chromium   # for talent/asset if needed

# Create .env with (use your real Render Postgres URL):
# DATABASE_URL=postgresql://USER:PASSWORD@dpg-xxx.ohio-postgres.render.com/DATABASE
# PLAYWRIGHT_ENABLED=true

# Run anything via run.sh (uses .venv + .env automatically):
./scripts/run.sh              # run all channels
./scripts/run.sh talent       # talent only
./scripts/run.sh press        # press only
./scripts/run.sh seed         # seed competitors
./scripts/run.sh serve        # start web server
./scripts/run.sh migrate      # run migrations
```

1. Create a Postgres database named `competitor_signals` (or use Render Postgres and put its URL in `.env`).
2. Install dependencies (see above; use `.venv`).
3. Run migrations: `./scripts/run.sh migrate` or `./scripts/run_local.sh`
4. Start the app: `./scripts/run.sh serve` or `./scripts/run_local.sh` (http://127.0.0.1:8000)

## Run the site locally (one command)
From the project root, with `DATABASE_URL` in `.env` and dependencies installed (e.g. in a venv):
```bash
./scripts/run_local.sh
```
Then open http://127.0.0.1:8000 (landing page redirects to /competitors).

## Runner
Use `./scripts/run.sh` so the project venv and `.env` are used (avoids "python not found" / "No module named psycopg"):

- Run all channels: `./scripts/run.sh` or `./scripts/run.sh all`
- Single channel: `./scripts/run.sh talent`, `./scripts/run.sh asset`, `./scripts/run.sh press`, etc.
- Seed: `./scripts/run.sh seed`
- Serve: `./scripts/run.sh serve`
- Migrate: `./scripts/run.sh migrate`

Or with venv activated: `python -m app.cli --channel all`, `python -m app.cli --channel talent`, etc.  
Weekly digest: `python -m app.cli --digest` (with venv active).

## Update cadence and scheduling
- **Data updated weekly:** Run a full refresh (all channels) once per week (e.g. Sunday) so the dashboard reflects the latest signals.
- **Dossiers refreshed daily:** Run talent and press (and optionally homepage) on other days so dossiers and executive summaries stay current without re-running asset every day.
- **Dashboard:** Shows a “Data last refreshed” timestamp (latest run across any channel) and “Dossiers refreshed daily; full data weekly.”

Example cron (run full refresh Sundays at 02:00; run talent+press daily at 03:00):
```cron
0 2 * * 0 cd /path/to/app && python -m app.cli --channel all
0 3 * * 1-6 cd /path/to/app && python -m app.cli --channel talent && python -m app.cli --channel press
```
Optionally add `python -m app.cli --channel homepage` to the daily run to detect homepage/product page changes. Add a **public_records** source (e.g. USPTO or SEC search URL) per competitor and run `--channel public_records` weekly to pick up trademark/regulatory filings.

**Press:** Only items that pass the executive-relevance filter (e.g. CEO, fundraise, partnership in title) create events, to keep the feed low-noise.

## Asset Collection Notes
- Sitemap-first strategy: try `sitemap.xml` and `sitemap.xml.gz` before HTML parsing.
- `/search` pages are often JS-rendered and may require a headless browser later.

## Seed Competitors
- Run:
  - `python -m app.seed`

## JS Rendering (Playwright)
- Install Playwright browsers after dependencies:
  - `python -m playwright install`

## PDF Export (WeasyPrint)
- WeasyPrint requires system dependencies (Cairo, Pango). Install per OS before using PDF export.

## Health Check
- `GET /health` returns status and app version.

## Deploy/Readiness Checklist
- Configure `DATABASE_URL`
- Run migrations locally first: `./scripts/migrate.sh` (then deploy; Render runs `alembic upgrade head` at startup)
- Seed competitors: `python -m app.seed`
- Enable Playwright if needed: `PLAYWRIGHT_ENABLED=true`
- Install Playwright browsers: `python -m playwright install`
- Run a collector dry run: `python -m app.cli --channel all`

## Tests
- Run: `pytest -q`
