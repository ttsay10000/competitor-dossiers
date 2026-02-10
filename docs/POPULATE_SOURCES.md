# Populating data: sources and what we pull

Goal: get at least the last week’s information into the system before relying on cron. This doc goes through each source type and what’s needed so data actually shows up.

---

## First run: how to populate the baseline

Do this once so the DB has competitors, sources, and initial snapshots.

1. **Database and schema**
   - Create Postgres DB (e.g. `competitor_signals`) and set `DATABASE_URL` if needed.
   - From project root: `alembic upgrade head`

2. **Seed competitors and sources**
   - `python -m app.seed` (or `python3 -m app.seed`)
   - This creates the competitors and their talent/asset/press URLs from `app/seed.py`.

3. **Run the collector(s)**
   - First run only stores snapshots (no feed events yet; events come from the *next* run when we diff).
   - **Talent only:** `python -m app.cli --channel talent`
   - **All channels:** `python -m app.cli --channel all`
   - Use a venv and the same Python you use for the app (e.g. `source .venv/bin/activate` then `python -m app.cli --channel talent`).

4. **Check**
   - **Runs** (`/runs`): Each competitor/channel should show **success** (or a clear error).
   - **Dossiers**: Open a competitor dossier; "Jobs by function" and other snapshot blocks should show data from that first run.

After that, a **second run** (later the same day or next day) will diff against the first and create events in the Feed (e.g. new senior roles, hiring surges).

---

## Current seeded sources (from `app/seed.py`)

| Competitor  | Talent | Asset | Press |
|------------|--------|--------|-------|
| Placemakr  | jobs.lever.co/placemakr (Lever API) | placemakr.com/locations | placemakr.com/blog |
| AvantStay  | careers.kula.ai/avantstay (Kula; we use JS render when available to get full list) | avantstay.com/search (sitemap_first, js_required) | avantstay.com/blog/ |
| Lark       | ats.wizehire.com/career-site/lark-hospitality (generic; JS + link/heading fallback—see below) | larkhospitality.com/portfolio/ | larkhospitality.com/press/ |

No homepage or public_records sources are seeded (optional; add later if needed).

---

## 1. Talent

**What we pull:** Job listings from each competitor’s talent URL. Priority order:

1. **Lever** — if the URL is a Lever jobs page, we use the Lever API and store all jobs.
2. **Greenhouse** — else if Greenhouse, we use the Greenhouse API and store all jobs.
3. **Ashby** — else if Ashby, we use the Ashby API and store all jobs.
4. **Generic** — else we fetch the career page HTML and scrape job links (or headings as fallback for WizeHire).

**Job listing date (posted_date):** We use the source’s date when available, not the date we pull. **Lever** provides `createdAt` (ms), **Greenhouse** provides `updated_at`/`created_at`, **Ashby** provides `publishedAt`—we store these as `posted_date` and use them for events and for “recent by capability” (e.g. hiring surge). For **generic** (Kula, WizeHire) we have no date from the API; we optionally scrape from the page when present (e.g. `<time datetime="...">`, `data-posted-date`, or “Posted X days ago” text) so listing date can be populated when the ATS exposes it.

**Lark / WizeHire:** The source is harder to pull from because (1) the job list is often JS-rendered, so we use Playwright when `PLAYWRIGHT_ENABLED=true`; (2) the page may use `<a href="#">Job Title</a>` or headings instead of normal job URLs, so we accept fragment links with job-like text and, if that yields 0 jobs, we fall back to extracting titles from `<h2>`/`<h3>`/`<h4>`. Enable Playwright and re-run talent to populate Lark.

If an API fails (e.g. wrong format), we fall back to generic HTML for that URL. Every run persists the **full current job list** for that source so the next run can diff and detect changes.

**What creates events:** Only when we *diff* two runs:

- New **senior** roles (e.g. Chief, VP, Head) → `talent.senior_hire_or_role_posted`
- New **strategic** roles (e.g. strategy, corp dev, data) → `talent.strategic_role_posted`
- **New capability** (first time we see a bucket like ai_data, strategy_finance) → `talent.new_capability`
- **Hiring surge** (e.g. +5 in a capability in 30 days) → `talent.hiring_surge`

So:

- **First run:** We only store a **snapshot** (jobs in DB). No events yet (no “previous” to diff against).
- **Second run (e.g. next day or after a small change):** We diff; new senior/strategic jobs and new capabilities create events.

**Run today to populate all current jobs:**

