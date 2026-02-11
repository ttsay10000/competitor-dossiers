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

Input: `items` = filtered_items (90d + cap). Output: **press_groups** — list of `{ group_title, one_line_summary, articles: [ { title, url, date, outlet }, ... ] }`. No articles dropped; similar items grouped. Runner flattens to `canonical_items` for diff/Top news.

| Step | What | Where |
|------|------|--------|
| **2.0a** | **Company-domain filter** — Drop items whose URL is on the competitor’s own domain (so we don’t surface “Google News → company’s own link”). Exception: keep `press_endpoint` items. | `enrich_press_items_with_llm` (start) |
| **2.1** | **Classify (headlines + body)** — LLM gets title, outlet, URL and (when fetched) body text (paragraphs only, up to ~2k chars). Assigns `is_about_company`, `topic` (e.g. new_partnership, fundraising, irrelevant), `is_promo`. Body used when available so classification is content-based. | `_classify_press_headlines_with_llm` (fetches body for up to 200 items, capped) |
| **2.2** | **Heuristics** — Mark as promo: URLs that look like own marketing (e.g. /blog, /guides, /owners) or guide-style titles (e.g. “how to”, “best ”, “itinerary”). | `enrich_press_items_with_llm` (after classify) |
| **2.3** | **Business-relevance filter** — PR Newswire: always keep. Google News: drop only if `topic == irrelevant`. press_endpoint / other: require `is_about_company`, drop `irrelevant` and promo. | `enrich_press_items_with_llm` |
| **2.4** | **Split by provider** — PR Newswire items → separate list; all other relevant items → grouping input. | `enrich_press_items_with_llm` |
| **2.5** | **Group (LLM)** — LLM sees title, date, outlet per item. Groups by similarity (same story, same topic). Returns groups with `group_title`, `one_line_summary`, `article_indices`. Every article in exactly one group; none dropped. | `_group_press_into_clusters_llm` |
| **2.6** | **Press releases group** — Append one cluster: group_title "Press releases", one_line_summary "Company press releases.", articles = all PR Newswire items (after filter). | `enrich_press_items_with_llm` |

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
| **4.1** | **Load press_groups** — Read latest press snapshot’s `press_groups`; if missing (old snapshot), derive from `canonical_items` (one group per item). | `build_dossier_context` in `dossier.py` |
| **4.2** | **Flat list** — Flatten groups to build `press_90d` (sorted by date, newest first) for Top news. | `dossier.py` |
| **4.3** | **Top news** — From flattened list, score by topic newsworthiness (when present) + recency (last 7 days boost). Take up to 5; optional filter by reporting baseline date. | `dossier.py` → `top_news` |
| **4.4** | **Template** — “Top news” shows up to 5 links; full “Press” section renders `press_groups`: each group shows group_title, one_line_summary, then articles with "see full article" per link. | `dossier.html` |

---

## Config / constants (relevant to press)

- **Runner:** `press_max_items_per_source`, `press_max_raw_items_per_competitor`, `press_enable_google_news`, `press_max_articles_to_summarize`
- **LLM classify:** body fetches capped (e.g. 200 items, ~2k chars per body)
- **Group LLM:** one call; input = compact lines (index, date, outlet, title); output = groups with article_indices
- **Press search name:** From competitor’s press endpoint `extra_options.press_search_name`, or fallback e.g. “Lark” → “Lark Hotels”

---

## Flow summary

```
Collection (endpoints + Google News + PR Newswire)
  → 90d + cap
  → [incremental: skip if no new URLs]
Enrichment:
  company-domain filter → classify (headlines + body) → heuristics → business filter
  → split PR vs rest → group LLM (no dropping) → append Press releases group
Persist: press_groups + canonical_items (flattened) + diff/events
Display: press_groups by topic; flattened list for Top news
```
