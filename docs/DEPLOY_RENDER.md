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
   - **`DATABASE_URL`** — required. Use the **internal** URL Render shows for the database (e.g. `postgresql://user:pass@dpg-xxx-a/DATABASE`) so the web service can reach it. For the **cron job**, if you see "Connection refused" to the internal host (e.g. `10.x.x.x`), either: (1) set **`CRON_DB_STARTUP_DELAY=5`** on the cron job so the private network is ready before connecting (the app will sleep 5s on Render before DB connect), or (2) set the cron job’s **`DATABASE_URL`** to the **external** Postgres URL (from the Postgres instance’s *External* tab in the Dashboard) so the cron connects over the public endpoint instead of the private network.
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

## Must use Docker (not Python runtime)

Playwright and Chromium are only installed in the **Docker** image (Dockerfile). If your build log shows **"Installing Python version"** and **"pip install -r requirements-render.txt"** with **no** `playwright install` step, Render is using the **Python** runtime, so Playwright is never installed and Lark/AvantStay (and any JS flows) will not work.

**Render does not let you change runtime in the Dashboard** (no “Switch to Docker” in service Settings). Use one of these:

### Option A: Service is managed by a Blueprint

If you created the app from **New → Blueprint** and connected this repo:

1. Go to [Render Dashboard](https://dashboard.render.com) → in the left sidebar click **Blueprints**.
2. Click the Blueprint that owns **competitor-dossiers** (or your app name).
3. On that Blueprint page, click **Manual Sync** (or **Sync**). Your `render.yaml` already has `runtime: docker`; the sync applies it and redeploys with Docker.
4. After the deploy, the build log should show Docker steps and `playwright install --with-deps chromium`.

### Option B: Service was created as “Web Service” (not from Blueprint)

If you used **New → Web Service** and connected the repo, the service is **not** managed by `render.yaml`, so there is no “change to Docker” button. Fastest fix: create a **new** Web Service with Docker, then retire the old one.

1. **Dashboard** → **New** → **Web Service**.
2. Connect the **same repo** (e.g. `ttsay10000/competitor-dossiers`) and **same branch** (e.g. `feature/competitor-signals-mvp` or `main`).
3. On the create form, find the **Language** dropdown (it may default to Python). Open it and select **Docker**. Leave **Dockerfile Path** and **Docker build context** blank — the Dockerfile is in the repo root and Render uses that by default.
4. Set **name** (e.g. `competitor-dossiers`) and add **Environment** variables: `DATABASE_URL`, `OPENAI_API_KEY`, and optionally `PLAYWRIGHT_ENABLED=true` (Dockerfile already sets it).
5. Create the service. Wait for the first Docker build to finish (you should see `playwright install` in the log).
6. Use the new service URL. When ready, delete or suspend the old Python web service from the Dashboard.
7. If you have a **Cron** job that was also Python: create a **New → Cron Job**, connect the same repo, choose **Docker**, set the same env vars, and set **Docker Command** to `python -m app.cli --channel all` (and schedule as in `render.yaml`). Then remove the old cron.

### Option C: Use the Render API

You can change an existing service’s runtime to `docker` via the [Update Service](https://api-docs.render.com/reference/update-service) API (`serviceDetails.runtime`). Use this if you’re comfortable with API calls and want to avoid creating a new service.

## Cron job: daily refresh (step-by-step)

Use this to add a **Cron Job** on Render that runs a full data refresh every day using the same Docker image (Playwright + Chromium). All times are **UTC**.

1. **Dashboard** → **New** → **Cron Job**.
2. **Connect repository** — same repo and branch as your Web Service (e.g. `ttsay10000/competitor-dossiers`, branch `main` or `feature/competitor-signals-mvp`).
3. **Name** — e.g. `competitor-signals-collect`.
4. **Language** — select **Docker**. Leave **Dockerfile Path** and **Docker build context** blank.
5. **Schedule** — cron expression (UTC). Examples:
   - **Every day at 2:00 AM UTC:** `0 2 * * *`
   - Every day at 6:00 AM UTC: `0 6 * * *`
   - Every day at midnight UTC: `0 0 * * *`
6. **Command** — the command that runs inside the container each time:
   ```bash
   python -m app.cli --channel all
   ```
   (This runs talent, asset, press, homepage, and public_records.)
7. **Environment variables** — add the same vars as your Web Service:
   - **`DATABASE_URL`** — use the **internal** Postgres URL (see top of this doc).
   - **`OPENAI_API_KEY`** — same key as the web app (for press/executive summary).
   - **`PLAYWRIGHT_ENABLED`** — `true` (so Lark/AvantStay use Playwright).
8. Click **Create Cron Job**. Render will build the Docker image (same as the web service); the first run happens at the next scheduled time, or you can click **Trigger Run** to test immediately.
9. **Logs** — open the cron job in the Dashboard and use **Logs** (or **Runs**) to see output and any errors. Runs are billed by duration; there is a $1/month minimum per cron job.

To run only one channel (e.g. press), use **Command:** `python -m app.cli --channel press`. For asset only: `python -m app.cli --channel asset`.

## Troubleshooting Playwright on Render

Redeploying reuses **cached Docker layers**. If an earlier build had a failed or partial `playwright install`, that layer can be reused and Chromium may be missing or broken. Do this when Playwright data/flow doesn’t work on the site:

### 1. Clear build cache and redeploy

- In **Render Dashboard** → your **Web Service** → **Manual Deploy** → choose **“Clear build cache & deploy”**.
- Do the same for the **Cron job** if it’s a separate service (or it shares the same image; clearing cache on the web service and redeploying is usually enough).
- This forces a full rebuild so `playwright install --with-deps chromium` runs again.

### 2. Confirm environment (most common fix when “still not enabling Playwright”)

- **Web Service** and **Cron job**: In Render Dashboard → each service → **Environment**, **add** (or edit) **`PLAYWRIGHT_ENABLED`** and set its value to **`true`** (no quotes). Do not leave it blank or set to empty — that can override the Dockerfile and disable Playwright. If you use an **Environment Group**, either set `PLAYWRIGHT_ENABLED=true` there or add it on the service so it overrides the group.
- After changing env, redeploy the Web Service (or trigger a new deploy). For the Cron job, the next run will use the new env.
- **Check at runtime:** Open **Logs** for the web service after a deploy — you should see `[startup] PLAYWRIGHT_ENABLED=True ...`. For a cron run you should see `[cli] PLAYWRIGHT_ENABLED=True`. If you see `False`, the env var is still wrong or overridden.

### 3. Check health and Playwright probe

- **`GET /health`** — should include `"playwright_enabled": true`.
- **`GET /health/playwright`** — tries to launch Chromium once. Returns `playwright_launch: "ok"` if the browser starts, or an error message if it fails (so you can tell “enabled but browser won’t start” from “install failed at build”).

### 4. Check build and runtime logs

- **Build logs**: Search for `playwright install`. You should see Chromium (and deps) installing; any failure there means the image has no working browser.
- **Runtime**: When you trigger a refresh (or when cron runs), check **Logs** for Playwright/Chromium errors (e.g. crash, timeout, missing binary).

### 5. If Chromium still fails at runtime

- Try a **larger instance** (more RAM). The free tier can OOM when launching Chromium.
- The app uses Docker-friendly launch args (`--no-sandbox`, `--disable-dev-shm-usage`, etc.); no app config to clear—troubleshooting is cache, env, and resources above.

## After deploy

- **Web:** Your app URL (e.g. `https://competitor-dossiers.onrender.com`). Health: `GET /health` (includes `playwright_enabled`).
- **Cron:** Runs automatically on the schedule in `render.yaml` (e.g. weekly). Check Dashboard → Cron job → Logs.
- **Cron "Connection refused" to Postgres:** The app waits 5 seconds on Render before connecting (so the private network is ready). To change the delay, set `CRON_DB_STARTUP_DELAY=5` (or `0` to disable) on the cron job. If it still fails, set the cron job’s `DATABASE_URL` to the **external** Postgres URL (Postgres → Connect → External).
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
