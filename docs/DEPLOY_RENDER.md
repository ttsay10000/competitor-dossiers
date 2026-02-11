# Push to Render: local setup and checklist

What you need **locally** and **on Render** so pushes deploy correctly to the site.

## Reference: Render internal Postgres and API keys

Use these values in the **Render Dashboard** (Environment) for the **web service** and **cron job**. The internal URL is for services running on Render only (not for local `.env`).

**Render internal PostgreSQL (for `DATABASE_URL` on Render):**
```
postgresql://competitor_dossiers_db_user:YqVBo84GBY5hx2so3uuab5t1Lrpcn2PV@dpg-d62lo89r0fns738qpbh0-a/competitor_dossiers_db
```

**OpenAI API key (for `OPENAI_API_KEY` on Render):**  
Set in Dashboard → Service → Environment. Use your key from https://platform.openai.com/api-keys (starts with `sk-`). Required for executive summary, location cleanup, and LLM-based asset/press extraction.

> **Security:** This file contains credentials. Keep the repo private or add `docs/DEPLOY_RENDER.md` to `.gitignore` if you prefer not to commit them. For local `.env`, use the **external** Postgres URL (host `dpg-d62lo89r0fns738qpbh0-a.ohio-postgres.render.com`) so your machine can reach the database.

---

## One-time: Render

1. **Create a Render account** and (if needed) a Postgres database.
2. **Connect your repo** (GitHub/GitLab) to Render — either via **Blueprint** (repo has `render.yaml`) or by adding a Web Service + Cron manually.
3. **Set environment variables on Render** (Dashboard → your Web Service and Cron job, or an Environment Group they share):
   - **`DATABASE_URL`** — required. Use the **internal** URL Render shows for the database (e.g. `postgresql://user:pass@dpg-xxx-a/DATABASE`) so the web and cron can reach it from inside Render.
   - **`OPENAI_API_KEY`** — optional but recommended; needed for executive summary, location cleanup, and LLM-based extraction.
   - **`PLAYWRIGHT_ENABLED`** — already set in `render.yaml` and the Dockerfile for both web and cron; you only need to add it in the Dashboard if you override env (e.g. env group) and want it explicit.

4. **Blueprint (`render.yaml`)** in the repo defines:
   - Web service (Docker, runs migrations then uvicorn)
   - Cron job (Docker, `python -m app.cli --channel all` on a schedule)

Render will use the same branch you connect (usually `main`). Each push to that branch triggers a new deploy.

## Local setup (so you can push and test)

You don’t need any special local env **just to push** — `git push origin main` (or your connected branch) is enough. For a clean deploy and to avoid breaking the site:

1. **Git**
   - Commit and push to the branch Render is watching (e.g. `main`).

2. **Migrations**
   - Run migrations locally first so you don’t push a broken migration:
     ```bash
     ./scripts/run.sh migrate
     ```
   - If that fails, fix before pushing. Render runs `alembic upgrade head` at startup; if it fails, the deploy can fail or the app can start with an old schema.

3. **Optional: `.env` for local dev**
   - Not required for pushing, but needed if you run the app or CLI locally (e.g. `./scripts/run.sh serve` or `./scripts/run.sh all`) and want to use Render’s Postgres or test locally:
     ```bash
     # .env (do not commit; it's in .gitignore)
     DATABASE_URL=postgresql://USER:PASSWORD@dpg-XXXX-a.ohio-postgres.render.com/DATABASE   # external URL for your machine
     PLAYWRIGHT_ENABLED=true
     OPENAI_API_KEY=sk-...   # optional
     ```
   - Use the **external** Postgres URL (with full host like `...ohio-postgres.render.com`) in `.env`; Render services use the internal URL.

4. **Don’t commit**
   - `.env` (secrets)
   - `.venv/`

## Push flow

```bash
# 1. Run migrations locally (catches migration errors before deploy)
./scripts/run.sh migrate

# 2. Commit and push to the branch Render watches (e.g. main)
git add .
git commit -m "Your message"
git push origin main
```

Render will build the Docker image (from the Dockerfile), run migrations at startup, and start the web service. The cron job uses the same image and runs on its schedule.

## After deploy

- **Web:** Your app URL (e.g. `https://competitor-dossiers.onrender.com`). Health: `GET /health` (includes `playwright_enabled`).
- **Cron:** Runs automatically on the schedule in `render.yaml` (e.g. weekly). Check Dashboard → Cron job → Logs.
- **First time:** Seed competitors and sources (Dashboard shell or run `python -m app.seed` locally with `DATABASE_URL` pointing at Render Postgres).

## Summary

| What | Where | Required to push? |
|------|--------|--------------------|
| Git repo connected to Render | Render Dashboard | Yes (one-time) |
| `DATABASE_URL` | Render Dashboard (web + cron) | Yes |
| `OPENAI_API_KEY` | Render Dashboard (optional) | No |
| `PLAYWRIGHT_ENABLED` | In `render.yaml` + Dockerfile | No (already in repo) |
| Run migrations locally before push | Your machine | Recommended |
| `.env` with `DATABASE_URL` | Your machine | Only for local dev against Render DB |
