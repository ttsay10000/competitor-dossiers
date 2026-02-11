# Jobs data flow: what is parsed and what goes to the LLM

This doc describes what job data exists at each step and **what is actually sent to the LLM** for categorization. If categorization is off, it’s often because the model only sees `title`, `dept`, and `location` (and sometimes those are empty).

---

## Step 0: Collect (`collect_talent_snapshot`)

**Source:** Lever API, Greenhouse API, Ashby API, or generic HTML scrape.

**Parsed job shape (after `normalize_jobs`):**

| Field         | Source (Lever example) | Notes |
|--------------|--------------------------|-------|
| `job_id`     | `id` or `shortCode`      | Can be `None` for HTML scrape |
| `title`      | `text`                   | Required |
| `location`   | `categories.location`    | Often empty for generic HTML |
| `dept`       | `categories.team`        | Lever “team”; Greenhouse first department; Ashby `department` |
| `posted_date`| `createdAt`              | ISO string or None |
| `url`        | `hostedUrl`              | Can be None for fragment links |

**Sample job after collect (Lever):**

```json
{
  "job_id": "abc-123",
  "title": "Senior Software Engineer",
  "location": "San Francisco, CA",
  "dept": "Engineering",
  "posted_date": "2025-01-15T00:00:00.000Z",
  "url": "https://jobs.lever.co/company/abc-123"
}
```

**Sample job from generic HTML (e.g. WizeHire):**

```json
{
  "job_id": null,
  "title": "Front Desk Agent",
  "location": null,
  "dept": null,
  "posted_date": null,
  "url": null
}
```

So for many sources the LLM only sees **title**; `dept` and `location` are often empty.

### Why are department and location empty for many jobs?

- **API sources (Lever, Greenhouse, Ashby):** We do extract `dept` and `location` from the provider’s JSON (e.g. Lever `categories.team` / `categories.location`, Greenhouse `departments[0].name` / `location.name`). They’re only empty if the employer didn’t set them in the ATS.
- **Generic HTML (career pages we scrape):** When we fall back to `extract_jobs_from_html` or `extract_jobs_from_headings`, we only parse the **job title** (from link text or headings) and optionally `posted_date` and `url`. We **never** look for department or location in the page structure, so we set `dept` and `location` to `None`. Any competitor using a non-API career page (e.g. Kula, WizeHire, custom “Join our team” HTML) will have all jobs with empty dept/location unless we extend the extractors to read those fields from the same card/section as each job.

---

## Step 1: Build structured (`build_structured_json` in talent collector)

**Input:** Raw snapshot `{ provider, source_url, raw_content, raw_hash, jobs }`.

**Output:** Same job list, no extra parsing:

```json
{
  "provider": "lever",
  "source_url": "https://boards-api.greenhouse.io/...",
  "fetched_at": "2025-02-10T12:00:00.000000+00:00",
  "jobs": [
    { "job_id": "...", "title": "...", "location": "...", "dept": "...", "posted_date": "...", "url": "..." },
    ...
  ]
}
```

So **into build_structured** you have the normalized job objects above; no new fields are added here.

---

## Step 2: Enrich with LLM (`enrich_jobs_with_llm`)

**Input:** `structured["jobs"]` — list of dicts with at least `title`, `dept`, `location` (others ignored for the prompt).

**What is actually sent to the LLM:** Only three fields per job, as plain text lines. No JSON, no `job_id`, no `posted_date`, no `url`.

**Code that builds the user message** (`app/llm_structured.py`):

```python
lines = []
for i, j in enumerate(batch):  # batch = jobs[:150]
    title = (j.get("title") or "").strip()
    dept = (j.get("dept") or "").strip()
    location = (j.get("location") or "").strip()
    lines.append(f"{i}: title={title!r} dept={dept!r} location={location!r}")
user = "Jobs:\n" + "\n".join(lines)
```

**Sample payload sent to the LLM (first 5 jobs):**

```
Jobs:
0: title='Senior Software Engineer' dept='Engineering' location='San Francisco, CA'
1: title='Front Desk Agent' dept='' location=''
2: title='Director of Revenue Management' dept='Revenue' location='Remote'
3: title='Housekeeping Supervisor' dept='' location='Miami, FL'
4: title='Marketing Manager' dept='Marketing' location='New York, NY'
```

So the model only sees:
- **index** (0-based)
- **title** (string, required)
- **dept** (string, often `''` for HTML scrapes)
- **location** (string, often `''`)

If most of your jobs come from generic HTML or ATSes that don’t expose department/location, the LLM is categorizing from **title only**, which can hurt quality (e.g. “Operations Manager” could be property or corporate).

**System prompt:** Asks for a JSON array with one object per job: `index`, `functional_area` (one of the fixed list), `is_senior` (boolean). No markdown.

**After LLM:** Each job gets `functional_area` and `is_senior` set (and if the LLM returns “Other”, the code falls back to rule-based `job_functional_area` from `talent_rules`).

---

## Step 3: Persist and diff

**Input:** Enriched jobs (with `functional_area`, `is_senior`) from Step 2.

**Then:** `assign_job_flags(job)` adds:
- `capability_bucket` (from title/dept keywords: ai_data, strategy_finance, partnerships, real_estate, or None)
- `is_senior` (already set by LLM; if missing, rule-based from title)
- `functional_area` (already set by LLM; if missing, rule-based)
- `is_strategic` (derived from capability_bucket)

So **into Step 3** the jobs already have LLM (or rule) `functional_area` and `is_senior`; Step 3 just adds flags and persists. The “data going into the LLM” is in Step 2, not Step 3.

---

## Why categorization might be wrong

1. **Sparse context:** Only `title`, `dept`, `location` are sent; for many sources `dept` and `location` are empty.
2. **Title-only ambiguity:** e.g. “Operations Manager” or “Manager” without dept is hard to split between Property operations vs Business & Strategy.
3. **No job description:** The LLM never sees description text, so it can’t use role details.

**Ways to improve (for later):**
- Include a short snippet of job description in the prompt when available (e.g. from Lever/Greenhouse `content`).
- Pass `posted_date` or `url` as hints (e.g. “corporate” vs “property” URLs) if you have a reliable heuristic.
- Tighten the system prompt with more examples for ambiguous titles when `dept`/`location` are missing.

---

## How to inspect real payloads

Install the project’s dependencies first so the DB driver is available (required for scripts that read from the DB):

```bash
pip install -r requirements.txt
```

The app uses the **psycopg** (PostgreSQL) driver; it’s listed as `psycopg[binary]>=3.2` in `requirements.txt`. If you see `ModuleNotFoundError: No module named 'psycopg'`, you’re running the script in an environment where those deps aren’t installed—use the project’s virtualenv or run `pip install -r requirements.txt` in the environment you use.

Then from repo root (with `.env` containing `DATABASE_URL`):

```bash
python scripts/debug_talent_llm_input.py [competitor_name]
```

This prints the **exact payload** sent to the LLM (“Jobs:” + lines), rule-based classification counts, and (if `OPENAI_API_KEY` is set) post-enrichment counts and which jobs stayed in “Other”.

For a short sample of job dicts and the LLM user message only:

```bash
python scripts/sample_jobs_llm_payload.py [competitor_name]
```
