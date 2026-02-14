# Seed flow: UI → DB → file → fallback

This doc describes how competitors added via the UI flow into the seed system and related files.

---

## Two seed sources

| Source | Location | When used |
|--------|----------|-----------|
| **seed_data.json** | Project root | Primary. Used by `load_seed_competitors()` when the file exists and parses. |
| **SEED_COMPETITORS** | `app/seed.py` (Python constant) | Fallback. Used when `seed_data.json` is missing or unreadable (e.g. deploy where file path is wrong, or JSON parse error). |

---

## Why SEED_COMPETITORS was out of sync

- **Export seed** (and `_sync_seed_file`) write DB → `seed_data.json` only.
- **SEED_COMPETITORS** is a separate Python constant; nothing in the app updated it.
- When AKA, Landing, Rove were added (via UI or by editing `seed_data.json`), Export updated the JSON file but not the Python fallback.
- Result: on deploys that use the fallback (file missing), those competitors had no press sources.

---

## Flow: Add competitor from UI

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ 1. User submits form at /competitors/new                                     │
└─────────────────────────────────────────────────────────────────────────────┘
                                        │
                                        ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ 2. POST /competitors → creates Competitor + SourceEndpoint rows in DB        │
│    (name, primary_domain, talent/asset/press URLs, extra_options)            │
└─────────────────────────────────────────────────────────────────────────────┘
                                        │
                                        ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ 3. _sync_seed_file() runs after create/edit/delete                           │
│    → export_seed_to_file()                                                   │
│    → writes DB → seed_data.json                                              │
│    → syncs seed_data.json → SEED_COMPETITORS in seed.py (NEW)                │
└─────────────────────────────────────────────────────────────────────────────┘
                                        │
                                        ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ 4. Commit seed_data.json and app/seed.py; push.                              │
│    Next deploy: Run seed loads from seed_data.json (or fallback if missing)  │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Adjacent files and terms

| File / term | Role |
|-------------|------|
| **seed_data.json** | Canonical export of competitors + sources. Written by Export. Read by Run seed. |
| **app/seed.py** | `load_seed_competitors()`, `run_seed()`, `export_seed_to_file()`, `SEED_COMPETITORS` fallback. |
| **scripts/export_seed_with_env.py** | CLI to export when UI fails (e.g. Render read-only). Uses `DATABASE_URL_EXTERNAL` when set. |
| **scripts/compare_seed_and_db.py** | Compare `seed_data.json` vs DB. Run after Export or Run seed to verify sync. |
| **LOCAL_PRESS_COMPETITORS** | In `app/runner.py`. Fallback when DB is unavailable for `--local --channel press`. (Separate from seed.) |
| **Run seed** | File → DB. Upserts competitors/sources from `seed_data.json` (or `SEED_COMPETITORS`). |
| **Export seed** | DB → file. Overwrites `seed_data.json` and updates `SEED_COMPETITORS` in seed.py. |

---

## Recommended workflow

1. **Add competitor in UI** → DB updated, `_sync_seed_file` runs → `seed_data.json` and `SEED_COMPETITORS` updated.
2. **On Render (read-only disk)**: Export may fail in UI. Run `scripts/export_seed_with_env.py` locally with Render’s external DB URL, then commit `seed_data.json` and `app/seed.py`.
3. **Verify sync**: `python scripts/compare_seed_and_db.py`.
4. **After pulling seed changes**: Run seed (UI or `--seed`) so DB matches the file.
