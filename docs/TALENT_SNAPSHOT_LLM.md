# Talent Snapshot: LLM Role Classification

How job listings get **functional_area** and **is_senior** for the dossier “Talent snapshot” and why many roles end up in **Other**.

## Data flow

1. **Collect** (`app/collectors/talent.py`)  
   Jobs are fetched from Lever, Greenhouse, Ashby, or generic HTML. Each job is normalized to:
   - `title` — job title
   - `dept` — department/team (often `None` for generic scraped pages)
   - `location` — location string (optional)
   - `job_id`, `posted_date`, `url`

2. **Enrich** (`app/llm_structured.enrich_jobs_with_llm`)  
   The **only** fields sent to the LLM are:
   - `index` (0-based)
   - `title`
   - `dept`
   - `location`  
   So the model sees lines like:
   ```text
   0: title='Software Engineer' dept='Engineering' location='Remote'
   1: title='Guest Experience Manager' dept='' location='Miami, FL'
   2: title='Manager' dept=None location=''
   ```

3. **LLM output**  
   For each index the model returns:
   - `functional_area`: one of the canonical areas (see below)
   - `is_senior`: boolean (C-level, VP, Head of, Director → true)

4. **Post-processing**  
   - `_normalize_functional_area()` maps the model’s string to a canonical area (exact match + aliases for common variants like "Sales", "Property operations", "HR", "Operations"). **Any unrecognized value becomes `"Other"`.**  
   - If the LLM (or normalization) returns `"Other"`, we run the **rule-based** `job_functional_area()` from `app/rules/talent_rules.py` (keyword match on title + dept). If that also fails to match, the job stays **Other**.  
   - Only the first 150 jobs are sent to the LLM; jobs beyond that get **rule-based** `functional_area` and `is_senior` so every job is classified and the dossier does not under-count categories.

## Canonical functional areas (LLM + display)

Defined in `app/llm_structured.TALENT_FUNCTIONAL_AREAS` and displayed in `app/rules/talent_rules.FUNCTIONAL_AREA_DISPLAY_ORDER`:

- Sales / Growth  
- Marketing  
- Business & Strategy  
- AI / Data  
- Product  
- Engineering  
- Property operations  
- **Other**

## Why so many “Other”?

1. **Sparse input**  
   Generic career pages often yield only `title` (e.g. “Manager”, “Coordinator”). With no `dept` and no location, the model has little signal and may default to Other.

2. **Strict normalization**  
   If the model returns a variant (e.g. “Sales”, “Property Operations”) that isn’t in the canonical list or the small alias set in `_normalize_functional_area`, we map it to Other.

3. **Rule fallback is keyword-based**  
   `job_functional_area()` only matches phrases in `FUNCTIONAL_AREA_KEYWORDS`. Ambiguous titles (“Manager”, “Operations”) may not match any keyword and stay Other.

4. **Prompt says “Other only when clearly does not fit”**  
   So the model is instructed to use Other sparingly, but with minimal context (e.g. title-only) it often has no better choice.

## Inspecting the underlying data

To see the **exact payload** sent to the LLM and which jobs end up in Other after enrichment:

see `app/llm_structured.py` (`enrich_jobs_with_llm` and the talent prompt building) and `app/rules/talent_rules.py`. The “Jobs:” block (index, title, dept, location) is built there; enrichment returns counts by `functional_area` and flags jobs that stay in Other. Adjust keywords or prompt/areas in those modules to fix classification.
