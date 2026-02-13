# Why Vacasa Refresh Is Slow and How LLM Is Used

## What happens on refresh (Vacasa)

On a full refresh, the runner runs **all channels per competitor** in sequence for Vacasa:

1. **Talent** — Greenhouse fetch → **1 LLM batch** (up to 150 jobs via `enrich_jobs_with_llm`)
2. **Asset** — **Main bottleneck** (see below)
3. **Press** — Google News + PR Newswire → **up to 50 HTTP body fetches** + **2 LLM calls** (classify, then group)
4. **Homepage** — Fetch base + paths; LLM only if a page changed (`interpret_website_change` per changed page)
5. **Public records** — No LLM
6. **Social** — If endpoints exist, **1 LLM call** (`enrich_social_posts_with_llm`)
7. **Reviews** — No LLM (Google Places API only)

---

## Where LLM is used (project-wide)

| Stage | Function | What it does |
|-------|----------|----------------|
| **Asset collection** | `_extract_properties_via_llm` / `_extract_properties_via_llm_from_blocks` | Optional: extract properties from HTML or Lark-style blocks (Vacasa does **not** use this; it uses link extraction only) |
| **Asset enrichment** | `enrich_properties_with_llm` | Assigns state/city to each property in **batches of 150**; capped at **2000 properties** (rest keep URL/rule-derived state only) |
| **Talent** | `enrich_jobs_with_llm` | Functional area + senior/strategic flags; **first 150 jobs** only |
| **Press** | `_classify_press_headlines_with_llm` | One call to classify up to 200 items (topic, relevance); may **fetch up to 50 article bodies** (12s timeout each) first |
| **Press** | `_group_press_into_clusters_llm` | One call to group filtered articles by story |
| **Homepage** | `interpret_website_change` | One call per **changed** page (what changed, is it important) |
| **Social** | `enrich_social_posts_with_llm` | Classify posts (e.g. executive relevance) |
| **Dossier (view time)** | `summarize_top_news_llm`, `clean_location_display_for_dossier` | Top news roll-up and property-by-location display (not part of refresh) |

---

## Why Vacasa is slow

### 1. Asset channel (dominant cost)

- **Source:** `https://www.vacasa.com/search?place=/usa/` with `strategy_chain: ["html"]` (no sitemap, no JS) so one big HTML page is fetched. The comment in `seed.py` notes the search page has **~26k properties in static HTML**.
- **Collection:**  
  - One large HTTP response.  
  - `extract_properties_from_html()` walks **every `<a>`** and runs `is_property_like(href)` (e.g. `/unit/[0-9]+`). So you build **~26k property dicts** (parsing + regex + BeautifulSoup). No cap here.
- **Enrichment:**  
  - `enrich_properties_with_llm()` is called with all ~26k properties.  
  - It **caps at 2000** properties for LLM (`_ENRICH_MAX_PROPERTIES` in `app/llm_structured.py`).  
  - Those 2000 are processed in **batches of 150** → **14 sequential LLM calls** (e.g. gpt-4o-mini, ~3–15s each).  
  - So asset alone is often **~1–3 minutes** of LLM time, plus the cost of building and passing a 26k-item list.

So the main delays are:

- **CPU/memory:** Parsing and holding 26k properties from one HTML page.
- **LLM:** 14 sequential API calls for the first 2000 properties.

### 2. Press channel

- **Classify:** One LLM call for up to 200 items; before that, **up to 50 article body fetches** (sequential, 12s timeout each). For Vacasa this can add tens of seconds to a couple of minutes depending on how many bodies are fetched.
- **Group:** One LLM call after filtering.

### 3. Talent

- Single batch of up to 150 jobs; usually quick.

---

## Vacasa: LLM skipped for asset (implemented)

For **Vacasa only**, the asset channel no longer calls the LLM. The runner uses `enrich_properties_url_and_rules_only()` instead of `enrich_properties_with_llm()`:

- **State from HTML:** The collector already sets `state`/`city`/`market` from link `data-*` attributes (`_merge_location_from_link_and_parents` in the asset collector).
- **State from URL:** Avantstay-style URLs are resolved to state (Vacasa URLs are `/unit/12345` so this rarely applies).
- **State from name/market:** Rule-based inference (e.g. "Beach House in Destin" → Florida, "Condo in Bend" → Oregon) via `_infer_state_from_name_if_missing`.

Location is state-level only; city is kept when present from HTML but not requested from LLM. Dossier and diff still aggregate by state and show "State – N properties (M keys)".

---

## Other options to speed up Vacasa (if needed)

1. **Cap properties earlier for huge HTML-only results**  
   In the asset collector, when strategy is `"html"` and `extract_properties_from_html` returns more than e.g. 2000 (or 3000), keep only the first N. That reduces list size and ensures `enrich_properties_with_llm` never does more than the current 2000-cap batches (or fewer if you lower the cap). Same dossier behavior for “first N” but faster refresh.

2. **Lower `_ENRICH_MAX_PROPERTIES`**  
   In `app/llm_structured.py`, reduce from 2000 to e.g. 1000 or 500. Fewer batches → fewer LLM calls. Properties beyond the cap already keep URL/rule-derived state only.

3. **Reduce press body fetches**  
   In `app/llm_structured.py`, `_CLASSIFY_BODY_MAX_FETCHES = 50` (and timeout 12s). Lower to e.g. 20 to cut worst-case latency; classification still works with titles/snippets for most items.

4. **Add timing logs**  
   In `app/runner.py`, log elapsed time per channel (and optionally per step, e.g. “asset collect” vs “asset enrich”) so you can confirm asset and press dominate for Vacasa.

5. **Skip or limit asset for Vacasa in dev**  
   e.g. Run refresh with `--channel` excluding `asset`, or add a dev-only cap (e.g. max 500 properties) for Vacasa so you can iterate without full 26k + 14 LLM calls.

Implementing (1) and (2) will have the largest impact on Vacasa refresh time while keeping current LLM usage patterns.
