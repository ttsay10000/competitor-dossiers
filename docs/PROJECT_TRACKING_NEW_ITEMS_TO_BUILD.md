# PROJECT TRACKING - NEW ITEMS TO BUILD

A living list of features and improvements for the competitor signals platform. Use this for prioritization and implementation planning.

---

## Product & UX

### Add Competitor (existing → improve)

**Current behavior:** Users add a competitor at `/competitors/new` (name, primary domain, talent/asset/press URLs). The competitor is stored in the DB and `_sync_seed_file()` immediately overwrites `seed_data.json` on the server. To include the new competitor in the repo (and thus on future deploys and other envs), run `python -m app.cli --export-seed` locally with that DB, then commit and push `seed_data.json`.

**Improvements to consider:**

- **In-app "Export seed"** — Button or automatic sync after add/edit so non-technical users don't need to run the CLI; optionally run export in background after save and show "Saved; seed file updated" when done.
- **Validation** — Validate URLs (format, reachability) before save; suggest talent/asset/press URLs from primary domain (e.g. common paths like `/careers`, `/locations`, `/blog`).
- **Onboarding** — After adding a competitor, prompt to run collectors or show "Run data refresh to populate this competitor" with a link to Runs or a one-click "Refresh this competitor" (if we add that).
- **Default strategy for new competitors** — Already supported: new competitors get default asset strategy chain `["sitemap_first", "js_exhaust", "html"]` and min properties. Document this in the Add Competitor form (e.g. "We'll try sitemap, then JS, then HTML automatically").

---

### Summary report (competitors page)

**Goal:** Give Kasa executives a single, scannable view of major competitor news before they drill into individual dossiers.

**What to build:**

- **Location** — On the main competitors page, **above** the list where users select individual competitor dossiers. A single "Recent updates" box that aggregates executive-summary-level insight across all competitors.
- **Data source** — Fetch the (LLM-generated) executive summaries for each competitor (e.g. from the same data that powers `/dossier/{id}/summary`). Run an LLM over all of these to produce one cohesive summary.
- **Layout** — A **static box** (fixed max height so the page doesn’t grow unbounded) that is **scrollable**. Content inside follows the format below.

**Content format (LLM output):**

The LLM reads all competitor executive summaries and outputs:

1. **Recent updates:** — A short lead paragraph (2–3 sentences) of major news across competitors, summarized at executive level. End with the main takeaway or insight. Keep it succinct and direct for how Kasa executives should read this about competitors.
2. **Per-competitor bullets** — A list:
   - **[Competitor 1]:** [1–2 sentence summary of major news from that competitor’s executive summary — e.g. big hires, new partnerships, number of openings and city names.]
   - **[Competitor 2]:** [Same format.]
   - … one bullet per competitor with recent summary data.

**Implementation notes:**

- Reuse or call the same pipeline that generates per-dossier executive summaries; then pass the set of summaries (or their stored text) into a single “roll-up” LLM prompt that produces the formatted blob above.
- Cache the roll-up result (e.g. by “latest summary run” or timestamp) so the competitors page doesn’t re-run the LLM on every load; refresh when new executive summaries are generated (e.g. after digest/summary job).
- If no executive summaries exist yet, show a short message: “Run the summary/digest to generate executive summaries; the roll-up will appear here.”
- Optional: link “Recent updates” or each competitor name to that competitor’s dossier or summary page.

---

## New Signals & Data Sources

### 1. Digital Footprint

**Goal:** Detect meaningful changes to a competitor's web presence that may indicate strategy or product shifts.

**What to track:**

- **Homepage changes** — Already in scope (homepage channel; cache URL, detect meaningful changes). Extend with:
  - Diff of visible text/structure (ignore ads, timestamps, "Today's date") and flag significant changes (new sections, removed sections, copy changes).
- **New feature announcements** — Dedicated product/features pages or "What's new" / changelog; detect new entries or major edits.
- **"Coming Soon" pages** — New URLs or sections that explicitly say "Coming soon," "Launching soon," "Beta," or similar; treat as pipeline / market-entry signals.

**Implementation notes:**

- Reuse or extend existing homepage snapshot + diff logic; add rules to classify diffs as "cosmetic" vs "feature/announcement" (optional LLM pass).
- Crawl known product/feature paths per competitor (configurable in source_endpoints, e.g. `channel: homepage`, `extra_options: { product_paths: ["/features", "/product"] }`).
- "Coming soon" detection: scan for phrases in new or updated pages; emit `narrative.homepage_updated` or a new event type (e.g. `asset.pipeline_signal` or `narrative.coming_soon`).

---

### 2. Market Entry "Soft Launch" Signals

**Goal:** Surface early signals that a competitor is entering or testing new markets before full announcement.

**What to track:**

| Signal | Description | How to track |
|--------|-------------|--------------|
| **"Coming soon" pages** | Dedicated pages by city or region | Crawl known patterns (e.g. `/cities/denver`, `/coming-soon`); overlap with Digital Footprint. |
| **Subdomain launches** | e.g. `denver.competitor.com`, `beta.competitor.com` | Periodic subdomain enumeration (DNS, certificate transparency, or crawl from sitemap/links). |
| **SEO landing pages by city** | City-specific landing pages (e.g. "Short-term rentals in Austin") | Crawl sitemap + HTML; detect URL patterns and title/h1 containing city names; optional LLM to confirm "market" intent. |
| **Sitemap changes** | New URLs or path patterns in sitemap | Diff sitemap snapshots (we already use sitemaps for asset); emit events when new path patterns appear (e.g. new `/cities/*` or new subdomains in URLs). |

