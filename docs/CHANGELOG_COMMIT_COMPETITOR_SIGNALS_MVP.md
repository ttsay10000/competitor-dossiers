# Changelog: Competitor Signals MVP — Big Feature Commit

This document records all changes included in the single large commit on `feature/competitor-signals-mvp`. Use it for release notes, onboarding, or auditing what was added.

---

## 1. New Signals & Collectors

### Google Reviews (channel: `reviews`)
- **Collector:** `app/collectors/reviews.py` — Fetches Google Place Details (rating, review count, up to 5 reviews) per property; LLM summarizes sentiment. Does not store full review text.
- **Model:** `CompetitorReviewProperty` — User-tracked properties per competitor (place_id, display_name). Table `competitor_review_properties` via migration `0008_competitor_review_properties.py`.
- **Runner:** `run_reviews(competitor_name?)` — Runs reviews channel for competitors that have review properties; snapshot-only (no events from reviews).
- **Config:** `GOOGLE_PLACES_API_KEY` in env for Google Places API.
- **UI:** Edit competitor: add/remove review properties (by place ID or resolve from text). Dossier shows "Property reviews" card from `reviews_minimal`. Executive summary includes review sentiment sample when significant (declining/negative trend).

### Social (Twitter & LinkedIn) (channel: `social`)
- **Collector:** `app/collectors/social.py` — Fetches posts via RSS; Twitter via Nitter-style bridge (profile URL → RSS). No scraping.
- **Diff:** `app/diff/social_diff.py` — Identifies new/removed posts by id for event generation.
- **Rules:** `app/rules/social_rules.py` — Builds `narrative.social_signal` events (med severity) for executive-relevant posts.
- **LLM:** `enrich_social_posts_with_llm` in `llm_structured.py` — Classifies posts as executive-relevant vs promotion.
- **Runner:** `run_social(competitor_name?)` — Runs social channel; creates events for new executive-relevant posts. Dedupe window 30 days for `narrative.social_signal`.
- **Config:** `TWITTER_RSS_BRIDGE_BASE_URL` (e.g. Nitter instance) to turn Twitter profile URL into RSS.
- **UI:** Dossier "Social (Twitter & LinkedIn)" card; events appear in "Recent events" and executive summary with label `[social]`.

### Digital footprint / Homepage (channel: `homepage`) — extended
- **Multi-URL + product paths:** Homepage runner now crawls base URL plus configurable paths (e.g. `/locations`, `/coming-soon`, `/product`, `/features`, `/about`, `/blog`). Uses `build_composite_hash` for change detection across pages.
- **Events:** `narrative.homepage_updated` (existing), `narrative.coming_soon` (new) — detected via phrase rules in `homepage_rules.py` (`detect_coming_soon_phrases`). Dedupe 30 days for `narrative.coming_soon`.
- **Runner:** `_homepage_runs_for_competitor` — Supports explicit homepage endpoints with `extra_options.product_paths` or falls back to `primary_domain` + `COMMON_HOMEPAGE_PATHS`.

---

## 2. Add / Edit Competitor Flow

- **Add competitor:** Form supports multiple talent/asset/press URLs; optional Twitter and LinkedIn URLs; primary domain. "Suggest URLs" from domain (careers, locations, blog, etc.) via `app/validation.py` and `/competitors/suggest-urls`.
- **Validation:** `app/validation.py` — URL format validation (http(s), host), optional reachability (HEAD), `suggest_urls_from_domain` for talent/asset/press path suggestions.
- **Edit competitor:** Edit page includes review properties (add by place ID or search text), and source endpoints. Same URL validation on save.
- **Run-now:** After add or from dossier, user can trigger runs with channel checkboxes: talent, asset, press, homepage, public_records, reviews, social.
- **Export seed:** After add/edit, seed export persists competitor + source_endpoints + review_properties to `seed_data.json` (via `_sync_seed_file` / `export_seed_to_file`).

---

## 3. Executive Summary & Rollup

