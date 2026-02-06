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

## Scheduling
- Daily: talent + press
- 2–3x/week: asset