1. Run talent: `python -m app.cli --channel talent` (or trigger the Cron Job with that command). This run stores **all current jobs** for each competitor in the backend (one snapshot per competitor talent source).
2. In the app, open **Runs**: confirm each competitor’s talent run is **success**. The log may show `added_jobs` (relevant when comparing to a previous run). On the first run we’re just establishing the baseline.
3. Run talent again later (e.g. next day or weekly). The second run diffs against the first; new senior/strategic roles, new capabilities, and hiring surges will create events in the Feed.
4. If a run fails or returns 0 jobs for a URL, check that the URL is correct. For Lever/Greenhouse/Ashby we use their APIs; for other pages we need links whose href contains one of: job, career, position, opening, role, career-site. **AvantStay** talent source is `https://careers.kula.ai/avantstay`; we fetch it with JS (Playwright) when `PLAYWRIGHT_ENABLED=true` so the full job list is captured. If you see 0 jobs, enable Playwright or check Runs for errors.

**Render: Lark / AvantStay talent (Docker)**

The repo’s **Dockerfile** installs Playwright and Chromium (with system deps) so the app and cron can fetch JS-rendered career pages. Use it on Render by setting **runtime: docker** for the web service and cron job (see `render.yaml`).

1. **Blueprint:** The included `render.yaml` uses `runtime: docker` for both the web service and the cron job. If you created the services with `runtime: python` originally, you may need to add new services from the Blueprint or recreate them so they use Docker.
2. **Environment:** Set **`PLAYWRIGHT_ENABLED=true`** for both the web service and the cron job (Dashboard → Service → Environment, or an env group).
3. After deploy, the cron run will use Playwright for Lark and AvantStay; talent data should appear in dossiers.

If you cannot use Docker on Render (e.g. you must keep `runtime: python`), backfill Lark/AvantStay talent from your machine: set `PLAYWRIGHT_ENABLED=true`, run `python -m app.cli --channel talent` with `DATABASE_URL` pointing at your Render Postgres.

---

## 2. Asset (properties / footprint)

**What we pull:** List of properties or location-like URLs.

### Generic flow: strategy chain and minimum threshold

The asset collector supports a **generic flow** so each source can try several strategies in order and only “accept” a result that meets a minimum property count. That avoids “succeeding” with the wrong process (e.g. accepting 6 junk links from HTML when sitemap would have returned 500).

- **`strategy_chain`** (optional): list of strategy names to try in order, e.g. `["sitemap_first", "html"]` or `["js_exhaust", "sitemap_first", "html"]`. If present, we run each strategy in turn.
- **`min_properties_accept`** (optional, default 5): we only **accept** a result and stop if it has at least this many properties. If a strategy returns fewer, we try the next in the chain. If all return fewer, we return the last result (best effort).
- **New competitors (name + asset URL only):** If you add a competitor with **no** `strategy_chain` and **no** explicit `strategy` in `extra_options`, the system uses a **default discovery chain**: `["sitemap_first", "js_exhaust", "html"]`. It tries sitemap first (works for many property/vacation-rental sites like AvantStay), then JS exhaust with “Load more” (Lark-style), then HTML. We only accept when one method returns ≥ `min_properties_accept` (default 5), so we **never** falsely “succeed” with 0–4 properties from HTML. You don’t have to guess which method fits—the collector tries all three and uses the first that returns enough properties.
- **Explicit config:** If `strategy_chain` is set, we use it. If `strategy` is set (and no chain), we use that single strategy (legacy behavior).

### How each competitor’s asset process works

| Competitor  | Source URL                    | Chain / strategy        | How properties are discovered |
|-------------|-------------------------------|--------------------------|------------------------------|
| **Placemakr** | placemakr.com/locations     | `["html"]`, min 1       | One strategy: fetch HTML, scrape property-like links. Fast, no discovery. |
| **AvantStay**  | avantstay.com/search        | Chain: `["sitemap_first", "html"]`, min 5 | Try sitemap first; if ≥ 5, accept. Else HTML (search page + optional LLM). Works without Playwright. |
| **Lark**      | larkhospitality.com/portfolio/ | `strategy: "js_exhaust"` | Single strategy: Playwright + Load more + Lark blocks (~69 properties). No chain fallback. |
| **Any new competitor** | (your URL)              | Default chain: `["sitemap_first", "js_exhaust", "html"]`, min 5 | No config needed: we try sitemap → js_exhaust → html and accept the first result with ≥ 5 properties. |

**Why a single global order (e.g. html → js → sitemap) is not used:** For AvantStay, HTML of the search page can return a handful of links; we’d wrongly “succeed” and never try sitemap. So the **order is per source** when you set a chain; for **unknown** sources the default chain tries sitemap first, then JS, then HTML.

