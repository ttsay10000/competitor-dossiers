# Seed file vs database: sync and how to check

## Two directions

| Action | Direction | When to use |
|--------|-----------|-------------|
| **Run seed** | File → DB | You changed `seed_data.json` (e.g. in git) and want the DB to match. Loads file and upserts competitors/sources. Never deletes existing DB rows. |
| **Export seed** | DB → File | You added or edited competitors in the UI and want to save that to the repo. Writes current DB to `seed_data.json`. |

- **Run seed** (file → DB): upserts from file. Does not remove DB-only competitors or sources.
- **Export seed** (DB → file): overwrites `seed_data.json` with the full DB, and syncs `SEED_COMPETITORS` in `app/seed.py` so the fallback matches. Commit both files.

## Which is “latest”?

- After **Run seed**: DB has been updated from the file. File is the source of truth for what was loaded.
- After **Export seed**: File has been updated from the DB. DB is the source of truth for what was exported.
- If you never Export after UI changes: DB can have competitors/sources that are not in the file (e.g. AKA added in UI). The file in git then lags the DB.
- If you never Run seed after changing the file: DB can have old data (e.g. old Rove strategy). The DB then lags the file.

## Press (Google News): two variables

For press sources, Google News RSS uses two separate values: **Search name (quoted, strict)** and **Keywords (unquoted, broad)**. In the seed file and DB these are `press_search_name` (company name only, e.g. `"Rove"`) and `google_news_search_phrases` (e.g. `["furnished rentals"]`). Only the search name is sent in quotes to Google; keywords are sent unquoted so they match loosely. Set **Search name** to the company name only (e.g. Rove, AKA, Landing); put broader terms in **Keywords**. After changing these in `seed_data.json`, run **Run seed** so the DB gets the update; otherwise the runner keeps using old values and Google News may return 0 results. To fix existing DB rows that still have a long phrase in `press_search_name`, run once: `python scripts/fix_press_search_name_in_db.py` (with `DATABASE_URL` set).

## Where Export runs

- **UI**: Competitors page → “Export seed” button. Also after add/edit/delete competitor, the app calls `_sync_seed_file()` (same as Export) so the file is updated automatically — **unless** the server has a read-only filesystem (e.g. Render). On Render, that auto-sync can fail; the change is still in the DB but not written to the file.
- **CLI**: `python -m app.cli --export-seed` (requires DB connection, no `--local`). Uses `DATABASE_URL` from the environment.
- **Script (for Render external DB)**: From project root, put Render’s **External** Database URL in `.env` as `DATABASE_URL_EXTERNAL` (or `DATABASE_URL`; see `.env.example`), then run:
  ```bash
  .venv/bin/python scripts/export_seed_with_env.py
  ```
  The script uses `DATABASE_URL_EXTERNAL` when set so you can keep `DATABASE_URL` for run-seed (e.g. local or internal). Commit the updated `seed_data.json` and `app/seed.py` (Export syncs the SEED_COMPETITORS fallback).

## Why sync fails on Render (and you don't always see it)

- **Cause**: On Render (and many PaaS), the app runs from a **read-only** copy of the repo. `export_seed_to_file()` writes to `seed_data.json` and `app/seed.py` in the project root. Those paths are inside the deployed filesystem, so `path.write_text(...)` raises (e.g. `PermissionError` or read-only filesystem). The exception is caught in `_sync_seed_file()` and only logged; the HTTP request still returns success, so the UI does not show an error unless we explicitly redirect with `seed_sync=failed`.
- **When you see it**: After **adding** a new competitor, the app redirects to the "added" page with `?seed_sync=failed` when sync fails, and that page shows the "run export locally" note. After **editing** (details, source URLs, press keywords/terms), the redirect now also includes `?seed_sync=failed` when sync fails, and the edit page shows the same "run export locally" banner. So if you change terms or source URLs on Render and see that banner, run `scripts/export_seed_with_env.py` locally and commit the updated seed files.

## How to check that Export is working and file/DB are in sync

1. **Compare file vs DB**
   - Run: `python scripts/compare_seed_and_db.py` (from project root, with `DATABASE_URL` set).
   - Output shows:
     - **Only in file**: competitors in `seed_data.json` that are not in the DB (Run seed would add them).
     - **Only in DB**: competitors in the DB that are not in the file (Export seed would add them to the file).
     - **In both**: for each, whether sources match (channel, url, extra_options). If they differ, it prints “DIFFER” and suggests Export or Run seed.

2. **After clicking Export seed**
   - Run the script again. You should see no “Only in DB” and all “In both” should “match”. If the server filesystem is read-only, Export may have failed; check server logs and run Export locally with that DB’s `DATABASE_URL` if needed.

3. **After Run seed**
   - Run the script. “Only in file” should be empty (all file competitors are in DB). “In both” shows whether each competitor’s sources in the DB now match the file.

## Recommended workflow

- **Seed file in git** = canonical list for deploy and for “reset DB from file”.
- After **adding or editing a competitor in the UI**: click **Export seed** (or run `--export-seed`) so `seed_data.json` is updated, then commit the file. That way the next deploy and anyone else running Run seed gets the same data.
- After **pulling changes** that touch `seed_data.json`: run **Run seed** (UI or `--seed`) so the DB matches the file before running refresh.
