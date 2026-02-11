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

## Press grouping (same story = one group, no dropping)

After classification and business filter, we **group** (not dedupe) articles by similarity: same story, same topic, or related coverage. The LLM sees title, date, and outlet per item and returns **groups** with a `group_title`, `one_line_summary`, and the list of article indices in that group. Every article is assigned to exactly one group; none are dropped. PR Newswire items are split out and appended as a single "Press releases" group.

Result: grouped topics with articles as sub-articles under each group; each article keeps its link ("see full article"). Code: `app/llm_structured.py` (`_group_press_into_clusters_llm`, `enrich_press_items_with_llm`).

**Refresh behavior:** Every press refresh runs the **full** pipeline on the full pull (no skip when URL set is unchanged). All qualifying articles are re-classified and re-grouped each run, so late articles about the same topic join the right group and new topics appear as new groups.

## Scheduling
- Daily: talent + press
- 2–3x/week: asset
