# Asset data path: property count and classification by state

This traces where asset property **numbers** and **state classification** come from, and where state can go wrong.

---

## 1. Collection (run_asset)

**Entry:** `runner.run_asset(competitor_name)` → for each competitor, `collect_asset_snapshot(endpoint.url, ...)` then `build_asset_structured(snapshot)` then `enrich_properties_with_llm(properties, raw_content)`.

- **`app/collectors/asset.py`**  
  - Strategy depends on competitor/source (sitemap, HTML, Lark blocks, Blueground, API, etc.).  
  - Each strategy returns a list of property dicts with at least `name`, `url`; often `market`, `state`, `city`, `details`.  
  - For **AvantStay-style** URLs (`/{numeric_id}/{destination_slug}/{property_slug}`), state is **not** set here; it’s set later from the destination slug.  
  - For **Lark-style** blocks, `_extract_properties_via_llm_from_blocks` asks the LLM for state per block; that can be wrong if the block text is ambiguous.  
  - For **sitemap-only** URLs, properties often have no state until enrichment or dossier URL parsing.

- **`app/llm_structured.py` – `enrich_properties_with_llm()`**  
  - First runs **`_assign_state_from_url(p)`** on every property (AvantStay-style only: parses path, gets destination slug, resolves to state via `resolve_destination_slug_to_state`).  
  - Then, in batches, calls the LLM to assign **state** (and optional city) from page text (if `raw_content` and small enough) or from url/name/market only.  
  - Only fills state when **missing**; it does not overwrite URL-derived state with "Other".  
  - **Stored** in `Snapshot.structured_json["properties"]` with whatever `state` (and city/market) we have.

So after run_asset:

- **Property count** = length of that list (correct if the collector strategy and filters are correct).  
- **State** = URL-derived (AvantStay-style) when slug is in the map, else LLM or collector-set state (can be wrong if slug missing, LLM misreads, or collector doesn’t set it).

---

## 2. State-from-URL (used in collector and dossier)

**Slug → state map and parsing:**

- **`app/diff/asset_diff.py`**  
  - **`_DESTINATION_SLUG_TO_STATE`** – static map: destination slug (e.g. `coachella-valley`, `austin-tx`) → full US state name.  
  - **`resolve_destination_slug_to_state(slug)`** – if slug ends with `-XX` (2-letter abbrev), uses `US_STATE_ABBREV`; else looks up in `_DESTINATION_SLUG_TO_STATE`.  
  - **`_parse_avantstay_style_path(path)`** – from path like `/12345/destination-slug/property-slug` or `/12345/destination-slug` returns `destination-slug`.

- **`app/llm_structured.py` – `_assign_state_from_url(prop)`**  
  - If `prop` already has state, returns as-is.  
  - Parses `prop["url"]` path, gets destination slug via `_parse_avantstay_style_path`, then `resolve_destination_slug_to_state(slug)`.  
  - If state found: sets `out["state"]` and `out["market"]` (e.g. "Place Name, State").  
  - So **classification by state can be “a little off”** if:  
    - A destination slug is **missing** from `_DESTINATION_SLUG_TO_STATE` (or wrong state in the map).  
    - Slug has a **typo/variant** (e.g. `newport-beach` vs `newport-beach-ca`).  
    - **Non–AvantStay** competitors don’t use this path; their state comes only from collector or LLM enricher.

---

## 3. Dossier: how “by state” is built

**Entry:** `routes/dossier.build_dossier_context()`.

- **Raw properties**  
  - `raw_asset_props = latest_asset.structured_json["properties"]`  
  - `asset_props = [_assign_state_from_url(p) for p in raw_asset_props]`  
  - So we **only** re-apply URL-derived state here; we do **not** re-run per-property LLM. Any state already stored from the last run is kept unless URL derivation fills a missing state.

