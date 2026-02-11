# LLM Cleaning Steps for Final Output

This document describes the **order of LLM steps** and **prompts** that clean data so the dossier (and related outputs) populate correctly. When "data doesn't populate" or "cleaning doesn't work across articles," the failure is usually in one of these steps.

---

## 1. Press pipeline (articles) — `app/llm_structured.py`

The press flow runs in **one fixed order**. Each step consumes the output of the previous step. If an earlier step drops too much or mislabels, later steps see less/wrong data.

### Step 1 — Classify headlines (metadata only)

- **Function:** `_classify_press_headlines_with_llm(competitor_name, items)`
- **Input:** Raw items with `title`, `url`/`link`, `source` (outlet). **No article body.**
- **What the LLM does:** For each item, returns:
  - `is_about_company` (bool)
  - `topic` (one of: fundraising, new_hotel_opening, new_partnership, restructuring_or_layoffs, other_business, executive_interview, promo_or_brand_marketing, irrelevant)
  - `is_promo` (bool)
- **Prompt focus:** Business vs promo/irrelevant; wrong-entity (e.g. Lark = hotel company only); partnership/executive-hire rules. **No cross-article cleaning** — each item classified independently.
- **Output:** Same list with `is_about_company`, `topic`, `is_promo` added. Downstream filtering drops `irrelevant` and promo/not-about-company.

**If classification is wrong:** Irrelevant articles can slip through or good articles get dropped here; later steps cannot "recover" them.

---

### Step 2 — First dedupe (body-aware)

- **Function:** `_first_dedupe_press_with_body_llm(competitor_name, items)`
- **Input:** Classified items. **Fetches article body** for up to 40 items (snippet ~1200 chars each).
- **What the LLM does:** Returns **only a JSON array of 0-based indices to KEEP**, e.g. `[0, 2, 5]`.
  - **EXCLUDE:** Not about hospitality/hotels/lodging/real estate/fundraising/company; wrong entity; same-name different company.
  - **MERGE:** Same article content (syndicated) or same story (same event, different outlets) → keep one index; prefer best source; order most recent first.
- **Prompt rule:** *"Your array MUST be shorter than the input when there are irrelevant items or content duplicates."*
- **Output:** Subset of `items` at the returned indices. **This is the first cross-article cleaning** — uses body text to drop irrelevant and merge duplicates.

**If this doesn’t clean properly:** Model may return all indices (no reduction), or parse fails and the code returns the full list unchanged. Check stderr for `[press] First dedupe (body): N -> M items`.

---

### Step 3 — Story keys (grouping, no dropping)

- **Function:** `_assign_press_story_keys(competitor_name, items)`
- **Input:** Items after first dedupe (title, outlet, date per item).
- **What the LLM does:** Assigns a **short story key** (e.g. `lark four properties 2026`) so that items about the **same news event** get the **same key**. Same event = same deal/announcement/expansion; different headlines/dates/outlets can share a key.
- **Output:** List of strings, one per item. Used only for **clustering** (next step). No indices returned; nothing dropped here.

---

### Step 4 — Clustering + canonical row build (code, not LLM)

- **Code:** Builds clusters by story key, then merges clusters with high Jaccard title similarity. For each cluster, picks one primary item (by source priority) and `secondary_urls` for the rest.
- **Output:** One **canonical** dict per story: `title`, `url`, `date`, `outlet`, `topic`, `secondary_urls`, `summary: None` (filled next).

---

### Step 5 — Per-article summarization

- **Function:** `_summarize_press_article_with_llm(...)` — called once per canonical article (up to `max_articles_to_summarize`, default 40).
- **Input:** One article: title, outlet, date, url, **HTML body** (fetched), topic hint.
- **What the LLM does:** Returns a **JSON object** with:
  - `summary`: one-sentence factual summary
  - `topic`: refined topic (one of `PRESS_TOPICS`)
- **No cross-article logic** — each article summarized in isolation. This step **populates** `summary` (and refines `topic`) so the dossier can show it.

**If summaries don’t populate:** Check that `summary` is read from the JSON and assigned to the canonical item (`out["summary"] = meta.get("summary") or item.get("title") or "Press item"`). Missing API key or parse failure leaves title as fallback.

---

### Step 6 — Final dedupe (canonical list + snippets)

- **Function:** `_dedupe_canonical_press_with_llm(competitor_name, items)`
- **Input:** **Canonical list with summaries** — each item has title, url, date, outlet, topic, summary. Optionally fetches short **snippets** (up to 25 articles, ~450 chars each) for context.
- **What the LLM does:** Returns **only a JSON array of 0-based indices to KEEP**, e.g. `[0, 2, 5]`.
  - **EXCLUDE:** Not hospitality/hotels/lodging/real estate/fundraising/company; obituaries; wrong entity (e.g. LARK Distilling vs hotel company); person/street/unrelated. For "Lark" only Lark Hotels / Lark Hospitality.
  - **MERGE:** Same event = same entities + same topic + similar date; one index per story; use snippet to confirm same story or wrong-entity. Order most recent first.
