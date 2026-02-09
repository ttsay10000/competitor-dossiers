# Local refresh (Playwright + Render DB)

You run the collector on your Mac with Playwright so Lark/AvantStay talent (and everything else) gets updated and written to the same Postgres the Render website uses.

## One-time setup

1. **Create `.env`** in the project root (do not commit it; it’s in `.gitignore`):

   ```
   DATABASE_URL=postgresql://competitor_dossiers_db_user:YOUR_PASSWORD@dpg-d62lo89r0fns738qpbh0-a.ohio-postgres.render.com/competitor_dossiers_db
   PLAYWRIGHT_ENABLED=true
   ```

   Use your real **External** database URL from Render.

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

## Summary

| Goal              | Command / approach                    |
|------------------|----------------------------------------|
| Full refresh     | `./scripts/refresh.sh` or `./scripts/refresh.sh all` |
| Talent only      | `./scripts/refresh.sh talent`          |
| Same thing, no script | `export DATABASE_URL=... PLAYWRIGHT_ENABLED=true; python -m app.cli --channel all` |
| Weekly auto      | cron or Launch Agent (see above)      |
