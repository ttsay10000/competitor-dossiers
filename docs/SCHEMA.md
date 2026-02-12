# SCHEMA

## Core Tables
- competitors
  - id
  - name
  - primary_domain
  - created_at

- source_endpoints
  - id
  - competitor_id
  - channel (talent | asset | press | social)
  - url
  - confidence (high | low)
  - created_at

- snapshots
  - id
  - competitor_id
  - channel
  - raw_content
  - raw_hash
  - structured_json
  - captured_at

- events
  - id
  - competitor_id
  - category (talent | asset | partner | capital | narrative)
  - type
  - severity (high | med | low)
  - title
  - summary
  - why_it_matters
  - evidence_json
  - occurred_at
  - detected_at

## Optional / Derived
- capabilities
  - competitor_id
  - capability
  - first_seen_at

## Notes
- Snapshots are per-collector run and are immutable.
- Events are derived strictly from structured JSON diffs.