- **Prompt rule:** *"Your array MUST be shorter than the input when there are duplicates or wrong-entity items."*
- **Output:** `result = [items[i] for i in indices]`. **This is the final cross-article cleaning** before the list is returned to the dossier.

**If final dedupe doesn’t clean properly:**
- **Parse failure:** `_parse_dedupe_indices` returns `None` → function returns **full list unchanged** (data still populates, but no extra dedupe).
- **Model returns all indices:** Code retries once with a minimal "merge same-story" prompt; if still no reduction, full list is kept.
- **Model drops too many:** Too few indices → fewer items in "Top news" / press section. Check stderr: `[press] Final dedupe: N -> M items`.

---

### Press pipeline order (summary)

1. **Classify** (headlines only) → add is_about_company, topic, is_promo  
2. **Filter** (code) → drop irrelevant/promo by those flags  
3. **First dedupe** (body) → LLM returns indices to KEEP → subset  
4. **Story keys** → LLM assigns key per item  
5. **Cluster + canonical** (code) → one row per story, primary + secondary_urls  
6. **Summarize** (per article) → LLM fills summary + refined topic  
7. **Final dedupe** (canonical + snippets) → LLM returns indices to KEEP → **final list**

The **final list** is what the dossier uses for "Top news" and "Press (90d)". Each item must have: `title`, `url`, `date`, `outlet`, `topic`, `summary`. If any of these are missing, the template still renders but may show "Press item" or blank where you expect text.

---

## 2. Location cleaning (properties) — `app/executive_summary.py`

- **Function:** `clean_location_display_for_dossier(competitor_name, properties_by_location, asset_delta_by_city, other_sub_bullets_text=...)`
- **Input:** Raw location rows (e.g. "Temecula: 5", "Central Oregon: 3") and optional "Other" sub-bullets (URLs). **No per-property list** — only aggregated "location: count" (and keys/deltas).
- **What the LLM does:** Maps every input row to **one US state** (or "Other"), and **sums counts/keys** when merging. Returns JSON:
  - `properties_by_location`: `[{ "location": "California", "count": n, "keys": k }, ...]`
  - `asset_delta_by_city`: `[{ "location": "Texas", "added": a, "removed": r }, ...]`
- **Prompt rules:** Output only US state names (or "Other"); no regions/cities in output; grand total of counts must match input total.
- **Output:** Used in `build_dossier_context` to replace raw location breakdown so the dossier shows state-level breakdown. If the LLM returns only "Other" or total &lt; 50% of raw, the result is **rejected** and raw data is used (or totals don’t match and UI can show a note).

**If location data doesn’t populate:** Rejection can happen for: no API key, JSON parse failure, only_other_or_under_half, no_state_row_in_output. Use `debug_return_parsed=True` to see `_rejected` and `_reason`.

---

## 3. Executive summary — `app/executive_summary.py`

- **Function:** `generate_executive_summary(context)`
- **Input:** Context dict with events, top_news, asset deltas, etc. **Already filtered** to post-baseline / comparison data.
- **What the LLM does:** Produces a **short bullet list** (3–6 bullets) of **changes since baseline** only. No cross-article cleaning — it synthesizes from the context text provided.
- **Output:** Plain text bullets shown in the dossier. Does not affect whether press or location data "populates"; it only describes what’s already there.

---

## 4. What to check when "LLM doesn’t clean properly" or "data doesn’t populate"

| Symptom | Likely step | What to check |
|--------|-------------|----------------|
| Irrelevant or wrong-entity articles in Top news | Classification (1) or First dedupe (2) or Final dedupe (6) | Stderr for dedupe counts; try tightening EXCLUDE in prompts for step 2 and 6. |
| Same story shown multiple times | First dedupe (2), story keys (3), or Final dedupe (6) | Model may return all indices; check retry log for final dedupe; consider stronger "same story = one index" in prompts. |
| Missing summaries (e.g. "Press item" only) | Summarization (5) | API key, fetch failures for HTML, or JSON parse failure in `_summarize_press_article_with_llm`; fallback is title. |
| Press list empty or very short | Classification (1) or Filter (code) or First dedupe (2) or Final dedupe (6) | Too aggressive is_about_company/topic filter, or dedupe returning too few indices. |
| Location shows only "Other" or wrong totals | Location cleaning | `debug_return_parsed` and `_reason` (e.g. only_other_or_under_half, no_state_row_in_output); prompt says output must be state names only and sums must match. |

---

## 5. Prompt locations (file:line)

- **Classification:** `app/llm_structured.py` ~597–643 (system), 644–645 (user)  
- **First dedupe (body):** ~891–916 (system + user)  
- **Story keys:** ~762–731 (system), 732 (user)  
- **Summarization:** ~1266–1302 (system + user)  
- **Final dedupe:** ~1032–1055 (system + user); retry ~1124–1130  
- **Location cleaning:** `app/executive_summary.py` ~334–354 (system), ~357–360 (user)  
- **Executive summary:** `app/executive_summary.py` ~254–264 (system), 266 (user)

These are the exact prompts that control cleaning and population; changing them will change what the dossier shows.
