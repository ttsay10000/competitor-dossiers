# Research: Full-site snapshot (URLs + subdomains) and change-only event recording

Deep research on: (1) taking a snapshot of a website URL and all underlying subdomains to track added/removed pages, and (2) detecting changes on existing pages vs snapshot and recording only those as events.

---

## Part 1: Snapshot of website + subdomains and tracking added/removed pages

### 1.1 Discovering subdomains

**Certificate Transparency (CT) logs**

- **Idea:** Public CT logs list every issued certificate; certificate names include subdomains. No crawling required.
- **crt.sh:** Free API. Query `https://crt.sh/?q=%.example.com&output=json` to get all known subdomains for `example.com`. The `%` is a wildcard. Response is JSON list of cert rows; extract unique `name_value` (can be multiline for SANs).
- **Python:** Use `requests.get("https://crt.sh/?q=%.example.com&output=json")`, parse JSON, collect and dedupe hostnames from the name fields. Optional: filter by same base domain, exclude wildcards if you only want concrete hostnames.
- **Libraries:** `certspy` (PyPI) wraps crt.sh; CTFR, crt-search (GitHub) do similar. Censys (`censys`, `censys-platform`) offers richer search but is a separate service/API.
- **Limitation:** Only finds subdomains that have had an SSL cert issued. Misses subdomains with no HTTPS or not in CT.

**Other discovery methods (optional)**

- **DNS enumeration:** Brute-force subdomains from a wordlist (e.g. `www`, `blog`, `api`, `app`, `staging`) via DNS A/AAAA/CNAME lookups. More invasive and can be slow; useful to complement CT.
- **Sitemaps / robots.txt:** Often list only main domain; subdomains may have their own sitemaps (e.g. `blog.example.com/sitemap.xml`). Can be discovered after you have subdomain list (e.g. from CT).

**Recommendation for your flow:** Add an optional “subdomain discovery” step using crt.sh (or CertSPY): given `primary_domain`, query `%.{domain}` and get a list of hostnames. Then for each subdomain (or only those you care about), use the same “page discovery” logic below. You can scope to same eTLD+1 to avoid unrelated domains.

---

### 1.2 Discovering all pages (URL inventory)

**A. Sitemap-based (preferred when available)**

- **What:** Fetch `sitemap.xml` (and `sitemap.xml.gz`) from origin; parse `<loc>`; follow sitemap index links on same origin to get full URL list. You already do this in asset collector for *property-like* URLs ([app/collectors/asset.py](app/collectors/asset.py): `discover_sitemap`, `expand_sitemap`, `extract_links_from_sitemap`).
- **For “website changes” (digital footprint):** You don’t need to filter by “property-like”; you want *all* page URLs (or a defined subset, e.g. exclude `/tag/`, `/author/`). Reuse the same sitemap fetch/expand logic; optionally filter by path/prefix per competitor.
- **Libraries:** `ultimate-sitemap-parser` (PyPI): `sitemap_tree_for_homepage('https://www.example.org/')` then `tree.all_pages()` for URLs. Handles indexes, gzip, robots.txt discovery. Good for “full site URL list” without writing XML parser.
- **Storage:** Store the URL list (and optionally per-URL metadata) in the snapshot’s `structured_json`, e.g. `{ "urls": ["https://...", ...], "fetched_at": "..." }` or `{ "pages": [ { "url": "...", "content_hash": "..." }, ... ] }` so you can diff “previous URL set” vs “current URL set” to get added/removed.

**B. Link-following crawl (when sitemap missing or incomplete)**

- **What:** Start from seed URL(s), fetch HTML, extract same-domain links, enqueue unvisited, repeat with depth/max-pages limit. Restrict to same origin or same eTLD+1 so you don’t leave the “site.”
- **Scrapy:** `CrawlSpider` + `LinkExtractor`, `allowed_domains`, `follow=True`. Use `DEPTH_LIMIT` or custom `meta['depth']` to cap depth. Yields every URL you visit; collect into a set for “current URL inventory.”
- **Crawlee (Python):** Sitemap loader or custom crawler that follows links; can limit by depth and domain.
- **Distill / Firecrawl:** Distill’s sitemap monitor “follows links from a starting URL within the same domain/subdomain” to build a sitemap. Firecrawl has a “crawl” API that discovers pages and can return a list; you can combine with change tracking (see Part 2).
- **Politeness:** Respect robots.txt, rate limit (e.g. 1 req/s per host), and optionally limit total URLs per run (e.g. 500–2000) so runs stay bounded.

**C. Hybrid (fits your codebase)**

- **Step 1:** Try sitemap first (origin + optional subdomains from CT). If sitemap returns a good number of URLs, use that as the canonical “page list” for this run.
- **Step 2:** If no sitemap or very few URLs, fall back to link-following from homepage (and maybe a few key paths like `/about`, `/product`). Merge and dedupe so you have one “current URL set” per competitor (and per subdomain if you track subdomains separately).

---

### 1.3 Storing URL inventory and computing added/removed

