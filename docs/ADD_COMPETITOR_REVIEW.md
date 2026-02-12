# Add Competitor: Sync (Seed ↔ DB) and Channel Logic Review

This doc confirms that (1) new competitors are synced to both seed and database as intended, and (2) all runner channels and LLM flows apply correctly to every competitor, including competitor-specific options.

---

## 1. New competitors: where they live and how they sync

### Add via UI (`POST /competitors`)

- **DB:** Competitor and `SourceEndpoint` rows are created immediately.
- **Seed file:** `_sync_seed_file()` runs after create (and after every competitor/source edit). It calls `export_seed_to_file()`, which overwrites `seed_data.json` with the full DB contents. So the new competitor is written into `seed_data.json` on the server right away.
- **Persistence across deploys:** The updated `seed_data.json` lives on the server. To get it into the repo (so other envs and future deploys see the new competitor), run locally with the same DB:
  ```bash
  python -m app.cli --export-seed
  ```
  then commit and push `seed_data.json`. See README and `ADD_COMPETITOR_FLOW.md`.

**Conclusion:** UI additions are in both DB and seed file immediately; export + commit is only for repo/deploy persistence.

### Add via seed (edit `seed_data.json`)

- **Seed file:** You add a competitor (and optional sources with `extra_options`) in `seed_data.json`.
- **DB:** They are not in the DB until seed runs:
  - **On Render:** Every deploy runs `python -m app.seed` (see `render.yaml`), which runs `run_seed()` and upserts all competitors/sources from the file into the DB. So a push that includes the updated `seed_data.json` will put the new competitor in the DB on next deploy.
  - **Locally:** Run `python -m app.cli --seed` (or `python -m app.seed`) so `run_seed()` upserts from the file into the DB. If you run channels without running seed first, DB-only runners won’t see the new competitor.

**Conclusion:** Seed-only additions reach the DB on deploy (Render) or when you run `--seed` locally. No automatic “seed → DB” on every CLI run; that’s by design (seed is explicit or deploy-time).

---

## 2. All channels use the DB and apply to every competitor

Every channel runner loads competitors from the **database** only:

- `run_talent`, `run_asset`, `run_press`, `run_homepage`, `run_social`, `run_public_records`, `run_reviews` all do:
  - `session.query(Competitor).order_by(Competitor.name.asc()).all()`
  - then filter by `competitor_name` if provided, or by `is_active` when running “all”.

So any competitor that exists in the DB (either added via UI or after seed run) is included when you run that channel for “all” competitors. No channel uses only `seed_data.json` at runtime.

---

## 3. Competitor-specific options and flows (nothing missing in logic)

| Channel | Competitor-specific behavior | Source of config | Applied for new competitors? |
|--------|------------------------------|-------------------|-------------------------------|
| **Talent** | Provider/parser (e.g. Blueground) inferred from **endpoint URL** (e.g. `theblueground.com`). | URL in `SourceEndpoint` | Yes. No `extra_options` needed; URL drives behavior. |
| **Asset** | Strategy (e.g. `blueground_destinations`, `js_exhaust`), `strategy_chain`, `min_properties_accept`, `load_more`, etc. | `SourceEndpoint.extra_options` | Yes when set. UI add does not set `extra_options` → collector uses default strategy chain (sitemap_first, js_exhaust, html). |
| **Press** | `press_search_name` for Google News / PR Newswire; company domains for filtering. | First press endpoint’s `extra_options.press_search_name`, else `competitor.name`; hardcoded fallback e.g. `"lark"` → `"Lark Hotels"`. | Yes. New UI-added competitor uses name (or fallback) and `competitor.primary_domain` + endpoint URLs for domains. |
| **Homepage (digital footprint)** | Optional `product_paths` per endpoint. | `SourceEndpoint.extra_options.product_paths`; else `COMMON_HOMEPAGE_PATHS`. If no homepage endpoints, uses `competitor.primary_domain` + common paths. | Yes. New competitor without homepage endpoints still gets a run via `primary_domain`. |
| **Social** | Platform (Twitter vs LinkedIn). | `SourceEndpoint.extra_options.platform` or inferred from URL. Add-competitor form sets `platform` for Twitter/LinkedIn. | Yes. |
| **Reviews** | Uses `competitor.review_properties`. | DB `CompetitorReviewProperty` (and seed/export for persistence). | Yes. Competitors with no review properties are skipped (no run); that’s intended. |
| **Public records** | One run per `public_records` endpoint. | `SourceEndpoint` with `channel="public_records"`. | Yes. Competitors with no such endpoint are skipped; intended. |

