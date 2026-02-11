# Press data flow: from collection to final output

This doc traces **where grouped press data goes** after the LLM groups it, and **where to edit** if the final output is wrong.

---

## Step 1: Collect raw items (runner)

**Where:** `app/runner.py` — `run_press()` (or `run_press_local()` for local/cli).

**What happens:**
- User press endpoints (if any) → items with `provider: "press_endpoint"`.
- Google News (90d) → `collect_google_news_items(press_search_name, ...)`.
- PR Newswire (90d) → `collect_prnewswire_items(press_search_name, ...)`.
- Items are filtered by 90-day window and capped per source; PR Newswire is always kept.
- **Every refresh** runs the full pipeline on this full pull (no skip when "no new URLs"). All qualifying articles go through the same cleaning and grouping so late articles join the right groups and new topics appear as new groups.

**Output:** `raw_items` / `filtered_items` — list of dicts with `title`, `url`/`link`, `date`, `outlet`/`source`, `provider`, etc.

**Edit collection:** `app/collectors/global_press.py` (Google News, PR Newswire) or endpoint config for user URLs.

---

## Step 2: Build structured (runner)

**Where:** `app/runner.py` — `build_press_structured(snapshot_like)` from `app/collectors/press.py`.

**What happens:** Wraps `filtered_items` into a structured dict: `{ "source_url", "items": [...] }`. No grouping yet.

**Output:** `structured` with `structured["items"]` = same list of raw items.

**Edit:** `app/collectors/press.py` — `build_press_structured()`.

---

## Step 3: Classify + filter (LLM)

**Where:** `app/llm_structured.py` — `enrich_press_items_with_llm()`.

**What happens (inside that function):**
- Drops all items whose URL is on the competitor’s own domain (including press_endpoint).
- Calls `_classify_press_headlines_with_llm(competitor_name, items)` → each item gets `topic` (e.g. `irrelevant`, `promo_or_brand_marketing`, `new_hotel_opening`, `other_business`).
- Applies business filter: for Google News (and similar), drops `irrelevant` and `promo_or_brand_marketing`; PR Newswire is always kept.
- Result is a **filtered list** of items that will be grouped.

**Edit classification/filter:** `app/llm_structured.py` — `_classify_press_headlines_with_llm`, overrides/heuristics, and the filter block inside `enrich_press_items_with_llm` (e.g. “drop irrelevant/promo for google_news”).

---

## Step 4: Group by story (LLM)

**Where:** `app/llm_structured.py` — `_group_press_into_clusters_llm(competitor_name, rest)` inside `enrich_press_items_with_llm()`.

**What happens:**
- Splits filtered items: **PR Newswire** → one group “Press releases” (no LLM). **Everything else** → sent to LLM.
- LLM gets lines: `INDEX | DATE | OUTLET | TITLE` for each article. It returns JSON: `{ "groups": [ { "group_title", "one_line_summary", "article_indices": [0,1,...] }, ... ] }`.
- Code maps indices back to articles and builds:  
  `[ { "group_title": str, "one_line_summary": str, "articles": [ { "title", "url", "date", "outlet" }, ... ] }, ... ]`.
- PR Newswire group is appended: `{ "group_title": "Press releases", "one_line_summary": "Company press releases.", "articles": [...] }`.

**Output:** `press_groups` — list of group dicts, each with `group_title`, `one_line_summary`, `articles` (list of article dicts with `title`, `url`, `date`, `outlet`).

**Edit grouping (prompt, parsing, fallback):** `app/llm_structured.py` — `_group_press_into_clusters_llm()` (system/user prompt, `_parse_group_response`, `_fallback_single_group`).

---

## Step 5: Attach to structured and flatten (runner)

**Where:** `app/runner.py` — right after `enrich_press_items_with_llm()`.

**What happens:**
- `structured["press_groups"] = press_groups` (the list from Step 4).
- `structured["canonical_items"]` is built by flattening all groups: each article gets `title`, `url`, `date`, `outlet`, plus `group_title` and `summary` from its group (for Top news / diff / backward compat).

**Edit:** `app/runner.py` — same block that sets `structured["press_groups"]` and builds `canonical_items`.

---

## Step 6: Persist snapshot (runner)

