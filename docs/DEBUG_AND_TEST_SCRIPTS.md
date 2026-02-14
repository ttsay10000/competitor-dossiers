# Debug and Test Scripts

Index of all scripts in `scripts/` and `tests/`. These are for local development and debugging only—not deployed to Render (`scripts/` is in `.dockerignore`).

---

## How to run scripts (from a blank terminal)

1. **Open a terminal** and go to the project root:
   ```bash
   cd "/Users/tylertsay/Desktop/AI project - competitor dossiers"
   ```

2. **Create a virtualenv and install dependencies** (once per machine):
   ```bash
   ./scripts/setup_venv.sh
   ```
   If `.venv` already exists, use the project's Python (e.g. `.venv/bin/python` or `source .venv/bin/activate`).

3. **Add a `.env` file** in the project root with at least:
   - `DATABASE_URL` — for run/refresh/migrate and DB-backed scripts
   - `OPENAI_API_KEY` — for LLM-backed features (press classification, asset location, talent enrichment)
   - `PLAYWRIGHT_ENABLED=true` — for JS-rendered sources (Lark, Kasa, Rove, Blueground, LinkedIn, etc.)

4. **Run the script you need** from the project root.

---

## Operational scripts (`scripts/`)

| Script | Purpose |
|--------|---------|
| `run.sh` | Run the app |
| `run_local.sh` | Run the app locally |
| `refresh.sh` | Refresh data |
| `force_refresh_all.sh` | Force full refresh |
| `migrate.sh` | DB migrations (Alembic; for local) |
| `setup_venv.sh` | Set up Python virtualenv |

---

## Data & seed scripts

| Script | Purpose | Requires |
|--------|---------|----------|
| `run_seed_with_env.py` | Run seed (file → DB) using `DATABASE_URL` from `.env`. Use internal URL on Render; locally use localhost or Render External. | `DATABASE_URL` |
| `export_seed_with_env.py` | Export DB → `seed_data.json`. Uses `DATABASE_URL_EXTERNAL` if set (e.g. Render External), else `DATABASE_URL`. Use when Export seed fails in UI (e.g. Render read-only). | `DATABASE_URL_EXTERNAL` or `DATABASE_URL` |
| `compare_seed_and_db.py` | Compare `seed_data.json` vs database (competitors + sources). Verify export/import parity. | `DATABASE_URL` |
| `run_asset_counts.py` | Run asset collection per competitor from seed; write results to `asset_count_results.json`. Vacasa and Blueground run last. | Network; `PLAYWRIGHT_ENABLED=true` for JS sources |

**Usage examples:**
```bash
.venv/bin/python scripts/run_seed_with_env.py      # seed locally using .env DATABASE_URL
.venv/bin/python scripts/export_seed_with_env.py
.venv/bin/python scripts/compare_seed_and_db.py
python scripts/run_asset_counts.py --competitor "AKA"
python scripts/run_asset_counts.py --summary-only   # Print table from existing results, no network
```

---

## Asset test scripts (per competitor / strategy)

| Script | Purpose | Requires |
|--------|---------|----------|
| `test_aka_asset.py` | Test AKA asset: sitemap_first vs HTML-only; show property count, sample URLs, location inference. | Network |
| `test_blueground.py` | Unit tests + optional fast asset fetch (2 destinations, no DB). | Network + Playwright for fetch |
| `test_vacasa_asset.py` | Test Vacasa: sitemap check, HTML-only vs sitemap_first; why sitemap might return 0. | Network |
| `test_kasa_asset.py` | One-off Kasa Living asset pull (~77–90 properties); JS exhaust with load-more. | `PLAYWRIGHT_ENABLED=true` |
| `test_kasa_asset_quick.py` | Quick Kasa parser test: HTTP fetch (no Playwright) + `_extract_kasa_locations_html`. | Network (requests only) |
| `test_rove_asset.py` | Test Rove: sitemap fetch + `collect_asset_snapshot`; Rove /listing/ URLs. | Network |
| `test_press_placemakr.py` | Test press collection for Placemakr: Google News + PR Newswire. No DB or OpenAI. | Network |
| `test_asset_strategy_per_competitor.py` | Print which asset strategy runs per competitor (canonical map). No network. | — |
| `test_asset_location_inference.py` | Test non-LLM location/state inference for Vacasa and Blueground (sample props, no network). | — |