So:

- **Talent:** Logic is complete; URL-based behavior applies to any new competitor with that URL.
- **Asset:** Full logic applies; `extra_options` are read from DB when present (e.g. after seed). UI-add uses defaults.
- **Press:** Full logic applies; `press_search_name` and company domains are used; fallback covers e.g. Lark.
- **Digital footprint (homepage):** Full logic applies; runs for all active competitors (endpoints or primary_domain).
- **Social / Reviews / Public records:** All apply per competitor; skip when no endpoints/properties is by design.

Executive summary and dossier also use `session.query(Competitor)` (and related models); they always see the same competitor set as the runners.

---

## 4. Optional `extra_options` not in the UI

When adding or editing a **source** in the UI (add source, edit source), the form does **not** expose:

- `press_search_name` (press)
- Asset `strategy` / `strategy_chain` / `load_more` / etc.
- Homepage `product_paths`

So for competitor-specific press search names or asset strategies, you can:

- Add (or edit) the competitor and sources in `seed_data.json` with the right `extra_options`, then run seed (or deploy), or
- Manually update the DB if needed.

Export-seed includes `extra_options`, so once they’re in the DB (e.g. from seed), they persist in `seed_data.json` and survive export/commit.

---

## 5. Scripts and `--local` press

- **`scripts/inspect_press_grouping.py`** and **`scripts/inspect_press_classification.py`** load competitors from **`seed_data.json`** (with a fallback list). They do not read the DB. So they see the same set as in the file; after you run `--export-seed` and commit, they’ll see UI-added competitors.
- **`run_press_local`** (CLI `--local --channel press`): Uses a **hardcoded** list `LOCAL_PRESS_COMPETITORS` in `runner.py`; it does not use the DB or `seed_data.json`. So new competitors added only in DB or seed are not included in `--local` press. Use normal (non-local) press runs for DB competitors.

---

## 6. Troubleshooting: New competitor not appearing in runs or flows

- **Added via seed_data.json but runs don’t include them**  
  Channel runners read from the **database** only. After editing `seed_data.json`, run seed once so the DB is updated:  
  `python -m app.cli --seed`  
  Then run channels (e.g. `--channel all` or “Populate now” from the UI). On Render, seed runs on every deploy, so a push that includes the updated file is enough.

- **Added via UI; “Populate now” runs but no data for this competitor**  
  - Ensure the competitor has at least one **source URL** for the channel you ran (e.g. a talent URL for the talent channel). Channels that need endpoints (talent, asset, press, social, public_records, reviews) skip a competitor when there are no endpoints for that channel; that’s intended.  
  - Check **Runs**: if the competitor/channel shows “success” or “skipped”, the run saw them; if it’s missing, the runner may have filtered them (e.g. no endpoints).  
  - If you see a JSON log line `competitor_not_found`, the name used for the run didn’t match any DB competitor (e.g. typo or wrong env).

- **Seed file not updated after adding in UI**  
  If the “added” page was opened with `?seed_sync=failed`, the server couldn’t write `seed_data.json` (e.g. read-only filesystem). The competitor is still in the DB and all flows use it. Run `python -m app.cli --export-seed` locally with this app’s `DATABASE_URL`, then commit `seed_data.json`, so future deploys and other envs have the competitor.

- **Scripts (inspect_press_*, etc.) don’t list the new competitor**  
  Those scripts load from `seed_data.json` only. After adding in the UI, run `--export-seed` and commit the file so the scripts see the same set.

- **`--local --channel press` doesn’t include new competitors**  
  Local press uses a hardcoded list when the DB isn’t used. Use a normal (non-local) press run so the DB is used and all competitors are included.

---

## 7. Summary

- **Sync:**  
  - UI add → DB + seed file updated immediately on server; use `--export-seed` + commit for repo/deploy.  
  - Seed add → DB updated on deploy (Render) or when you run `--seed` locally.

- **Logic:**  
  All channels and the executive summary/dossier read competitors from the DB. Every channel applies to all active competitors (or those with the relevant endpoints/properties). Competitor-specific options (press search name, asset strategy, homepage paths, social platform) are respected when stored in DB/seed; UI does not yet set press/asset/homepage `extra_options`, but the pipeline logic does not miss them.
