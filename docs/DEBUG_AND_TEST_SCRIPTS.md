# Debug and test scripts

This document lists all scripts used for internal debugging and testing of asset, press, and talent collectors. They are safe to remove from the codebase if you no longer need them.

---

## Single entry point

**`scripts/internal_tests.py`** — Run any of the tests below via one command:

```bash
python scripts/internal_tests.py <channel> <test> [options]
# Examples:
python scripts/internal_tests.py talent llm-input Lark
python scripts/internal_tests.py asset load-more --save-html
python scripts/internal_tests.py press final-output --no-enrich
python scripts/internal_tests.py dossier
```

Run with no arguments to see the full list of channels and tests.

---

## Talent

| Script | Purpose | Run directly | Via internal_tests |
|--------|---------|--------------|---------------------|
| `debug_talent_llm_input.py` | Inspect exact payload sent to LLM for job classification; rule-based vs post-LLM counts; which jobs end up in "Other". | `python scripts/debug_talent_llm_input.py [competitor]` | `talent llm-input [competitor]` |
| `check_lark_llm_payload.py` | Fetch Lark career page (no DB), show payload that would be sent to LLM. | `python scripts/check_lark_llm_payload.py` | `talent lark-payload` |
| `sample_jobs_llm_payload.py` | Sample job dicts + first lines of LLM payload; empty dept/location counts (from DB). | `python scripts/sample_jobs_llm_payload.py [competitor]` | `talent sample-payload [competitor]` |
| `debug_lark_scroll.py` | Debug Lark infinite scroll: heights, job count per round, scrollable containers; optional `--container` to scroll a specific element. | `python scripts/debug_lark_scroll.py [--save-html] [--container SEL]` | `talent lark-scroll [--save-html] [--container SEL]` |

**Requires:** `.env` (DATABASE_URL for DB-based scripts); OPENAI_API_KEY optional for live enrichment; Playwright for Lark fetch/scroll.

---

## Asset

| Script | Purpose | Run directly | Via internal_tests |
|--------|---------|--------------|---------------------|
| `debug_load_more.py` | Debug "Load more" / "Load more hotels" for Lark portfolio (Playwright). | `PLAYWRIGHT_ENABLED=true python scripts/debug_load_more.py [--url URL] [--save-html]` | `asset load-more [--url URL] [--save-html]` |
| `test_all_asset_collectors.py` | Run Placemakr, AvantStay, Lark asset collectors with step-by-step diagnostics. | `python scripts/test_all_asset_collectors.py` (optional: `COMPETITOR=Name`) | `asset all [competitor]` |
| `test_lark_asset_fetch.py` | Test Lark portfolio fetch (Playwright + load more). | `python scripts/test_lark_asset_fetch.py` | `asset lark` |
| `test_avantstay_asset_fetch.py` | Test AvantStay asset fetch. | `python scripts/test_avantstay_asset_fetch.py` | `asset avantstay` |
| `test_article_fetch.py` | Test fetching full article text for sample URLs (same path as press summarizer). | `python scripts/test_article_fetch.py` | `asset article-fetch` |

**Requires:** Network; Playwright for JS/load-more tests.

---

## Press

| Script | Purpose | Run directly | Via internal_tests |
|--------|---------|--------------|---------------------|
| `test_press_final_output.py` | Full press pipeline (endpoints + Google News + PR Newswire); print final canonical links. | `python scripts/test_press_final_output.py [competitor] [--no-enrich] [--local]` | `press final-output [competitor] [--no-enrich] [--local]` |
| `test_press_compact_dedupe.py` | Test compact dedupe payload sent to LLM. | `python scripts/test_press_compact_dedupe.py [competitor]` | `press compact-dedupe [competitor]` |
| `debug_prnewswire_date.py` | Debug PR Newswire date parsing. | `python scripts/debug_prnewswire_date.py` | `press prnewswire-date` |
| `inspect_google_news_classify.py` | Inspect Google News classification (LLM). | `python scripts/inspect_google_news_classify.py` | `press google-news-classify` |
| `inspect_first_dedupe_and_story_keys.py` | First dedupe + story keys for press (e.g. Lark); clusters and Jaccard merge. | `python scripts/inspect_first_dedupe_and_story_keys.py [competitor]` | *(not in internal_tests)* |
| `inspect_classify_full_list.py` | Full list of collected press items with classification (index, title, outlet, topic). | `python scripts/inspect_classify_full_list.py [competitor]` | *(not in internal_tests)* |

**Requires:** `.env` (DATABASE_URL unless `--local`); OPENAI_API_KEY for enrichment/classification.

---

## Dossier and property/LLM

| Script | Purpose | Run directly | Via internal_tests |
|--------|---------|--------------|---------------------|
| `debug_dossier.py` | Build dossier context and render template; catch exceptions. | `python scripts/debug_dossier.py [competitor_id]` | `dossier` |
| `debug_property_breakdown.py` | Property count vs state breakdown (totals and list). | `python scripts/debug_property_breakdown.py [competitor]` | `property-breakdown [competitor]` |
| `debug_llm_location_clean.py` | Inputs/output of LLM location cleaning for dossier. | `python scripts/debug_llm_location_clean.py [competitor]` | `llm-location-clean [competitor]` |

**Requires:** `.env` (DATABASE_URL); OPENAI_API_KEY for `debug_llm_location_clean`.

---

## Operational scripts (not debug/test)

These are part of normal runs and deployment; keep them unless you change your workflow:

- `run.sh`, `run_local.sh`, `run_final_output.sh` — run the app or pipelines
- `refresh.sh`, `force_refresh_all.sh` — refresh data
- `migrate.sh` — DB migrations
- `setup_venv.sh` — environment setup

---

## Removing internal testing

To remove all debug/test scripts from the codebase:

1. Delete **`scripts/internal_tests.py`**.
2. Delete these scripts (all referenced above):
   - `scripts/check_lark_llm_payload.py`
   - `scripts/debug_dossier.py`
   - `scripts/debug_lark_scroll.py`
   - `scripts/debug_llm_location_clean.py`
   - `scripts/debug_load_more.py`
   - `scripts/debug_prnewswire_date.py`
   - `scripts/debug_property_breakdown.py`
   - `scripts/debug_talent_llm_input.py`
   - `scripts/inspect_classify_full_list.py`
   - `scripts/inspect_first_dedupe_and_story_keys.py`
   - `scripts/inspect_google_news_classify.py`
   - `scripts/sample_jobs_llm_payload.py`
   - `scripts/test_all_asset_collectors.py`
   - `scripts/test_article_fetch.py`
   - `scripts/test_avantstay_asset_fetch.py`
   - `scripts/test_lark_asset_fetch.py`
   - `scripts/test_press_compact_dedupe.py`
   - `scripts/test_press_final_output.py`
3. Optionally delete this doc: **`docs/DEBUG_AND_TEST_SCRIPTS.md`**.

The rest of the app does not depend on these scripts.