- **Sitemap:** If the source has `use_sitemap_first`, we try `sitemap.xml` (and `.gz`) and take URLs that look like properties (e.g. contain `/locations/`, `/properties/`, `/search`, or AvantStay-style `/{id}/{destination}/{slug}`).
- **HTML:** We scrape `<a>` links whose `href` matches property-like paths (e.g. `/locations/`, `/portfolio/`, `/properties/`).
- **JS / Load more (e.g. Lark):** For `strategy: "js_exhaust"` we use Playwright to load the page and click “Load more” until the list is exhausted, then we have full HTML. If `llm_extract: true`, we run an LLM over that HTML to extract property names, URLs, and **location (state/city)** from the page in one pass (card text, subheadings, addresses). So for Lark we no longer rely only on link text; the LLM reads the visible card content and any location metadata.
- **Where the scraper is limited (if you see too few properties):**
  - **Load more:** For JS exhaust (e.g. Lark), the number of "Load more" clicks is set in the source’s `extra_options.load_more.max_clicks` (default 50 in `exhaust_list_in_browser`; Lark seed uses 200). If the button disappears after the first batch, we stop (`stop_when_selector_gone`). Check that the button selector matches the site (e.g. "Load more", "View more").
  - **Block text truncation:** Lark-style extraction used to send all blocks in one go and truncated at 14k characters, so only the first ~15–20 blocks reached the LLM. This is now fixed by processing blocks in batches of 80 with no truncation per batch.
  - **Enricher:** Only the first 100 properties get LLM-enriched state/city when using the list-only path; for Lark we set state/city/details in the collector so this cap does not apply.

- **Location metadata:** The text we send to the LLM includes both body text and common **data attributes** that often hold location on cards: `data-city`, `data-state`, `data-region`, `data-market`, `data-location`, `data-address`, `aria-label`. If the site stores “Denver, CO” in e.g. `data-market` on each card, we surface that so the LLM can assign state/city. If location is only in visible card text (e.g. a subtitle under the property name), the LLM uses that. If you still see “Other”, the location may be in a different attribute or in JS-rendered content that isn’t in the HTML we capture—inspect the card markup (e.g. DevTools → Elements) and we can add that attribute to the list.

**What creates events:** Again, only when we have a *previous* snapshot to diff:

- **New market** (new market name in properties) → `asset.new_market`
- **Market exit** (market disappeared) → `asset.market_exit`
- **Pipeline signal** (e.g. “coming soon” or similar) → `asset.pipeline_signal`

So:

- **First run:** Only snapshot (list of properties). No events.
- **Second run:** Diff produces new_market / market_exit / pipeline_signal events if the data changed.

**What to do:**

