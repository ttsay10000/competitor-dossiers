# How events are created and where descriptions come from

This doc explains **how** events get into the system, **who writes** the title/summary/why_it_matters, and **where to edit** so the signals you care about are more targeted.

---

## 1. How events are created (no “editing” of events after creation)

Events are **inserted once** when something is detected. There is no “update event description” flow.

- **Runner** (e.g. `run_homepage`, `run_talent`, …) decides **when** to create an event (e.g. “content hash changed”, “new job in capability X”).
- It calls a **rules** function that returns an **event dict** with `title`, `summary`, `why_it_matters`, `evidence`, etc.
- **`create_event(session, competitor_id, event_dict)`** in `app/runner.py` (around line 162) turns that dict into a row in the `events` table.

So: **what gets stored is exactly what the rules return at creation time.** To change how events look or what they say, you change the **rules** (and/or the **trigger logic** in the runner).

---

## 2. Who writes the descriptions (title / summary / why_it_matters)

Almost everything is **code-defined in rules modules**. One exception is social, where the **LLM** can supply the text.

| Channel / event type | Where the text comes from | File |
|----------------------|---------------------------|------|
| **Website changes** | **LLM + rules** – when enabled, LLM interprets old vs new text and subdomain/path; can override title/summary/why_it_matters and skip low-importance changes. Fallback: rules in `app/rules/homepage_rules.py`. | `app/llm_structured.py` (`interpret_website_change`), `app/runner.py` (`run_homepage`) |
| **Talent** (senior role, capability, surge, strategic) | **Rules (code)** – templates + job title, capability, count | `app/rules/talent_rules.py` |
| **Asset** (new market, pipeline, market exit) | **Rules (code)** – templates + market/property name | `app/rules/asset_rules.py` |
| **Press** | **Rules (code)** – item title + fixed summary/why_it_matters | `app/rules/press_rules.py` |
| **Social** | **LLM + rules** – title/summary can be `post["executive_summary"]` from LLM; why_it_matters is fixed in code | `app/rules/social_rules.py` |
| **Public records** | **Rules (code)** – item title + fixed summary/why_it_matters | `app/rules/public_records_rules.py` |

For **website changes**, the flow can use an **LLM** to interpret the change (old vs new visible text, URL subdomain/path) and produce a summary, importance flag, and suggested title; if the LLM says not important, no event is created. If the LLM is unavailable or fails, the fixed copy from `app/rules/homepage_rules.py` is used.

---

## 3. Website-change events in detail (so you can make them targeted)

### 3.1 When are they created? (trigger logic in runner)

- **`narrative.homepage_updated`**  
  Created in **`app/runner.py`** in `run_homepage` when, for a given URL, the **content_hash** of the visible text (after noise normalization) **changed** vs the previous snapshot.  
  So: “something meaningful changed on this page” (no detail on *what* – we don’t store a diff).

- **`narrative.coming_soon`**  
  Created when that same page **also** has at least one **coming-soon phrase** (see list below). So the **trigger** is: content changed **and** phrase detected.

Both are created **per URL** (one event per page that changed / had a phrase). Dedupe is by event type + title (including URL) within a time window (e.g. 14 days for homepage_updated, 30 for coming_soon).

### 3.2 Exact text (where to edit for more targeted copy)

All of this is in **`app/rules/homepage_rules.py`**:

**Homepage updated**

- **Title:** `"Homepage or product page updated"`
- **Summary:** `"Meaningful change detected on a tracked page (content hash changed)."`
- **Why it matters:** `"Signals possible messaging, product, or positioning update."`

**Coming soon**

- **Title:** `"Coming soon or pipeline signal on page"`
- **Summary:** `f"Page contains pipeline/market signal: \"{phrase_or_snippet}\"."`  (the phrase is the one we matched, e.g. `"coming soon"`)
- **Why it matters:** `"May indicate new market entry, product launch, or beta."`

To make these **more targeted**, edit those strings (and, if you want, add more evidence into the `evidence` dict and surface it in the dossier/executive summary).

### 3.3 More detail: what changed + subdomain/path + LLM

