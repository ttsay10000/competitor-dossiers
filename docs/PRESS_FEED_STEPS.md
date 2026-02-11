# Press feed pipeline — steps

End-to-end flow from collection to what appears on the dossier.

---

## 1. Collection (runner: `run_press`)

| Step | What | Where |
|------|------|--------|
| **1.1** | **User press endpoints** — Fetch each competitor’s configured press URLs (e.g. company news page). Parse feed/HTML and collect items (title, url, date, source). Provider: `press_endpoint`. | `runner.run_press` → `collect_press_snapshot(endpoint.url)` |
| **1.2** | **Google News** — RSS for quoted company name, first word, and “company partnership”. 90-day window. Merge and dedupe by URL. Provider: `google_news`. | `collect_google_news_items(press_search_name, window_days=90)` in `global_press` |
| **1.3** | **PR Newswire** — Search by company name, parse result links (or Playwright if JS-rendered). 90-day window. Provider: `prnewswire`. | `collect_prnewswire_items(press_search_name, window_days=90)` in `global_press` |
| **1.4** | **Raw total** — All items from 1.1–1.3; counts by provider logged. | `runner.run_press` |
| **1.5** | **90-day + cap** — Drop items older than 90 days; cap at `press_max_raw_items_per_competitor` (e.g. 120). | `runner.run_press` |

**Incremental skip:** If there are no *new* URLs vs last snapshot, Steps 2–5 are skipped (no LLM); snapshot and events unchanged.

---

## 2. Enrichment (LLM pipeline: `enrich_press_items_with_llm`)

Input: `items` = filtered_items (90d + cap). Output: list of canonical press dicts (title, url, date, outlet, topic, summary, secondary_urls).

| Step | What | Where |
|------|------|--------|
| **2.0a** | **Company-domain filter** — Drop items whose URL is on the competitor’s own domain (so we don’t surface “Google News → company’s own link”). Exception: keep `press_endpoint` items. | `enrich_press_items_with_llm` (start) |
| **2.0b** | **Incremental** — If `previous_items` / `previous_canonical` provided, only items not in `previous_items` are sent through classify/dedupe; result is merged with `previous_canonical`. | `enrich_press_items_with_llm` |
| **2.1** | **Classify (headlines + body)** — LLM gets title, outlet, URL and (when fetched) body text (paragraphs only, up to ~2k chars). Assigns `is_about_company`, `topic` (e.g. new_partnership, fundraising, irrelevant), `is_promo`. Body used when available so classification is content-based. | `_classify_press_headlines_with_llm` (fetches body for up to 200 items, capped) |
| **2.2** | **Heuristics** — Mark as promo: URLs that look like own marketing (e.g. /blog, /guides, /owners) or guide-style titles (e.g. “how to”, “best ”, “itinerary”). | `enrich_press_items_with_llm` (after classify) |
| **2.3** | **Business-relevance filter** — PR Newswire: always keep. Google News: drop only if `topic == irrelevant`. press_endpoint / other: require `is_about_company`, drop `irrelevant` and promo. | `enrich_press_items_with_llm` |
| **2.4** | **First dedupe (body-aware)** — Fetch article body (up to 40 items, 1200 chars). LLM returns indices to KEEP: drop not hospitality/hotels/real estate/fundraising/company; merge same article content or same story (one index per story). | `_first_dedupe_press_with_body_llm` |
| **2.5** | **Story keys** — LLM assigns a short story key per item from headline + outlet + date. Same event → same key. | `_assign_press_story_keys` |
| **2.6** | **Jaccard merge** — Clusters that share the same story key are already grouped; merge clusters whose title fingerprints have Jaccard ≥ 0.38 (same story, different key). | `enrich_press_items_with_llm` (cluster loop) |
| **2.7** | **Canonical build** — Per cluster: pick primary by source priority (PR Newswire > Google News > press_endpoint; then outlet rank). Others go to `secondary_urls`. Normalize date to ISO. | `enrich_press_items_with_llm` |
| **2.8** | **Summaries** — For each canonical item (or only new ones when incremental): fetch article HTML, extract body (paragraphs), LLM returns 1-line summary and refined topic. Capped at `max_articles_to_summarize` (e.g. 40). | `_summarize_press_article_with_llm` (and `_fetch_article_html` / `_html_to_article_text`) |
| **2.9** | **Final dedupe** — LLM sees titles, dates, outlets, URLs, and short snippets (up to 25 fetches, 450 chars). Returns indices to KEEP; drop duplicates and wrong-entity (e.g. not Lark Hotels). | `_dedupe_canonical_press_with_llm` |

---

## 3. Persist and events (runner)

| Step | What | Where |
|------|------|--------|
| **3.1** | **Persist snapshot** — Save `structured_json`: items (raw), canonical_items (enriched), sources. Hash from raw items used for “unchanged” skip on next run. | `runner.run_press` → `persist_snapshot` |
| **3.2** | **Diff and events** — Compare previous vs current items; for each *added* item run rules (e.g. `classify_press`, `is_executive_relevant`, `build_press_event`). Create events (e.g. new_partnership, new_hotel_opening) and persist. | `diff_items`, `classify_press`, `build_press_event`, `create_event` |

---

## 4. Dossier display (routes + template)

| Step | What | Where |
|------|------|--------|
| **4.1** | **Load canonical** — Read latest press snapshot’s `canonical_items`. | `build_dossier_context` in `dossier.py` |
| **4.2** | **Exclude irrelevant/promo** — Drop items with `topic` in `{irrelevant, promo_or_brand_marketing}`. Sort by date (newest first). This list is `press_90d`. | `dossier.py` |
| **4.3** | **Top news** — From `press_90d`, keep items whose topic is in STRONG_BUSINESS_TOPICS (e.g. fundraising, new_partnership, new_hotel_opening). Score by topic newsworthiness + recency (last 7 days boost). Take up to 5; optional filter by reporting baseline date. | `dossier.py` → `top_news` (or similar) |
| **4.4** | **Template** — “Top news” shows up to 5 links; full “Press” section lists all `press_90d` with display_title, date, link, summary. | `dossier.html` |

---

## Config / constants (relevant to press)

- **Runner:** `press_max_items_per_source`, `press_max_raw_items_per_competitor`, `press_enable_google_news`, `press_max_articles_to_summarize`
- **LLM classify:** body fetches capped (e.g. 200 items, ~2k chars per body)
- **First dedupe:** 40 fetches, 1200 chars snippet
- **Final dedupe:** 25 fetches, 450 chars snippet
- **Press search name:** From competitor’s press endpoint `extra_options.press_search_name`, or fallback e.g. “Lark” → “Lark Hotels”

---

## Flow summary

```
Collection (endpoints + Google News + PR Newswire)
  → 90d + cap
  → [incremental: skip if no new URLs]
Enrichment:
  company-domain filter → classify (headlines + body) → heuristics → business filter
  → first dedupe (body) → story keys → Jaccard merge → canonical build
  → summaries (fetch + LLM) → final dedupe
Persist snapshot + diff/events
Display: canonical minus irrelevant/promo = press_90d; top 5 = Top news
```