**Where:** `app/runner.py` — `persist_snapshot(session, competitor.id, "press", "", raw_hash, structured)`.

**What happens:** The whole `structured` dict (including `items`, `press_groups`, `canonical_items`, `sources`) is stored in the **Snapshot** table as `structured_json` for `channel="press"`. One row per run; latest is used when viewing the dossier.

**Edit:** `app/runner.py` — `persist_snapshot()` call and anything that changes `structured` before it.

---

## Step 7: Load snapshot for dossier (route)

**Where:** `app/routes/dossier.py` — when building the dossier for a competitor.

**What happens:**
- Load latest press snapshot:  
  `latest_press = session.query(Snapshot).filter(competitor_id, channel=="press").order_by(captured_at.desc()).first()`.
- Read from snapshot:  
  `raw_press_groups = (latest_press.structured_json or {}).get("press_groups", [])`.  
  Filter to valid groups: `press_groups_snapshot = [g for g in raw_press_groups if isinstance(g, dict) and g.get("articles")]`.

**Edit:** `app/routes/dossier.py` — how `latest_press` is queried or how `press_groups_snapshot` is derived (e.g. filtering, ordering).

---

## Step 8: Build template payload (route)

**Where:** `app/routes/dossier.py` — “Build press_groups for template” block.

**What happens:**
- If `press_groups_snapshot` is non-empty: for each group, copy `group_title`, `one_line_summary`, and build `articles` with each article dict plus `display_title` (cleaned title via `_press_display_title()`).
- If no groups (old snapshot): fallback to one “group” per canonical item using `canonical_press`.
- Result is the `press_groups` list passed to the template.

**Edit:** `app/routes/dossier.py` — the block that builds `press_groups` (and `_press_display_title` if you want to change how titles look).

---

## Step 9: Render in HTML (template)

**Where:** `app/templates/dossier.html` — “Press” card.

**What happens:**
- For each `group` in `press_groups`: render **group_title** as headline, **one_line_summary** if present, then a `<ul>` of articles.
- Each article: **date**, **outlet**, “see full article” link (**url**), **display_title** (or **title**).

**Edit:** `app/templates/dossier.html` — the `{% for group in press_groups %}` block (structure, labels, links, order).

---

## Final output JSON (groupings included)

- **Persisted snapshot** (Step 6): `structured_json` stored in the Snapshot table includes `press_groups`. Each group has `group_title`, `one_line_summary`, and `articles` (each article: `title`, `url`, `date`, `outlet`). Do not remove or rename `press_groups` when changing the runner.
- **Dossier JSON API**: `GET /dossier/{competitor_id}/json` returns the full dossier as JSON, including **`press_groups`** with the same structure (group_title, one_line_summary, articles). Use this when you need final output JSON with groupings.

---

## Quick reference: “Final output is wrong” → where to look

| If the problem is… | Look here |
|-------------------|-----------|
| Wrong articles included (irrelevant/promo) | Step 3: `app/llm_structured.py` — classification + filter |
| Groups are wrong (e.g. one big group, bad headlines) | Step 4: `app/llm_structured.py` — `_group_press_into_clusters_llm` (prompt + parsing) |
| Groups not saved / old data shown | Step 6: runner `persist_snapshot`; Step 7: dossier loads latest snapshot |
| Groupings missing from final output JSON | Snapshot has `press_groups`; API: `GET /dossier/{id}/json` includes `press_groups` |
| Wrong title/date/outlet/link on the page | Step 8: `app/routes/dossier.py` — `press_groups` build + `_press_display_title`; Step 9: `app/templates/dossier.html` |
| Order of groups or articles | Step 8: groups sorted by latest article date in group (most recent first); each group tagged with `group_latest_date`; template shows that date. Competitor-domain articles filtered out at display. |

---

## Inspect scripts (no DB)

- **Classification only:** `scripts/inspect_press_classification.py [Lark|Placemakr|AvantStay]` — shows what’s marked irrelevant/promo vs included.
- **Full pipeline including grouping:** `scripts/inspect_press_grouping.py [Lark|Placemakr|AvantStay]` — same as Steps 1–4 output (groups as printed), uses `.env` for `OPENAI_API_KEY`.
