# Blueground Asset Build – Logic Flow & Debug Guide

## End-to-end flow

```
run_asset(competitor_name="Blueground")
  → endpoint: https://www.theblueground.com/destinations
  → extra_options: {"strategy": "blueground_destinations"}
  → collect_asset_snapshot(url, extra_options=...)
      → opts.get("strategy") = "blueground_destinations" (single strategy, no chain)
      → run_one_strategy("blueground_destinations")
          → if _playwright_available() and "theblueground.com" in source_url:
              → _fetch_blueground_destinations(source_url)
          → else:
              → _fetch_without_browser()  ← sitemap + HTML fallback (gets ~0 properties for /destinations)
  → build_asset_structured(snapshot)
  → enrich_properties_with_llm(...)
  → persist_snapshot(...)
```

---

## Step-by-step checks

### Step 0: Playwright must be available

- **Config**: `settings.playwright_enabled` must be True (env: `PLAYWRIGHT_ENABLED=true`).
- **Package**: `playwright` must be installed.
- **Browser**: Chromium must be installed (`playwright install` or `playwright install chromium`).

**If any of these fail** → falls back to `_fetch_without_browser()`, which does sitemap + HTML fetch. The `/destinations` page has no property listings in static HTML, so you get **0 properties**.

**Fix locally**: Run `python3 -m playwright install chromium`.

---

### Step 1: `_fetch_blueground_destinations(source_url)`

1. **Navigate** to `https://www.theblueground.com/destinations` (Playwright, `networkidle`).
2. **Find North America USA links**: Parse HTML in DOM order; stop at South America/Europe/etc. section headers. Keep links where:
   - href contains `/m/furnished-apartments/`
   - slug ends with `-usa`
   - exclude Canada (`-can`, `-on`, `-bc`, `-ab`, "canada" in slug)
   - no link limit (all North America USA destinations)
3. **For each destination**:
   - Navigate to e.g. `https://www.theblueground.com/m/furnished-apartments/acton-ma-usa`
   - Click Search (or Add dates) → wait for navigation to `/sp?placeId=...`
   - Scrape the **search results page** (`/sp?placeId=...`) for property names
   - Extract from: `listing-card*` spans, `#ID • Name` text patterns, property links

---

### Step 2: USA link discovery

**Filter logic** (in `_fetch_blueground_destinations`):

- Required: `/m/furnished-apartments/` in href
- Required: slug ends with `-usa`
- Excluded: Canada (`canada`, `-on`, `-bc`, `-ab`)

**Note**: Major cities like Atlanta, Austin, Boston, Chicago, Dallas use URLs such as:
- `https://www.theblueground.com/furnished-apartments-atlanta-ga` (no `/m/`)

These are **skipped** by the current filter. Only `/m/furnished-apartments/*-usa` links are visited. That’s still hundreds of USA cities (Acton, Agoura Hills, Alameda, etc.).

---

### Step 3: Search button click

Selectors tried in order:

```python
"button:has-text('Search')",
"button:has-text('Add dates')",
"a:has-text('Search')",
"[data-testid*='search']",
"button[type='submit']",
```

If none match or none are visible, `clicked` stays False but scraping continues. The destination page may not show listings until a Search/Add dates interaction; if the UI changed, you may get **0 properties** even when the page loads.

---

### Step 4: Property extraction

Property names are taken from:

```python
span with class containing "listing-card_name"
  → name = span.get("title") or span.get_text()
```

If Blueground changed this class or structure, extraction will fail and you’ll get **0 properties**.

---

## Failure points summary

| # | Failure point                    | Symptom          | What to check                                           |
|---|----------------------------------|------------------|---------------------------------------------------------|
| 1 | Playwright disabled / not installed | 0 properties     | `PLAYWRIGHT_ENABLED`, `playwright install chromium`     |
| 2 | Chromium not installed           | RuntimeError     | `playwright install`                                    |
| 3 | USA link filter too strict       | Few cities       | Consider adding `/furnished-apartments-*-usa` pattern   |
| 4 | Search button selectors outdated | 0 properties     | Inspect Blueground UI for new Search/Add dates elements |
| 5 | `listing-card_name` class changed| 0 properties     | Inspect rendered HTML for listing card structure        |
| 6 | `_fetch_without_browser` fallback| 0 properties     | `/destinations` has no listings in static HTML          |

---

## Debug script

Run:

```bash
PLAYWRIGHT_ENABLED=true python3 scripts/debug_blueground_asset.py
```

Ensure Chromium is installed:

```bash
python3 -m playwright install chromium
```

---

## Possible improvements

1. **Include alternate URL pattern**: Add support for `/furnished-apartments-{city}-{state}` (e.g. Atlanta, Austin) so major cities are scraped.
2. **Fallback selectors**: Broaden Search button and listing card selectors if the site structure changed.
3. **Logging**: Add debug logs for `usa_links` count, per-destination property counts, and whether the Search button was clicked.