- **Location label per property**  
  - `loc = infer_location_for_property(p)` for each `p` in `asset_props`.  
  - **`app/diff/asset_diff.py` – `infer_location_for_property(prop)`**:  
    1. Prefers **`prop["state"]`** (then `prop["city"]`) → normalized to “State” or “State - City”.  
    2. Else **market / location / region**.  
    3. Else **URL**:  
       - Placemakr-style trailing `-XX` (e.g. `saltlakecity-ut`) → state from `US_STATE_ABBREV`.  
       - AvantStay-style path → `_parse_avantstay_style_path` → `resolve_destination_slug_to_state(dest_slug)` → state or slug title-cased (e.g. "Newport Beach").  
       - Other path segments (e.g. under `/locations/`, first segment) as fallback.  
    4. Else **"Unspecified"**.

- **Aggregation**  
  - `location_counts[loc]` = count of properties per `loc`.  
  - `properties_by_location` = list of `{location, count, keys}`.  
  - Then **`aggregate_state_and_state_city_rows(properties_by_location)`** to merge “State - City” into state-level rows for the LLM.  
  - **`clean_location_display_for_dossier(competitor_name, ...)`** (in executive_summary / LLM): one LLM call on **(location, count)** rows to map regions to states and tidy display (e.g. “Coachella Valley” → California). It does **not** change per-property state.  
  - **`properties_by_state`** = rows whose `location` is in the US states list; **`properties_other`** = the rest.

So on the dossier:

- **Property numbers** = same as collected (plus any `location_counts` from snapshot for special cases like Landing).  
- **Classification by state** = driven by `prop["state"]` (from last run + URL fill-in) and `infer_location_for_property`. If stored state or slug→state is wrong, the “by state” breakdown will be off until the next asset run (and/or until the slug map or enricher is fixed).

---

## 4. Where to fix “classification by state” when it’s off

1. **AvantStay-style (or same URL pattern) competitor**  
   - **Add or correct destination slugs** in `app/diff/asset_diff.py`: `_DESTINATION_SLUG_TO_STATE`.  
   - Ensure **`_parse_avantstay_style_path`** matches the actual URL shape (e.g. 2-segment sitemap URLs).  
   - Re-run asset so new state is stored in `structured_json["properties"]`.

2. **Other competitors (e.g. Lark, sitemap-only, custom HTML)**  
   - State comes from **collector** (e.g. Lark block LLM) and/or **`enrich_properties_with_llm`**.  
   - Improve prompts or add **URL-based state** for their URL pattern (e.g. in `infer_location_for_property` or a dedicated parser used in the collector/enricher).

3. **Dossier display only**  
   - **`clean_location_display_for_dossier`** only sees aggregated (location, count) rows; it can merge regions into states but can’t fix per-property state. Fix at collection/enrichment or slug map for lasting correct counts by state.

---

## 5. Quick reference: key functions and files

| What | Where |
|------|--------|
| Collect properties (all strategies) | `app/collectors/asset.py` |
| Enrich state (URL + LLM) at run_asset | `app/llm_structured.py` → `enrich_properties_with_llm`, `_assign_state_from_url` |
| Destination slug → state map | `app/diff/asset_diff.py` → `_DESTINATION_SLUG_TO_STATE`, `resolve_destination_slug_to_state` |
| AvantStay path parsing | `app/diff/asset_diff.py` → `_parse_avantstay_style_path` |
| Location label for grouping (dossier) | `app/diff/asset_diff.py` → `infer_location_for_property` |
| Dossier: raw props → URL state → location → by state | `app/routes/dossier.py` (asset_props, location_counts, properties_by_location, properties_by_state) |
| Tidy location display (aggregated rows) | `app/executive_summary.py` → `clean_location_display_for_dossier` (and related aggregation) |

If “aka” is a specific competitor (e.g. a company name), the same path applies; which of the above fixes applies depends on whether their asset URLs are AvantStay-style (slug map) or something else (collector + enricher).
