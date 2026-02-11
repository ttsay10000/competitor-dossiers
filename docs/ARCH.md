# ARCH

## System Architecture
- Collectors: talent, asset, press (configurable per competitor)
- Extraction: parse raw sources into structured JSON
- Diff Engine: compare structured JSON only (never raw HTML diffs)
- Rules Engine: apply meaningful-change + severity rules
- Event Writer: deduplicate and persist events
- Storage: snapshots (raw + structured) and events
- UI: competitors CRUD + feed
- Runner: scheduled jobs and manual CLI

## Data Flow
1. Fetch raw source content per endpoint.
2. Store raw snapshot for evidence/debugging.
3. Extract structured JSON (jobs, properties/markets, press items).
4. Diff structured JSON against prior snapshot for deltas.
5. Apply persistence gates (e.g., market exit requires 2 consecutive runs).
6. Deduplicate related deltas into single events (e.g., hiring surge).
7. Persist event and update derived capability history.
8. Serve /feed and weekly digest.

## Design Principles
- Always store both raw snapshot and structured extraction.
- Events are generated only from structured JSON diffs.
- Idempotent runs; no duplicate events.
- Prefer false negatives to false positives.
- LLM usage (optional later) only to polish summaries after rules gating.

## Press deduping (same story = one row)

We treat multiple articles as the **same story** when they refer to the same underlying event (same deal, announcement, expansion, etc.), not just similar wording. That way "Lark Hotels to Open Four New Properties in 2026", "Lark Hotels Adding Four New Spots In 2026", and "Four New Lark Hotels Set To Boost Hospitality In Massachusetts" (different headlines and dates) become one canonical item with one primary URL and `secondary_urls` for the rest.

**How it works (two steps):**

1. **Story keys (LLM)**  
   For each item the LLM sees: headline, outlet, and **publication date**. It assigns a short canonical key (e.g. `lark four properties 2026`). Same event + same rough time window → same key. So we use general thrust and date, not just keyword matching.

2. **Title-similarity merge**  
   After grouping by story key, we merge any two clusters whose titles have high word overlap (Jaccard on non-stopwords). That catches cases where the LLM gave different keys to the same story.

Result: one row per story (primary + `secondary_urls`). Code: `app/llm_structured.py` (`_assign_press_story_keys`, and the dedupe block in `enrich_press_items_with_llm`).

## Scheduling
- Daily: talent + press
- 2–3x/week: asset