- **Event priority:** Events in summary are ordered by type: precedent (talent, asset, partner, capital, digital footprint, public_record) first, then secondary (social, review-related). No cap on number of events; sorted by date (newest first) then by priority.
- **Review block:** When at least one review row is "significant" (declining/negative trend or negative sentiment keywords), up to `MAX_REVIEW_PROPERTIES_FOR_SUMMARY` (10) review lines are sent to the LLM.
- **Labels in prompt:** `narrative.social_signal` → `[social]`; other `narrative.*` → `[digital footprint]`.
- **Rollup summary:** New endpoint `GET /competitors/rollup-summary` returns JSON of LLM-generated roll-up of all competitor executive summaries (for competitors page). Uses `get_rollup_summary` and `format_rollup_summary_for_display` in dossier/executive_summary.
- **Caps:** `MAX_NEWS_FOR_SUMMARY = 15`; `MAX_DELTA_BY_CITY_ROWS = 15`; location rows 25. Events uncapped.

---

## 4. Competitors List & Dossier UI

- **Data / Status column:** Displays channels in order T A P W S R (talent, asset, press, homepage=W, social=S, reviews=R). `DISPLAY_CHANNELS` and `CHANNEL_LETTERS` in `routes/competitors.py`.
- **Dossier:** Cards for "Property reviews" (`reviews_minimal`), "Social (Twitter & LinkedIn)" (`social_posts`); "Populate now" checkboxes for talent, asset, press, reviews, social (homepage runs when running all).
- **Recommendations:** `RECOMMENDATIONS_MAP` in dossier includes `narrative.social_signal` for "Consider impact on brand and partnerships."

---

## 5. Config & Models

- **Config:** `google_places_api_key`, `twitter_rss_bridge_base` (from env `GOOGLE_PLACES_API_KEY`, `TWITTER_RSS_BRIDGE_BASE_URL`).
- **Models:** `Competitor.review_properties` → `CompetitorReviewProperty`; migration `0008_competitor_review_properties.py`.

---

## 6. Runner

- **Channels:** `RUNNER_CHANNELS` now includes `reviews` and `social`.
- **Dedupe windows:** `narrative.coming_soon`, `narrative.social_signal` added (30 days).
- **CLI:** `run_reviews`, `run_social`; full run invokes all channels including reviews and social when applicable.

---

## 7. Seed & Data

- **Seed:** `seed.py` — Loads and exports `review_properties` per competitor; new competitors get default asset strategy and min properties.
- **seed_data.json:** Extended with new competitors/sources/review_properties as used in development.

---

## 8. Documentation (new/updated)

- **docs/EXECUTIVE_SUMMARY_LLM_INPUT_PER_SIGNAL.md** — What is sent to the executive summary LLM per signal (talent, asset, press, reviews, events, etc.).
- **docs/RECENT_CHANGES_AND_SIGNAL_FLOW.md** — Recent changes, how signals reach executive summary, caps, event path.
- **docs/REVIEW_LOGIC_AND_UI_CONTAINERS.md** — Where each signal appears on the site; logic flow checks.
- **docs/SCHEMA.md** — Schema updates.
- **docs/SIGNALS.md** — Event taxonomy; added `narrative.social_signal`.

---

## 9. Tests

- **tests/test_talent.py** — Talent collector/structured tests.
- **tests/test_validation.py** — URL validation and suggest_urls_from_domain tests.
- **tests/test_asset_avantstay.py** — Minor updates.

---

## File summary

| Category   | Files |
|-----------|--------|
| New       | `app/collectors/reviews.py`, `app/collectors/social.py`, `app/diff/social_diff.py`, `app/rules/social_rules.py`, `app/validation.py`, `app/migrations/versions/0008_competitor_review_properties.py`, `docs/EXECUTIVE_SUMMARY_LLM_INPUT_PER_SIGNAL.md`, `docs/RECENT_CHANGES_AND_SIGNAL_FLOW.md`, `docs/REVIEW_LOGIC_AND_UI_CONTAINERS.md`, `tests/test_talent.py`, `tests/test_validation.py` |
| Modified  | `app/cli.py`, `app/collectors/asset.py`, `app/collectors/homepage.py`, `app/collectors/talent.py`, `app/config.py`, `app/executive_summary.py`, `app/llm_structured.py`, `app/models.py`, `app/routes/competitors.py`, `app/routes/dossier.py`, `app/rules/homepage_rules.py`, `app/runner.py`, `app/seed.py`, competitor templates, `competitors.html`, `dossier.html`, `docs/SCHEMA.md`, `docs/SIGNALS.md`, `seed_data.json`, `tests/test_asset_avantstay.py` |