**Implementation notes:**

- New channel or extend **asset** / **homepage**: e.g. `channel: soft_launch` with sources for sitemap URL + optional list of subdomains to watch.
- Sitemap diff: store previous sitemap URL list; on run, diff and classify new URLs (by city keyword, "coming soon" text, subdomain).
- Event types: e.g. `asset.pipeline_signal`, `asset.new_market` (if city is confirmed), or new `asset.soft_launch_signal`.

---

### 3. Google Reviews

**Goal:** Track reputation and sentiment at company level and per property (sample), to spot quality or market-specific issues.

**What to track:**

- **Company-level** — Overall Google Business (or similar) rating and review count; track over time; sentiment on recent reviews (LLM or keyword-based).
- **Per-property sample** — For each competitor, up to **top 25** (highest-rated) and **worst 25** (lowest-rated) properties by review score or volume; track rating and sentiment for that sample.

**Implementation notes:**

- Data source: Google Places API / Google Business Profile API (requires API key and possibly Business verification), or scraping (ToS and stability concerns). Alternatives: Trustpilot, Birdeye, or other review aggregators with APIs.
- Schema: New channel e.g. `channel: reviews`; snapshot shape: `{ company: { rating, review_count, sentiment_summary }, property_sample: [ { property_id/name, rating, review_count, sentiment_summary } ] }`.
- Events: e.g. `narrative.review_trend` — significant drop in company or property-sample sentiment; or "new worst-25 property" entering the list (quality risk).

---

### 4. LinkedIn / Social Media Tracking

**Goal:** Use company posts to detect strategic shifts (partnerships, product focus, hiring narrative, market focus) while filtering out pure promotion.

**What to track:**

- **Company posts** — LinkedIn (and optionally other channels) company page posts: new posts, content text, engagement.
- **LLM classification** — For each post (or batch), use an LLM to decide:
  - **Promotion only** — Marketing fluff, generic "we're hiring," seasonal campaigns; do not create executive-level alerts.
  - **Executive-relevant** — New strategy, partnership, market entry, leadership change, product/positioning shift, funding/restructuring hints; create events and optionally surface in digest/dossier.

**Implementation notes:**

- Data source: LinkedIn API (restricted; company posts may require partnership or specific product), or RSS if the company has a blog that mirrors posts; alternatively third-party social listening APIs (e.g. Brandwatch, Sprout, or firehose providers). Scraping LinkedIn is against ToS and fragile.
- Schema: New channel e.g. `channel: social`; snapshot: list of posts with `{ id, platform, text, url, published_at, engagement? }`; store raw and LLM classification (e.g. `relevance: executive|promotion|unknown`).
- Events: Only for posts classified as executive-relevant; event type e.g. `narrative.social_signal` with summary and link.
- Prompt: "Given this company post, classify as either (1) promotion/marketing only — no strategic signal, or (2) executive-relevant — indicates strategy, partnership, market, product, or leadership change. If (2), provide a one-line summary for the executive summary."

---

### 5. Public Filings & Disclosure Notices

**Goal:** Track city, permit, trademark, and other public filings that can indicate new markets, compliance, or strategic moves.

**What to track:**

- **City registration** — Business licenses, short-term rental registrations, or city-specific permits (often disclosed on city or state sites).
- **Permits** — Building, zoning, or operational permits by jurisdiction.
- **Trademark** — New trademark applications or registrations (e.g. USPTO); already partially in scope as `public_record.filing`.
- **Other public filings / disclosure notices** — SEC (if applicable), state filings, or "disclosure notice" pages on the competitor's own site (e.g. regulatory or legal).

**Implementation notes:**

- **Existing:** `public_records` channel and `public_record.filing` event type (SIGNALS.md). Extend source_endpoints to support multiple URLs per competitor (e.g. USPTO search URL, city business license search, state permit portal).
- **Structured sources:** Where APIs exist (e.g. USPTO TESS), use them; otherwise configurable scrape or RSS per jurisdiction. Store normalized fields: `filing_type`, `jurisdiction`, `date`, `summary`, `url`.
- **Dedupe:** Same filing may appear in multiple feeds; dedupe by external_id or (filing_type, jurisdiction, date, summary hash).
- **Events:** Continue using `public_record.filing`; optionally subtype in payload (e.g. `trademark`, `city_registration`, `permit`, `disclosure_notice`) for filtering in Feed and dossier.

---

## Cross-cutting

- **Event taxonomy** — New event types above should be added to `docs/SIGNALS.md` and to the code (rules, severity, persistence gates) so the Feed and digest stay consistent.
- **Dossier / Executive summary** — Any new channel should be represented in the per-competitor dossier and weekly summary (e.g. "New this week: soft launch signals, review sentiment drop, relevant LinkedIn post, new trademark filing").
- **Alerts** — Consider which of these signals should trigger immediate alerts vs. only appear in digest/dossier.

---

## Doc ownership

- **PROJECT_TRACKING_NEW_ITEMS_TO_BUILD.md** — This file; backlog of features and new signals.
- **SIGNALS.md** — Event taxonomy and rules; update when new event types or severity rules are added.
- **POPULATE_SOURCES.md** — How to populate and run each source; update when new channels (reviews, social, soft_launch, expanded public_records) are implemented.
