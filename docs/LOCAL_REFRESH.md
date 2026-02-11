# Local refresh (Playwright + Render DB)

You run the collector on your Mac with Playwright so Lark/AvantStay talent (and everything else) gets updated and written to the same Postgres the Render website uses.

## Render: Internal vs external Postgres

- **Internal URL** (Render only): host is `dpg-xxx-a` with no domain. Use this in Render’s Dashboard for the **web service** and **cron** (they run inside Render and resolve that host).
- **External URL** (your Mac, psql, etc.): host must be the full name, e.g. `dpg-xxx-a.ohio-postgres.render.com`. Use this in `.env` when running the app or CLI **locally**.

For local `.env`, always use the **external** form:

```
DATABASE_URL=postgresql://USER:PASSWORD@dpg-XXXX-a.ohio-postgres.render.com/DATABASE
```

(Not the internal form with `@dpg-xxxx-a/DATABASE` — that will fail to resolve from your machine.)

## One-time setup

1. **Create `.env`** in the project root (do not commit it; it’s in `.gitignore`):

   ```
   DATABASE_URL=postgresql://competitor_dossiers_db_user:YOUR_PASSWORD@dpg-d62lo89r0fns738qpbh0-a.ohio-postgres.render.com/competitor_dossiers_db
   PLAYWRIGHT_ENABLED=true
   ```

   Use your real password and the **external** host (`...ohio-postgres.render.com`).

2. **Install Playwright browser once:**  
   `python -m playwright install chromium`

## Re-run every time (manual)

**Refresh everything** (talent, asset, press, homepage, public_records):

```bash
./scripts/refresh.sh
```

or explicitly:

```bash
./scripts/refresh.sh all
```

**Refresh only talent** (faster; good when you only care about job listings):

```bash
./scripts/refresh.sh talent
```

If you don’t use the script, run the same thing by hand:

```bash
cd "/Users/tylertsay/Desktop/AI project - competitor dossiers"
source .venv/bin/activate
export DATABASE_URL="postgresql://..."   # or use .env
export PLAYWRIGHT_ENABLED=true
python -m app.cli --channel all
# or  --channel talent
```

## Run automatically on a schedule (optional)

Your Mac must be on and awake at the scheduled time.

### Option A: cron (weekly Sunday 9am)

```bash
crontab -e
```

Add a line (adjust the path):

```
0 9 * * 0 cd /Users/tylertsay/Desktop/AI\ project\ -\ competitor\ dossiers && ./scripts/refresh.sh all
```

### Option B: macOS Launch Agent (runs when you’re logged in)

1. Create `~/Library/LaunchAgents/com.competitor-dossiers.refresh.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.competitor-dossiers.refresh</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>-lc</string>
    <string>cd "/Users/tylertsay/Desktop/AI project - competitor dossiers" &amp;&amp; source .venv/bin/activate &amp;&amp; source .env 2>/dev/null; export PLAYWRIGHT_ENABLED=true; python -m app.cli --channel all</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Weekday</key>
    <integer>0</integer>
    <key>Hour</key>
    <integer>9</integer>
    <key>Minute</key>
    <integer>0</integer>
  </dict>
</dict>
</plist>
```

2. Load it:  
   `launchctl load ~/Library/LaunchAgents/com.competitor-dossiers.refresh.plist`

To disable:  
`launchctl unload ~/Library/LaunchAgents/com.competitor-dossiers.refresh.plist`

## How to confirm Playwright runs every time

Playwright is required for **Lark** (and **AvantStay** talent) so “Load more” and JS-rendered pages are fully fetched. It must be enabled in the **process** that runs the collectors.

| Trigger | Where it runs | How to ensure Playwright is on |
|--------|----------------|---------------------------------|
| **Refresh data** (dossier) | Same process as the web app | Web server must have `PLAYWRIGHT_ENABLED=true` in its environment when it starts. |
| **Reset baseline & refresh** (dossier) | Same process as the web app | Same as above. |
| **Set baseline for all & refresh** (competitors page) | Same process as the web app | Same as above. |
| **CLI / scripts** (e.g. `./scripts/run.sh asset`, `./scripts/refresh.sh`) | Separate process | `run.sh` and `refresh.sh` default `PLAYWRIGHT_ENABLED=true`; or set it in `.env` (they source `.env`). |
| **Each push (Render)** | Web service + Cron each have their own env | In Render Dashboard: set **`PLAYWRIGHT_ENABLED=true`** for **both** the **Web Service** and the **Cron** job (or in an env group they use). The Docker image already includes Playwright + Chromium. |

**Local (web server):**

- Start the server with `./scripts/run.sh serve` — it sources `.env` and defaults `PLAYWRIGHT_ENABLED=true`, so UI refresh/reset/set-baseline all use Playwright.
- Or put `PLAYWRIGHT_ENABLED=true` in `.env` and start the app any way; the app reads env at startup.

**Confirm:**

- **GET /health** returns `playwright_enabled: true` when the running process has Playwright enabled.  
  Example: `curl -s https://competitor-dossiers.onrender.com/health` or `curl -s http://127.0.0.1:8000/health` — check that `"playwright_enabled": true`.

## Summary

| Goal              | Command / approach                    |
|------------------|----------------------------------------|
| Full refresh     | `./scripts/refresh.sh` or `./scripts/refresh.sh all` |
| Talent only      | `./scripts/refresh.sh talent`          |
| Same thing, no script | `export DATABASE_URL=... PLAYWRIGHT_ENABLED=true; python -m app.cli --channel all` |
| Weekly auto      | cron or Launch Agent (see above)      |
