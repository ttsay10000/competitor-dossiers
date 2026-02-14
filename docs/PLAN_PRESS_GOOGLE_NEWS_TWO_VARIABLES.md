# Plan: Split press search into two variables for Google News RSS (strict name + broad keywords)

## Google News RSS search syntax (how quoted vs unquoted is enforced)

Google does not publish official docs for the RSS search API; the following is from community docs (e.g. [NewsCatcher’s “Google News RSS Search Parameters”](https://www.newscatcherapi.com/blog-posts/google-news-rss-search-parameters-the-missing-documentaiton)) and standard Google search behavior.

**RSS URL shape:**
```
https://news.google.com/rss/search?q=ENCODED_QUERY&hl=en-US&gl=US&ceid=US:en
```
Only the `q=` value is built by us; the rest is fixed.

**How the `q` parameter is interpreted:**

1. **Exact phrase (strict):** Terms inside **double quotation marks** are an exact phrase match. Google requires that phrase to appear word-for-word. Use for company names, people, places. In the query string we send, the strict part must be wrapped in **literal ASCII double-quote characters** (`"`), e.g. `"Rove"`.

2. **Unquoted terms (broad):** Terms **not** in quotes are ANDed together; each word must appear somewhere in the article, but not necessarily as one phrase. So `furnished rentals` means “furnished” AND “rentals” anywhere in the text.

3. **Time range:** The `when:` operator limits by publication time, e.g. `when:90d` (90 days), `when:1h` (1 hour). No space before the colon.

**What we build in code (before URL encoding):**
- Query string = **one** string that looks like: `"Rove" furnished rentals when:90d`
  - `"Rove"` = literal quote + company name + literal quote → strict.
  - `furnished rentals` = no quotes → broad (both words present, any order).
  - `when:90d` = time window.

**URL encoding:** We pass that string to `urllib.parse.quote_plus(query)`. Spaces become `+`, double quotes become `%22`, `:` becomes `%3A`. Example encoded `q`: `%22Rove%22+furnished+rentals+when%3A90d`. Google decodes it and applies the same rules: quoted part = exact phrase, unquoted = ANDed keywords.

**Where this happens in our code:** In [app/collectors/global_press.py](app/collectors/global_press.py), `collect_google_news_items` builds:
```python
raw_query = f'"{primary}" {keywords_part}'.strip()   # primary = strict, keywords_part = unquoted
# then _fetch_google_news_rss appends when:Nd and does: encoded = quote_plus(query)
```
So the **strict** part is only `primary` (wrapped in `"`); **broad** part is `keywords_part` with no quotes. That is how we ensure quoted vs unquoted when plugging into Google RSS.

---

## Goal

Fix the press search so Google News RSS always gets:
1. **Strict (quoted):** competitor name only — one variable, fed to RSS in quotes.
2. **Broad (unquoted):** optional keywords — second variable, fed to RSS without quotes.

No single field should ever hold a combined phrase like "Rove furnished rentals"; that caused 0 results because the whole phrase was quoted.

## Current state

- **extra_options** already has two keys:
  - `press_search_name` — used as the **quoted** phrase (but often misused as "Rove furnished rentals").
  - `google_news_search_phrases` — list of **unquoted** keywords (e.g. `["furnished rentals"]`).
- The UI only exposes **Keywords** (google_news_search_phrases). It does **not** expose a field for the quoted name, so `press_search_name` only comes from seed or DB and was never corrected in the UI.
- Runner reads both from DB and passes them to `collect_google_news_items(company_name=..., search_phrases=...)`. The collector builds the query as `"<company_name>" <phrases> when:Nd`.

## Implementation

### 1. Make the two variables explicit in code and data

- **Strict (quoted) for Google News:** Keep using `press_search_name` in extra_options, but define it clearly as "Google News quoted phrase — company name only". Default at runtime: `competitor.name` when not set.
- **Broad (unquoted) for Google News:** Keep `google_news_search_phrases` as the list of keywords; never quote these in the RSS query.

No schema change required; only clarify usage and ensure the UI can set both.

### 2. UI: add "Search name (quoted)" for press source

- **Add source form:** For channel "press", add a second field:
  - **Search name (quoted, strict):** Optional. Placeholder: competitor name (e.g. "Rove"). Stored in `extra_options.press_search_name`. If empty, runner uses `competitor.name`.
  - Keep existing **Keywords** field → `google_news_search_phrases` (broad, unquoted).
- **Edit source form:** For press endpoints, show both:
  - **Search name (quoted, strict):** Prefill from `(endpoint.extra_options or {}).get('press_search_name')` or leave empty (meaning use competitor name).
  - **Keywords (unquoted, broad):** Existing field.

Files:
- [app/templates/competitor_edit.html](app/templates/competitor_edit.html): Add input for press "Search name (quoted)" in both add-source and edit-source blocks (press only). Use a name like `press_search_name` for the input.
- [app/routes/competitors.py](app/routes/competitors.py):
  - Add source: Accept `press_search_name: Optional[str] = Form(None)`. When channel is press, set `extra_options["press_search_name"] = (press_search_name or "").strip() or None` (and keep google_news_search_phrases). If the value is empty, omit the key so runner falls back to competitor.name.
  - Update source: Same: read `press_search_name` from form; for press, update `extra["press_search_name"]` or pop if empty.

### 3. Runner: use the two variables explicitly for Google News only

- When building the Google News call, set:
  - **quoted_phrase** = `opts.get("press_search_name") or competitor.name` (normalized: strip, empty → competitor.name).
  - **broad_keywords** = `google_news_search_phrases` (already a list; never quote).
- Pass **quoted_phrase** as `company_name` and **broad_keywords** as `search_phrases` to `collect_google_news_items`. Do not concatenate them into one string.
- PR Newswire can keep using the same `press_search_name` (or competitor.name) for its search; no change required there.

Files:
- [app/runner.py](app/runner.py): In the press block, keep current logic but ensure the variable used as the quoted part is explicitly the "name only" (from opts or competitor.name). No behavioral change if data is correct; this is clarity and future-proofing.
- [app/runner.py](app/runner.py) `get_press_competitors_list`: Same: pass through `press_search_name` and `google_news_search_phrases` as the two separate values.

### 4. One-time DB fix script

- Add `scripts/fix_press_search_name_in_db.py`:
  - For competitors Rove, Landing, AKA, find press endpoints where `extra_options.press_search_name` is one of `"Rove furnished rentals"`, `"Landing furnished rentals"`, `"AKA furnished rentals"`.
  - Update those endpoints: set `press_search_name` to the company name only ("Rove", "Landing", "AKA") and set `google_news_search_phrases` to `["furnished rentals"]` (merge with existing if needed).
  - Commit session. Idempotent and safe to run multiple times.

### 5. Seed and docs

- Seed data already has the correct split (press_search_name = "Rove"/"AKA"/"Landing", google_news_search_phrases = ["furnished rentals"]). No change.
- [docs/SEED_AND_DB_SYNC.md](docs/SEED_AND_DB_SYNC.md): Add a short note that for press, "Search name (quoted)" should be company name only; keywords go in the Keywords field and are sent unquoted to Google News. After changing seed, Run seed updates the DB.
- [app/collectors/global_press.py](app/collectors/global_press.py): Docstring already describes the split; optional one-line comment at query build: "quoted = strict, unquoted = broad."

## Summary

| Variable                 | Role in Google RSS | Where set              | Example        |
|--------------------------|--------------------|------------------------|----------------|
| press_search_name        | Quoted (strict)    | Seed, DB, new UI field | "Rove"         |
| google_news_search_phrases | Unquoted (broad) | Seed, DB, existing UI  | ["furnished rentals"] |

RSS query built as: `"<press_search_name or competitor.name>" <space-separated phrases> when:90d`.

## Order of work

1. One-time script to fix existing DB (Rove, Landing, AKA).
2. UI: add "Search name (quoted)" for press in add and update source forms; routes persist it.
3. Runner: ensure quoted vs broad are read as two variables and passed separately (already mostly done).
4. Docs and seed: note the two-variable split and Run seed after file changes.
