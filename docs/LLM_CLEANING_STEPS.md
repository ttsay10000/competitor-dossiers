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

### Step 2 — Group (no dropping)

- **Function:** `_group_press_into_clusters_llm(competitor_name, items)`
- **Input:** Relevant items after business filter (title, url, date, outlet). PR Newswire items are split out and not sent to this step.
- **What the LLM does:** Returns a **JSON object** with key `"groups"`: array of `{ "group_title", "one_line_summary", "article_indices": [0, 2, 5] }`. **Every article index 0..N-1 must appear in exactly one group** — no dropping. Groups by similarity of title, date, and general topic.
- **Output:** List of groups; each group has `group_title`, `one_line_summary`, and `articles` (list of { title, url, date, outlet }).

**If this doesn’t clean properly:** Model may return all indices (no reduction), or parse fails and the code returns the full list unchanged. Check stderr for `[press] Group LLM: N items -> M groups`.

---

### Step 3 — Press releases group (code)

- **Code:** After grouping non-PR items, append one cluster: `group_title` "Press releases", `one_line_summary` "Company press releases.", `articles` = all PR Newswire items (already passed business filter).

---

### Press pipeline order (summary)

1. **Classify** (headlines + body when fetched) → add is_about_company, topic, is_promo  
2. **Filter** (code) → drop irrelevant/promo by those flags  
3. **Split** (code) → PR Newswire vs rest  
4. **Group** (LLM) → groups with group_title, one_line_summary, article_indices; every article in exactly one group  
5. **Append Press releases** (code) → one group for all PR items  

The **press_groups** list is what the dossier uses. Runner flattens to `canonical_items` for diff and Top news. Each article has: `title`, `url`, `date`, `outlet`. Group-level summary is `one_line_summary`.

---

## 2. Property-level state assignment (optional) — `app/llm_structured.py`

- **Functions:** `assign_states_to_properties_for_dossier(...)`, `_bucket_properties_by_state_single_call` (and batched fallback), `research_and_assign_states_for_other_properties(...)`.
- **Input:** Per-property list with `name`, `url`, `market`, `url_derived_location`, `details` (and `region` when present).
- **What the LLM does:** Assigns a **US state** (or "Other") to each property using name, url, market, region, and url_derived_location.
- **When it runs:** Not called from the dossier build by default; dossier uses URL-derived state then relies on aggregation (Section 3).
- **Prompt rules:** Map US regions, cities, and area names to the correct state. Do not put US cities or regions into "Other". Only use "Other" for: Unspecified, career site, privacy, non-property URLs, or genuinely non-US/unclear.
- **Prompt locations:** `app/llm_structured.py` — single-call ~249–254, batched ~337–344, research-for-Other ~420–428.

---

## 3. Location cleaning (aggregation) — `app/executive_summary.py`

- **Function:** `clean_location_display_for_dossier(competitor_name, properties_by_location, asset_delta_by_city, other_sub_bullets_text=...)`
- **Input:** **Only the list of summarized bullets** — e.g. "Temecula: 5 (12 keys)", "Central Oregon: 3", "Other: 2". No per-property data (no URLs, no names). Plus asset_delta_by_city. `other_sub_bullets_text` is not sent (kept for API compatibility only).
- **What the LLM does:** Reviews whether the list is already grouped by state. If not, adds totals and maps regions to the closest US state (or keeps a row separate if it doesn't map neatly). Returns JSON:
  - `properties_by_location`: `[{ "location": "California", "count": n, "keys": k }, ...]`
  - `asset_delta_by_city`: `[{ "location": "Texas", "added": a, "removed": r }, ...]`
- **Prompt rules:** Region → pick closest state; if region doesn't map neatly, keep separate. Use "Other" only for Unspecified/non-US/career/privacy. Grand total of counts must match input total.
- **Output:** Used in `build_dossier_context` to replace raw location breakdown so the dossier shows state-level breakdown. If the LLM returns only "Other" or total &lt; 50% of raw, the result is **rejected** and raw data is used (or totals don’t match and UI can show a note).

**If location data doesn’t populate:****If location data doesn't populate:** Rejection can happen for: no API key, JSON parse failure, only_other_or_under_half, no_state_row_in_output. Use `debug_return_parsed=True` to see `_rejected` and `_reason`.

---

## 4. Executive summary — `app/executive_summary.py`

- **Function:** `generate_executive_summary(context)`
- **Input:** Context dict with events, top_news, asset deltas, etc. **Already filtered** to post-baseline / comparison data.
- **What the LLM does:** Produces a **short bullet list** (3–6 bullets) of **changes since baseline** only. No cross-article cleaning — it synthesizes from the context text provided.
- **Output:** Plain text bullets shown in the dossier. Does not affect whether press or location data "populates"; it only describes what’s already there.

---

## 5. What to check when "LLM doesn’t clean properly" or "data doesn’t populate"

| Symptom | Likely step | What to check |
|--------|-------------|----------------|
| Irrelevant or wrong-entity articles in Top news | Classification (1) or Filter (code) | Stderr for classify counts; tighten business filter or EXCLUDE in classify prompt. |
| Same story shown in multiple groups | Group (2) | Check Group LLM response; prompt asks for similarity by title/date/topic. |
| Press list empty or very short | Classification (1) or Filter (code) | Too aggressive is_about_company/topic filter. |
| Location shows only "Other" or wrong totals | Location cleaning | `debug_return_parsed` and `_reason` (e.g. only_other_or_under_half, no_state_row_in_output); prompt says output must be state names only and sums must match. |

---

## 6. Prompt locations (file:line)

- **Classification:** `app/llm_structured.py` ~597–643 (system), 644–645 (user)  
- **Group (press):** `_group_press_into_clusters_llm` in `app/llm_structured.py` (system + user)  
- **Property state (per-property):** `app/llm_structured.py` — single-call ~249–254, batched ~337–344, research-Other ~420–428  
- **Location cleaning (aggregation):** `app/executive_summary.py` ~334–356 (system), ~357–360 (user)  
- **Executive summary:** `app/executive_summary.py` ~254–264 (system), 266 (user)

These are the exact prompts that control cleaning and population; changing them will change what the dossier shows.