- **Per-page visible text:** Each snapshot’s `structured_json.pages[]` now includes `visible_text_snippet` (first 4000 chars of visible text). That gives “old” and “new” content for diffing when a page’s `content_hash` changes.
- **URL context:** `app/utils.py` provides `parse_url_context(url)` → `{ "subdomain", "path", "host" }` (e.g. `blog.example.com/locations` → subdomain=`blog`, path=`/locations`). This is passed into the LLM and stored in event `evidence` (`evidence.subdomain`, `evidence.path`).
- **LLM interpretation:** `app/llm_structured.interpret_website_change(competitor_name, url, url_context, old_snippet, new_snippet, coming_soon_phrases)` calls the LLM with that context. The model returns:
  - **summary** – 1–2 sentences on what changed
  - **is_important** – if false, the runner skips creating the event
  - **reason** – why it’s important or not
  - **suggested_title** – short event title
  The runner uses these to set the event’s title/summary/why_it_matters (and to skip trivial changes). If the API is missing or the call fails, behavior falls back to the fixed rules copy and we still create the event.

### 3.4 Which “coming soon” phrases create an event (how to narrow/broaden)

In **`app/rules/homepage_rules.py`**, **`COMING_SOON_PHRASES`** is the list we match against (substring in page text, case-insensitive):

```python
COMING_SOON_PHRASES = [
    "coming soon",
    "launching soon",
    "beta",
    "coming to",
    "stay tuned",
    "coming in 2025",
    "coming in 2026",
    "opening soon",
    "now available in",
    "expand to",
    "new market",
]
```

- **More targeted:** Remove broad phrases (e.g. `"beta"`, `"stay tuned"`), or add **required context** (e.g. only create an event if both “coming soon” and “market”/“city” appear). The latter would need a small code change in `run_homepage` or in a helper used by the rules.
- **Broader:** Add more phrases (e.g. “launching in”, “opening in Q2”).

So: **descriptions** = rules in `homepage_rules.py`; **which pages count as “coming soon”** = `COMING_SOON_PHRASES` + (optionally) extra logic in the runner or rules.

---

## 4. Making *any* signal more targeted – checklist

1. **Tighten or loosen when the event is created**  
   In the **runner** (e.g. `run_homepage`): change the condition (e.g. only create if certain paths, or only if phrase list is non-empty, or add a minimum “amount of change” if you ever add such a metric).

2. **Change the wording**  
   In the **rules** module for that channel: edit `title`, `summary`, `why_it_matters` (and optionally `evidence`) so the description matches what you care about.

3. **Narrow or broaden what counts as a “signal”**  
   In the rules (or runner):  
   - **Homepage:** `COMING_SOON_PHRASES` + optional extra checks (e.g. require phrase + location/market).  
   - **Talent:** keyword lists / capability logic in `app/rules/talent_rules.py` (e.g. `CAPABILITY_KEYWORDS`, `SENIOR_TITLES`).  
   - **Press:** classification/relevance in `app/rules/press_rules.py` and LLM in `app/llm_structured.py`.  
   - **Social:** what gets marked “executive-relevant” and what the LLM puts in `executive_summary` (in `app/llm_structured.py` / social pipeline).

4. **Dedupe / volume**  
   - **Website-change events** (`narrative.homepage_updated`, `narrative.coming_soon`): Dedupe is **per refresh**, not time-based. We use **`event_already_created_since_baseline`**: “Has an event with this type + title already been created since `competitor.reporting_baseline_at`?” So at most one event per URL per refresh cycle; when you do a full refresh, the baseline advances and old website events are pruned, so the next run can create events again. If no baseline is set, we fall back to a time window (14 / 30 days).  
   - **Other channels** (talent, asset, press, social, etc.): Still use **`event_recently_created`** with **`DEDUPE_WINDOWS_DAYS`** (calendar-based: “same type + title in the last N days”).

---

## 5. Quick reference – files to touch for “targeted” website signals

| Goal | Primary place |
|------|----------------|
| Change title/summary/why_it_matters for “page updated” or “coming soon” | `app/rules/homepage_rules.py` – `build_homepage_updated_event`, `build_coming_soon_event` |
| Change which phrases trigger “coming soon” | `app/rules/homepage_rules.py` – `COMING_SOON_PHRASES` |
| Only create events for certain paths or when phrase list is non-empty | `app/runner.py` – `run_homepage` (loop over `structured["pages"]`, conditions before `build_*_event` / `create_event`) |
| Change how often we allow the same event type per competitor | `app/runner.py` – `DEDUPE_WINDOWS_DAYS`, and the `event_recently_created` calls in `run_homepage` |

Events are **not** “changed” after creation; they’re **created once** with whatever the rules and runner decide at that moment. So making signals more targeted means editing those rules and that trigger logic.
