# Files and documents index

Quick reference to find docs and to decide what to keep or remove. **Planning-only docs have been removed** (e.g. PRD); the rest are reference or operational.

---

## Root

| File | Purpose | Keep / Remove |
|------|---------|----------------|
| `README.md` | Project overview, local setup, how to run. | **Keep** — main entry for the repo. |

---

## docs/

| File | Purpose | Keep / Remove |
|------|---------|----------------|
| `ARCH.md` | System architecture, data flow, design principles, press deduping. | **Keep** — reference for how the system works. |
| `DEBUG_AND_TEST_SCRIPTS.md` | Index of all debug/test scripts and how to run or remove them. | **Keep** — use to find or delete internal tests. |
| `DEPLOY_RENDER.md` | Push-to-Render checklist; env vars; internal/external Postgres. | **Keep** — operational. (Contains credentials; consider `.gitignore` if repo is public.) |
| `FILES_AND_DOCS.md` | This index. | **Keep** — find/remove docs. |
| `JOBS_DATA_FLOW.md` | Talent: what job data is parsed and what is sent to the LLM. | **Keep** — reference for talent/LLM behavior. |
| `LLM_CLEANING_STEPS.md` | Order of LLM steps and prompts (press + asset location). | **Keep** — reference when debugging “data doesn’t populate.” |
| `LOCAL_POSTGRES.md` | Local Postgres setup (e.g. macOS/Homebrew). | **Keep** — how-to for local DB. |
| `LOCAL_REFRESH.md` | Run collectors locally against Render DB (Playwright, .env). | **Keep** — how-to for local refresh. |
| `POPULATE_SOURCES.md` | How to populate data: seed, first run, sources per competitor. | **Keep** — reference for populating data. |
| `PRESS_FEED_STEPS.md` | Press pipeline steps (collection → enrichment → canonical items). | **Keep** — technical reference for press flow. |
| `SCHEMA.md` | Core tables (competitors, source_endpoints, snapshots, events). | **Keep** — DB schema reference. |
| `SIGNALS.md` | Event taxonomy, capability buckets, severity rules, persistence gates. | **Keep** — reference for event types and rules (or remove if you only use code as source of truth). |
| `TALENT_SNAPSHOT_LLM.md` | Talent LLM role classification and why roles end up in “Other.” | **Keep** — reference for talent enrichment. |

**Removed (planning-only):** `PRD.md` — product requirements and MVP spec; product is built, so it was deleted.

---

## Optional removals

- **SIGNALS.md** — Event taxonomy and rules; if the code (e.g. `app/rules/`, runner) is the only source of truth you need, you can delete this.
- **DEPLOY_RENDER.md** — If you no longer deploy to Render or you move secrets elsewhere, you can remove it (or add to `.gitignore` if it holds credentials).

Everything else in `docs/` is reference or how-to for running, deploying, or understanding the system.