**Usage examples:**
```bash
python scripts/test_aka_asset.py
python scripts/test_blueground.py              # unit + fast fetch
python scripts/test_blueground.py --unit-only  # unit only
python scripts/test_vacasa_asset.py
PLAYWRIGHT_ENABLED=true python scripts/test_kasa_asset.py
python scripts/test_kasa_asset_quick.py
python scripts/test_rove_asset.py
python scripts/test_press_placemakr.py
python scripts/test_asset_strategy_per_competitor.py
python scripts/test_asset_location_inference.py
```

---

## Asset debug & trace scripts

| Script | Purpose | Requires |
|--------|---------|----------|
| `debug_blueground_asset.py` | Trace Blueground asset build step-by-step (slug parse, USA links, LLM enrichment). See `docs/BLUEGROUND_ASSET_FLOW.md`. | `PLAYWRIGHT_ENABLED=true` |
| `trace_aka_asset.py` | Trace AKA asset step-by-step: strategy chain, sitemap URLs, HTML links, Lark blocks. Find why 0 properties. | Network |
| `inspect_aka_asset_raw.py` | Print raw AKA asset data BEFORE LLM enrichment. DB or `--url`. | Network; DB optional |

**Usage examples:**
```bash
PLAYWRIGHT_ENABLED=true python scripts/debug_blueground_asset.py
.venv/bin/python scripts/trace_aka_asset.py
python scripts/inspect_aka_asset_raw.py --competitor AKA
python scripts/inspect_aka_asset_raw.py --url "https://example.com/locations" [--js] [--sitemap-first]
```

---

## Press inspect scripts

| Script | Purpose | Requires |
|--------|---------|----------|
| `inspect_press_classification.py` | Full list of press articles and what is marked irrelevant (or promo / not_about_company) vs included. | `OPENAI_API_KEY` |
| `inspect_press_grouping.py` | Run full press pipeline (classify + filter + grouping); print resulting groups. | `OPENAI_API_KEY` |

**Usage examples:**
```bash
python3 scripts/inspect_press_classification.py
python3 scripts/inspect_press_classification.py Lark
python3 scripts/inspect_press_classification.py AvantStay
python3 scripts/inspect_press_grouping.py Lark
python3 scripts/inspect_press_grouping.py Blueground
```

---

## Talent inspect script

| Script | Purpose | Requires |
|--------|---------|----------|
| `inspect_talent_titles.py` | Fetch talent data and print job titles (and dept, location) before LLM enrichment. | `PLAYWRIGHT_ENABLED=true` for JS careers |

**Usage examples:**
```bash
python3 scripts/inspect_talent_titles.py              # all competitors
python3 scripts/inspect_talent_titles.py Placemakr   # one competitor
```

---

## LinkedIn scripts (social channel)

| Script | Purpose | Requires |
|--------|---------|----------|
| `save_linkedin_session.py` | Open browser; you log into LinkedIn manually. Saves session to `linkedin_state.json`. | Playwright |
| `test_linkedin_blueground.py` | Test LinkedIn collector for Blueground. | Playwright; `LINKEDIN_STORAGE_STATE_PATH` (from save_linkedin_session) |

**Usage examples:**
```bash
python3 -m scripts.save_linkedin_session
export LINKEDIN_STORAGE_STATE_PATH=./linkedin_state.json
python3 -m scripts.test_linkedin_blueground
```

---

## Test suite (`tests/`)

Run all tests:
```bash
pytest -q
```

| Test file | Purpose |
|-----------|---------|
| `test_asset_landing.py` | Landing asset collector |
| `test_asset_avantstay.py` | AvantStay asset collector |
| `test_blueground.py` | Blueground unit tests |
| `test_diff.py` | Diff logic (asset, press, talent, etc.) |
| `test_dossier.py` | Dossier routes/context |
| `test_homepage.py` | Homepage collector |
| `test_runner.py` | Runner pipeline |
| `test_rules.py` | Event rules |
| `test_talent.py` | Talent collector/structured |
| `test_validation.py` | URL validation, suggest_urls_from_domain |

---

The web service and cron on Render use only the `app` package (`uvicorn app.main:app`, `python -m app.cli`).