- **Snapshot shape:** For the “homepage” (or a new “website_structure”) channel, store in `structured_json` something like:
  - `url_list`: list of URLs (normalized), or
  - `pages`: list of `{ "url": "...", "content_hash": "..." }` if you also fetch and hash each page (see Part 2).
- **Diff:** Load previous snapshot’s URL set (e.g. `prev_urls = set(s['url'] for s in prev_structured.get('pages', []))` or `set(prev_structured.get('url_list', []))`). Current run gives `curr_urls`. Then:
  - **Added:** `curr_urls - prev_urls`
  - **Removed:** `prev_urls - curr_urls`
- **Events:** Create one event per significant added/removed URL (or one “N pages added, M removed” summary event), with evidence (e.g. list of added/removed URLs). Dedupe by event type + title/window similar to `narrative.homepage_updated` so you don’t spam for the same structural change.
- **Scale:** If you have thousands of URLs, store only the URL list (and hashes for “existing pages” you actually diff for content); cap how many “added/removed” you report per run (e.g. top 20 + “and 15 more”) to avoid huge events.

---

## Part 2: Detecting changes on existing pages vs snapshot and recording only those as events

### 2.1 What you already do (and keep)

- **Per-page content hash:** You already compute `content_hash` on *normalized visible text* (strip script/style, normalize whitespace, remove noise patterns like dates, copyright, cookie text) in [app/collectors/homepage.py](app/collectors/homepage.py). That’s the right foundation.
- **Compare to previous snapshot:** In [app/runner.py](app/runner.py) `run_homepage` you load `latest.structured_json["pages"]`, build `prev_by_url`, and for each current page you set `content_changed = prev is None or (page.get("content_hash") != prev.get("content_hash"))`. You only create events when `content_changed` is true. So you **already** record only changed (or new) pages as events, not unchanged ones.
- **Dedupe:** `event_recently_created` with `DEDUPE_WINDOWS_DAYS` prevents duplicate events for the same URL/title within a window (14 days for `narrative.homepage_updated`, 30 for `narrative.coming_soon`).

So the “only record changes as events” behavior is already in place. The improvements below are about **reducing false positives** and **making the “existing pages” set align with a full-site snapshot** (Part 1).

---

### 2.2 Reducing false positives (only real content changes)

**Selective content (monitor a subset of the page)**

- **CSS/XPath:** Before hashing, extract only one region (e.g. `main`, `article`, `#content`). WebChanges and Distill do this: filter with CSS/XPath, then run diff/hash on that fragment. That avoids nav/footer/ads/sidebar churn.
- **Implementation:** In `homepage.py`, add an optional `content_selector` (CSS or XPath). If set, run BeautifulSoup/lxml on the HTML, select that node, then `extract_visible_text` on that subtree. Hash that instead of the full body. You could store both “full page hash” and “main content hash” and use the latter for change detection.

**Noise stripping (you already do this)**

- **Current:** `NOISE_PATTERNS` in homepage.py remove dates, “last updated”, copyright, cookie text. Keep extending this for competitor sites that inject “Viewed on …”, “Page X of Y”, or session tokens in the HTML.
- **Regex/delete_lines:** WebChanges uses `delete_lines_containing` and `re.sub` filters so that lines matching certain patterns are removed before storage/diff. Same idea: normalize before hashing.

**Dynamic content**

- **Wait for JS:** For SPAs, your `fetch_url_js` (Playwright) already helps. Some guides suggest 5–10s wait for network idle so counters/ads finish loading; you can add a short delay or wait for a selector if needed.
- **Ignore volatile elements:** If you move to DOM-based extraction (e.g. CSS selector), exclude elements with classes like `timestamp`, `view-count`, `advertisement`. That reduces “something changed” when only a number or ad changed.

**Stable comparison algorithm**

- **Firecrawl:** They state their comparison is resistant to whitespace and content order; iframe URLs are ignored. You can do the same: normalize whitespace (you do), and optionally sort lines or semantic blocks before hashing so that reordering doesn’t trigger a change (useful for list-style pages).

**Summary:** Keep per-URL content hash and “only emit event when content_changed.” Add optional main-content-only extraction and more noise patterns so that “changed” means “meaningful content change” in practice.

---

### 2.3 Aligning “existing pages” with full-site snapshot

- If you introduce a **full URL inventory** (Part 1), then “existing pages” are: URLs that appear in both previous and current inventory. For those, you already have (or will have) a previous `content_hash` in the snapshot.
- **New pages:** In current run, URL in current set but not in previous set. You can treat as “content_changed” (no previous hash), so one event per new page (or a single “N new pages” event) as you prefer.
- **Removed pages:** URL in previous set but not in current. Emit a “page removed” event (or summary) so the dossier shows both “added” and “removed.”
- **Unchanged pages:** Same URL in both sets and same `content_hash` → no event. Only **changed** (same URL, different hash) or **new**/removed get events. That keeps “only record those as events” precise.

---

### 2.4 Optional: richer diff (for display, not for “whether to record”)

