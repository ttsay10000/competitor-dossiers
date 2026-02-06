# Competitor Signals Platform

MVP system for monitoring strategic shifts among flexible-use hospitality competitors.

Initial focus:
- Talent Radar
- Asset Watch
- Press/Public Narrative

Primary users: Executive and Strategy leadership.

## Local Setup (MVP)
1. Create a Postgres database named `competitor_signals`.
2. Install dependencies:
   - `pip install -r requirements.txt`
3. Run migrations:
   - `alembic upgrade head`
4. Start the app:
   - `uvicorn app.main:app --reload`

## Runner
- Run all channels:
  - `python -m app.cli --channel all`
- Run talent only:
  - `python -m app.cli --channel talent`
- Weekly digest:
  - `python -m app.cli --digest`

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
- Run migrations: `alembic upgrade head`
- Seed competitors: `python -m app.seed`
- Enable Playwright if needed: `PLAYWRIGHT_ENABLED=true`
- Install Playwright browsers: `python -m playwright install`
- Run a collector dry run: `python -m app.cli --channel all`

## Tests
- Run: `pytest -q`
