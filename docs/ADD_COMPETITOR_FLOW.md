# Add Competitor: Flow, Storage, and Activation

This doc describes the end-to-end flow when adding a competitor from the UI, where data lives, and how activation and population work.

---

## Current flow (when activated from UI)

### 1. Create

- **Route:** User submits form at `/competitors/new` → `POST /competitors`
- **Action:** Creates `Competitor` and `SourceEndpoint` rows in the DB
- **Data:** Name, primary domain, talent/asset/press URLs are stored

### 2. Storage

- **Primary:** All data in Postgres (`competitors`, `source_endpoints`)
- **Persistence:** `_sync_seed_file()` writes to `seed_data.json` after any competitor/source change, so UI additions survive redeploys when you commit and push the file

### 3. Redirect

- User is sent to `/competitors/{id}/added` with a “Populate this competitor now” button
- This page summarizes what was saved and provides next steps

### 4. Populate

- **Route:** `POST /competitors/{id}/run-now` starts a background thread
- **Action:** Runs `run(competitor_name=name)` for all channels (talent, asset, press, homepage, public_records)
- **Optional:** Query param or form `channels=talent,asset,press` runs only the selected channels

### 5. Dossier

- User can open `/dossier/{id}` immediately
- Sections are empty until collectors finish
- When no snapshots exist: shows “No data yet—run collection” with a “Populate now” action

---

## Roles of core entities

| Entity           | Role                                                                 |
|------------------|----------------------------------------------------------------------|
| **competitors**  | One row per tracked competitor; name, primary_domain, is_active      |
| **source_endpoints** | URLs per competitor per channel (talent, asset, press)         |
| **snapshots**    | Per-competitor, per-channel captures; raw content + structured_json  |
| **run_logs**     | Per-competitor, per-channel run status (success, error, skipped)     |
| **seed_data.json** | Export of competitors + sources; used for persistence across deploys |

---

## Activation and status

- **is_active:** Competitors with `is_active=false` are excluded from cron/global refresh. Default: active for new competitors.
- **has_snapshots:** Per channel (talent, asset, press): whether at least one snapshot exists for that competitor/channel.
- **Last run status:** From `run_logs`; shown per channel on competitor list and dossier (success/error/pending).

---

## Adding via seed_data.json (instead of UI)

If you add a competitor (and sources) by editing `seed_data.json`:

- **Render:** On each deploy, `python -m app.seed` runs, so the file is upserted into the DB. Push the updated file and the next deploy will add the competitor to the DB.
- **Local:** Run `python -m app.cli --seed` (or `python -m app.seed`) so the new competitor is in the DB before running channels. Otherwise channel runners won’t see them (they read from the DB only).

See **ADD_COMPETITOR_REVIEW.md** for full sync (seed ↔ DB), channel logic, and troubleshooting (e.g. new competitor not appearing in runs).

---

## Channel-level run control

- **Populate now** can run all channels (default) or only selected ones.
- Use form fields or query `?channels=talent,asset` to run a first pass per channel.
- Each channel runs independently; status is logged per channel in `run_logs`.