1. Run asset only: `python -m app.cli --channel asset`.
2. In **Runs**, confirm success and that we’re not erroring on a given URL.
3. AvantStay uses sitemap-first (no Playwright required); Lark uses js_exhaust and needs Playwright for full “Load more” + block extraction. If Lark returns 0 properties, ensure `PLAYWRIGHT_ENABLED=true` and re-run asset.
4. **Lark and AvantStay in the same process:** Running asset for all competitors in one go can sometimes cause Lark to fail or return 0 properties (e.g. Playwright/memory when AvantStay's run precedes Lark). Lark is configured with a **single** strategy (`strategy: "js_exhaust"`) only—no `strategy_chain`—so it never falls back to sitemap/html. If Lark is flaky when run with others, run asset in **separate processes** per competitor: `./scripts/run.sh asset Placemakr`, `./scripts/run.sh asset AvantStay`, `./scripts/run.sh asset Lark`.
5. Run asset at least twice (e.g. once now, once after a day) so diffs can produce events.

---

## 3. Press (blog / press / RSS)

**What we pull:**

- **RSS:** If the URL ends in `.xml` or contains `rss`, we parse the feed and get items (title, link, date).
- **HTML:** We fetch the page and collect `<a>` links whose `href` contains `press`, `blog`, or `news`. So `/blog` and `/press` pages can yield items.

**What creates events:** Only for **new** items (vs previous snapshot) **and** only if the item is “executive relevant”:

- Title must contain at least one of: ceo, cfo, fundraise, partnership, expansion, launch, strategic, etc. (see `press_rules.EXECUTIVE_RELEVANCE_HINTS`).
- Then we classify as partner / capital / narrative and create one event per new, relevant item.

So:

- Many blog posts will **not** create events (by design, to reduce noise).
- First run: snapshot only. Second run: new items that pass the executive filter create events.

**What to do:**

1. Run press only: `python -m app.cli --channel press`.
2. In **Runs**, confirm success; check if any items were added (e.g. in logs or in the snapshot).
3. If you want **more** events for the first week (e.g. for testing), we can temporarily relax the filter (e.g. create events for all new items, or add more keywords). Otherwise, keep the filter and run press twice so new, high-signal items create events.

---

## Piecemeal plan to get “at least last week” of data

1. **Confirm seed and one full run**
   - DB has competitors and source endpoints (run `python -m app.seed` if not already).
   - Run **all** channels once: `python -m app.cli --channel all`.
   - In the app: **Runs** tab — every row should show success (or a clear error). Note any URLs that error.

2. **Talent**
   - Run talent again: `python -m app.cli --channel talent`.
   - Check **Runs** for `added_jobs`. Check **Feed** (and dossiers) for talent events (senior/strategic/new capability/surge).
   - If a talent URL never returns jobs, fix the URL or the collector for that provider (see Talent section above).

3. **Asset**
   - Run asset again: `python -m app.cli --channel asset`.
   - Check **Runs** and **Feed** for asset events (new_market, market_exit, pipeline).
   - If a source returns no properties, try sitemap URL directly or add a better selector/URL.

4. **Press**
   - Run press again: `python -m app.cli --channel press`.
   - Check **Runs** and **Feed** for press events (partner, capital, narrative).
   - If you see no press events but runs succeed, items may not match the executive filter; optionally relax it or add keywords (see Press section above).

5. **Cron**
   - Once Runs and Feed look good for all three channels, set the Cron Job schedule (e.g. daily) so “last week” stays populated going forward.

---

## Cron: do you need a separate job per channel?

**No.** You can use a single cron job that runs one command, or several cron jobs with different schedules.

- **One cron, all channels:** e.g. daily at 02:00 run `python -m app.cli --channel all`. That one command runs talent, asset, press, homepage, and public_records in sequence.
- **Multiple crons, different schedules:** e.g. one cron daily for `--channel talent`, another weekly for `--channel all`. Each cron job is still just one command; you choose which channel(s) and how often.

So you do **not** need a separate cron job per data repository. Use one cron that runs `--channel all`, or split by schedule (e.g. talent daily, full refresh weekly) with one command per cron run.

---

## How to check what is being pulled into each dataset

**In the app**

- **Dossier** (e.g. `/dossier/1`): Easiest place to see that data arrived.
  - **Talent:** “Latest Talent Snapshot” shows total roles; **“Jobs by function”** lists each department with counts and senior positions.
  - **Asset:** “Footprint” lists markets (parsed from properties).
  - **Press:** “Latest Press Snapshot” shows item count; individual items appear in events when they’re new and pass the executive filter.
- **Runs** (`/runs`): Confirms each competitor/channel succeeded or failed; does not show the actual payload.

**In the database**

All pulled data is stored in the **snapshots** table. Each row has `competitor_id`, `channel`, `captured_at`, and **structured_json** (the parsed payload). Shapes:

| Channel | Key in structured_json | Contents |
|--------|------------------------|----------|
| talent | `jobs` | List of `{title, dept, location, url, posted_date, ...}` (and flags like `is_senior` after processing). |
| asset  | `properties` | List of `{name, url, status, ...}`; markets are derived from these. |
| press  | `items` | List of `{title, link, published, summary, ...}`. |

To inspect the latest snapshot for a competitor/channel (e.g. competitor id 1, talent):

```bash
# From project root with venv active and DATABASE_URL set
python3 -c "
from app.db import get_session
from app.models import Snapshot
with get_session() as s:
    row = s.query(Snapshot).filter(Snapshot.competitor_id == 1, Snapshot.channel == 'talent').order_by(Snapshot.captured_at.desc()).first()
    if row and row.structured_json:
        import json
        print(json.dumps(row.structured_json, indent=2)[:4000])
    else:
        print('No snapshot')
"
```

Use `channel == 'asset'` or `'press'` to see properties or press items. For a quick count: `len(row.structured_json.get('jobs', []))` (talent), or `'properties'` / `'items'` for asset/press.

---

## Quick checks in the app

- **Runs** (`/runs`): Status per competitor/channel; “success” and optional `added_jobs` / `added_items` in the log.
- **Feed** (`/feed`): Events from talent, asset, press. If empty, either we only have one snapshot (no diff yet) or events are filtered out (e.g. press executive filter).
- **Dossier** (e.g. `/dossier/1`): Snapshot-derived content (markets, capabilities, recent events) for that competitor.

If you tell me which channel (talent / asset / press) you want to prioritize first, we can adjust that collector or rules next (e.g. ensure Placemakr/Lark/AvantStay talent URLs return jobs, or relax press filter for initial backfill).
