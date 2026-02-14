# Seed file vs database: sync and how to check

## Two directions

| Action | Direction | When to use |
|--------|-----------|-------------|
| **Run seed** | File → DB | You changed `seed_data.json` (e.g. in git) and want the DB to match. Loads file and upserts competitors/sources. Never deletes existing DB rows. |
| **Export seed** | DB → File | You added or edited competitors in the UI and want to save that to the repo. Writes current DB to `seed_data.json`. |

- **Run seed** (file → DB): upserts from file. Does not remove DB-only competitors or sources.
- **Export seed** (DB → file): overwrites the file with the full DB. So the file becomes a snapshot of the DB.

## Which is “latest”?

- After **Run seed**: DB has been updated from the file. File is the source of truth for what was loaded.
- After **Export seed**: File has been updated from the DB. DB is the source of truth for what was exported.
- If you never Export after UI changes: DB can have competitors/sources that are not in the file (e.g. AKA added in UI). The file in git then lags the DB.
- If you never Run seed after changing the file: DB can have old data (e.g. old Rove strategy). The DB then lags the file.

## Where Export runs

- **UI**: Competitors page → “Export seed” button. Also after add/edit/delete competitor, the app calls `_sync_seed_file()` (same as Export) so the file is updated automatically — **unless** the server has a read-only filesystem (e.g. Render). On Render, that auto-sync can fail; the change is still in the DB but not written to the file.
- **CLI**: `python -m app.cli --export-seed` (requires DB connection, no `--local`). Uses `DATABASE_URL` from the environment.
- **Script (for Render external DB)**: From project root, put Render’s **External** Database URL in `.env` as `DATABASE_URL` (see `.env.example`), then run:
  ```bash
  .venv/bin/python scripts/export_seed_with_env.py
  ```
  This loads `.env` and runs export-seed so you can write `seed_data.json` locally while connected to Render’s DB. Commit the updated file.

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