- **Line-level diff:** Firecrawl’s `changeTracking` with `git-diff` returns a text diff (add/delete lines). Useful for showing “what changed” in the UI; not required for “did it change” (hash is enough for that).
- **Field-level (JSON):** If you extract structured data (e.g. pricing fields), you can compare previous vs current JSON and only alert when certain fields change. Your “coming_soon” phrase detection is a light version of this; you could extend to more schema-based extraction if needed.

You can add git-diff or JSON diff later for the dossier/executive summary display; the core “only record when content changed” remains hash-based comparison against the previous snapshot.

---

## Implementation options (mapped to your codebase)

### Option A: Sitemap-based URL inventory only (no subdomains)

- **Where:** New or extended logic in [app/collectors/homepage.py](app/collectors/homepage.py) or a small `website_structure` helper.
- **Steps:** For each competitor (and each homepage endpoint or primary_domain), fetch sitemap(s) from origin (reuse asset’s `discover_sitemap` / `expand_sitemap` or use `ultimate-sitemap-parser`). Collect all `<loc>` URLs (optionally filter by path). Store in snapshot `structured_json` as `url_list` or `pages: [{ url }]`.
- **Diff:** In runner, load previous snapshot’s URL set; compute added/removed; create “page added” / “page removed” events (with dedupe). Optionally still fetch each URL and compute content_hash for “existing” pages and create “content updated” events as you do now (or limit to a subset to keep run time bounded).

### Option B: Sitemap + subdomain discovery

- **Subdomains:** One-time or periodic step: from `primary_domain`, query crt.sh `%.domain.com`, parse JSON, get unique hostnames. Optionally restrict to same eTLD+1 and to a allowlist (e.g. `www`, `blog`, `app`).
- **Per (sub)domain:** For each base URL (main + subdomains), run sitemap discovery and/or link-following. Merge URL lists or keep per-subdomain; store in structured_json (e.g. `{ "by_origin": { "https://www.example.com": [...], "https://blog.example.com": [...] }, "url_list": [...] }`).
- **Diff:** Same as Option A but on the merged or per-origin URL set; added/removed can include subdomain pages.

### Option C: Link-following fallback when no sitemap

- **When:** Sitemap returns 0 or very few URLs, or sitemap fails.
- **How:** From homepage URL, do a bounded crawl (e.g. Scrapy or a simple loop: fetch page, extract same-origin links, enqueue unvisited, max 500–1000 URLs or depth 3). Use that as `url_list` and store/diff same as Option A.
- **Libraries:** Scrapy, Crawlee, or a small in-house crawler using your existing `fetch_url`/`fetch_url_js` and BeautifulSoup link extraction.

### Option D: Change detection improvements only (no new URL inventory)

- **Where:** [app/collectors/homepage.py](app/collectors/homepage.py), [app/rules/homepage_rules.py](app/rules/homepage_rules.py).
- **Add:** Optional main-content selector (CSS/XPath) for hashing; more `NOISE_PATTERNS`; optional line-sort before hash to ignore order changes. No new events for “added/removed” pages; only fewer false positives on “content updated.”

---

## Recommended order

1. **Short term:** Option D — refine change detection (selectors, noise, order-invariant hash) so “existing page changed” events are cleaner. No new storage shape.
2. **Next:** Option A — sitemap-based URL list, store in snapshot, diff for added/removed and emit page-added/page-removed events. Reuse asset’s sitemap helpers or add `ultimate-sitemap-parser`.
3. **If needed:** Option B (add subdomain discovery via crt.sh) and Option C (link-following fallback) for sites without sitemaps or with important subdomains.

---

## References

- **Sitemap:** [ultimate-sitemap-parser](https://pypi.org/project/ultimate-sitemap-parser/), [Crawlee SitemapRequestLoader](https://crawlee.dev/python/docs/examples/using-sitemap-request-loader).
- **Subdomains:** [crt.sh](https://crt.sh/?q=%.example.com&output=json), [CertSPY](https://pypi.org/project/certspy/), [CTFR](https://github.com/UnaPibaGeek/ctfr).
- **Change detection:** [Firecrawl Change Tracking](https://docs.firecrawl.dev/features/change-tracking) (new/same/changed/removed; git-diff; JSON diff), [WebChanges filters](https://webchanges.readthedocs.io/en/stable/filters.html) (css, xpath, html2text, sha1sum, delete_lines_containing), [Distill](https://distill.io/docs/web-monitor/change-history-and-highlighted-changes/), [TrackSimple guide](https://tracksimple.dev/blog/the-ultimate-guide-to-website-change-detection-building-a-robust-monitoring-system).
- **Crawl:** Scrapy `CrawlSpider` + `DEPTH_LIMIT`, [Scrapy depth/domain](https://docs.scrapy.org/en/latest/topics/spiders.html).
- **Snapshot comparison:** [Wayback CDX API](https://archive.org/developers/tutorial-compare-snapshot-wayback.html) (timestamp + digest for comparing two versions).
- **Existing code:** [app/collectors/asset.py](app/collectors/asset.py) (sitemap discover/expand), [app/collectors/homepage.py](app/collectors/homepage.py) (content hash, noise), [app/runner.py](app/runner.py) (run_homepage diff and events).
