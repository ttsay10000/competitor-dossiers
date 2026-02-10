"""
LLM-based structuring for:
- asset (state/city)
- talent (functional_area, is_senior)
- press (business relevance, topic, 1-line summaries)

Primary path: always call OpenAI; on missing key or API failure we return
input unchanged (or apply simple heuristics) so rules-based logic downstream
still works and the app remains usable without an API key.
"""
import json
import re
from typing import Any, List, Optional

from .collectors.http import fetch_url, fetch_url_js, USER_AGENT_BROWSER

# Max characters of page text when enricher reads raw_content (fit context, control cost).
_ENRICH_RAW_MAX_CHARS = 14_000

# Functional areas for talent (must match talent_rules.FUNCTIONAL_AREA_DISPLAY_ORDER semantics).
TALENT_FUNCTIONAL_AREAS = [
    "Sales / Growth",
    "Marketing",
    "Business & Strategy",
    "AI / Data",
    "Product",
    "Engineering",
    "Property operations",
    "Other",
]


def _openai_client():
    from .config import settings
    if not settings.openai_api_key:
        return None
    try:
        from openai import OpenAI
        return OpenAI(api_key=settings.openai_api_key)
    except ImportError:
        return None


def _parse_json_response(content: str) -> Any:
    content = (content or "").strip()
    if content.startswith("```"):
        content = re.sub(r"^```\w*\n?", "", content).rstrip("`\n")
    return json.loads(content)


# Data attributes that often contain property location on cards (so LLM can see them when enriching).
_ENRICH_LOCATION_ATTRS = ("data-city", "data-state", "data-region", "data-market", "data-location", "data-address", "aria-label")


def _html_to_text_for_enricher(html: str, max_chars: int = _ENRICH_RAW_MAX_CHARS) -> str:
    """Reduce HTML to plain text for LLM (strip scripts, body text, location data-*, truncate). Mirrors asset collector."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return html[:max_chars] + "\n[... truncated]" if len(html) > max_chars else html
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(["script", "style", "noscript"]):
        tag.decompose()
    body = soup.find("body") or soup
    location_lines = []
    for el in (body.find_all(True) if body else []):
        parts = []
        for attr in _ENRICH_LOCATION_ATTRS:
            val = el.get(attr)
            if val and isinstance(val, str) and (val := val.strip()) and len(val) < 200:
                parts.append(f"{attr}={val}")
        if parts:
            location_lines.append(" ".join(parts))
    text = body.get_text(separator="\n", strip=True)
    if location_lines:
        text = text + "\n\n[Location metadata from page]\n" + "\n".join(location_lines[:500])
    text = re.sub(r"\n{3,}", "\n\n", text)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n[... truncated]"
    return text


def enrich_properties_with_llm(
    properties: List[dict],
    raw_content: Optional[str] = None,
) -> List[dict]:
    """
    Use LLM to assign state (and optionally city) to each property. On failure or no API key, return list unchanged.
    When raw_content is provided, the LLM reads the page text and assigns state/city from page context; otherwise
    it infers from url/name/market only.
    """
    if not properties:
        return properties
    client = _openai_client()
    if not client:
        return properties

    # Batch up to 100 to stay within context
    batch = properties[:100]
    lines = []
    for i, p in enumerate(batch):
        url = (p.get("url") or "").strip()
        name = (p.get("name") or "").strip()
        market = (p.get("market") or "").strip()
        lines.append(f"{i}: url={url!r} name={name!r} market={market!r}")

    if raw_content and raw_content.strip():
        # LLM reads raw page and assigns state (and optional city) from page context
        page_text = _html_to_text_for_enricher(raw_content.strip())
        system = """You are a data enricher for US real estate/hospitality property lists. Locations will be summarized by state only.
Given the page text below and a list of properties (index, url, name, market), assign a state to each property using only information from the page text (e.g. cards, addresses, subheadings).
Output a JSON array with one object per property. Each object must have: "index" (integer), "state" (full US state name only, e.g. "Texas" or "California"—no city in state field), "city" (optional, omit if unknown).
Use only standard US state names. For anything that does not neatly fit in a specific US state—non-property pages (career site, privacy, legal), unclear location, or non-US—use state "Other". Return only the JSON array, no markdown."""
        user = f"Page text:\n\n{page_text}\n\nProperties (assign state from page text above; use Other if not clearly in a US state):\n" + "\n".join(lines)
    else:
        # Infer from url/name/market only (no page context)
        system = """You are a data enricher for US real estate/hospitality property lists. Locations will be summarized by state only.
Given a list of properties (index, url, name, market), output a JSON array with one object per property.
Each object must have: "index" (integer), "state" (US state full name only, e.g. "Texas"—no city), "city" (optional, omit if unknown).
Use only standard US state names. For anything not clearly in a specific US state (unclear, career site, privacy, non-property URL, etc.) use state "Other". Return only the JSON array, no markdown."""
        user = "Properties:\n" + "\n".join(lines)

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            max_tokens=4000,
            temperature=0.1,
        )
        content = (resp.choices[0].message.content or "").strip()
        if not content:
            return properties
        data = _parse_json_response(content)
        if not isinstance(data, list):
            return properties
        by_index = {int(item["index"]): item for item in data if isinstance(item, dict) and "index" in item}
        result = []
        for i, p in enumerate(properties):
            out = dict(p)
            if i in by_index:
                # Only fill state/city when missing (don't overwrite collector-set values with "Other")
                enricher_state = (by_index[i].get("state") or "Other").strip() or "Other"
                enricher_city = (by_index[i].get("city") or "").strip() or None
                if not (out.get("state") or "").strip():
                    out["state"] = enricher_state
                if enricher_city and not (out.get("city") or "").strip():
                    out["city"] = enricher_city
            result.append(out)
        return result
    except Exception:
        return properties


def enrich_jobs_with_llm(jobs: List[dict]) -> List[dict]:
    """
    Use LLM to assign functional_area and is_senior to each job. On failure or no API key, return list unchanged.
    """
    if not jobs:
        return jobs
    client = _openai_client()
    if not client:
        return jobs

    areas_str = ", ".join(TALENT_FUNCTIONAL_AREAS)
    batch = jobs[:150]
    lines = []
    for i, j in enumerate(batch):
        title = (j.get("title") or "").strip()
        dept = (j.get("dept") or "").strip()
        location = (j.get("location") or "").strip()
        lines.append(f"{i}: title={title!r} dept={dept!r} location={location!r}")

    system = f"""You are a data enricher for job listings at real estate/hospitality companies.
Given a list of jobs (index, title, dept, location), output a JSON array with one object per job.
Each object must have: "index" (integer), "functional_area" (exactly one of: {areas_str}), "is_senior" (boolean).
Treat as senior: C-level (CEO, CFO, etc.), VP, Vice President, Head of, Director, and similar. Otherwise is_senior is false.
Return only the JSON array, no markdown."""

    user = "Jobs:\n" + "\n".join(lines)

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            max_tokens=4000,
            temperature=0.1,
        )
        content = (resp.choices[0].message.content or "").strip()
        if not content:
            return jobs
        data = _parse_json_response(content)
        if not isinstance(data, list):
            return jobs
        by_index = {int(item["index"]): item for item in data if isinstance(item, dict) and "index" in item}
        result = []
        for i, j in enumerate(jobs):
            out = dict(j)
            if i in by_index:
                fa = (by_index[i].get("functional_area") or "Other").strip()
                out["functional_area"] = fa if fa in TALENT_FUNCTIONAL_AREAS else "Other"
                out["is_senior"] = bool(by_index[i].get("is_senior"))
            result.append(out)
        return result
    except Exception:
        return jobs


# --- Press: classification, dedupe, and summaries --------------------------------

PRESS_TOPICS = [
    "fundraising",
    "new_hotel_opening",
    "new_partnership",
    "restructuring_or_layoffs",
    "other_business",
    "executive_interview",
    "promo_or_brand_marketing",
    "irrelevant",
]


def _press_outlet_from_item(item: dict) -> str:
    outlet = (item.get("source") or "").strip()
    if outlet:
        return outlet
    url = (item.get("url") or item.get("link") or "").strip()
    if not url:
        return ""
    # Best-effort: derive domain from URL
    try:
        import urllib.parse

        parsed = urllib.parse.urlparse(url)
        return (parsed.netloc or "").lower()
    except Exception:
        return ""


def _classify_press_headlines_with_llm(
    competitor_name: str,
    items: List[dict],
) -> List[dict]:
    """
    Classify press items using only headlines/metadata.

    Returns a list of dicts (same length/order as items) with added keys:
    - is_about_company: bool
    - topic: one of PRESS_TOPICS
    - is_promo: bool (true when mainly brand/positioning fluff)
    """
    if not items:
        return items
    client = _openai_client()
    if not client:
        # No API key: fall back to simple heuristics.
        return _classify_press_headlines_fallback(competitor_name, items)

    batch = items[:200]
    lines = []
    for i, it in enumerate(batch):
        title = (it.get("title") or "").strip()
        url = (it.get("url") or it.get("link") or "").strip()
        outlet = _press_outlet_from_item(it)
        lines.append(f"{i}: title={title!r} outlet={outlet!r} url={url!r}")

    topics_str = ", ".join(PRESS_TOPICS)
    system = (
        "You are classifying news headlines about hospitality / real estate / travel companies.\n"
        f"Possible topics are: {topics_str}.\n"
        "For each item decide if it is primarily about the target company AS A BUSINESS (its funding, strategy, "
        "corporate actions, or portfolio), and whether it is a business-focused story.\n"
        "Business-focused examples: fundraising rounds, new property or market openings, new partnerships or distribution deals, "
        "restructuring/layoffs, significant market expansion or exits, major product or strategy shifts, "
        "or in-depth interviews / Q&A with senior executives of the target company (those should use topic 'executive_interview').\n"
        "VERY IMPORTANT: Articles that are guides, checklists, ROI explainers, destination or travel guides, "
        "homeowner education, generic investment advice, thought-leadership, comparisons like 'X vs Y', "
        "or awards/roundups (e.g. 'Best homes', 'Guest review roundup') should be treated as marketing/blog content, "
        "NOT business news, even if the company name appears in the title or URL.\n"
        "Those pieces should normally have topic 'promo_or_brand_marketing' and is_promo=true, and is_about_company=false "
        "unless the article is principally about a discrete corporate action by the company.\n"
        "When the URL clearly looks like the company's own marketing site or blog (e.g. contains the company name plus "
        "paths like '/blog', '/destinations', '/guides', '/owners', '/awards', '/itinerary'), be extra strict: "
        "only treat the item as business-focused if the headline clearly states a concrete business event such as "
        "'<Company> raises...', '<Company> acquires...', '<Company> launches...', '<Company> partners with ...'.\n\n"
        "Examples of NONSENSE (set is_about_company=false; use topic 'promo_or_brand_marketing' and is_promo=true where applicable):\n"
        "- Listicles/roundups where the company is one of many: '10 Best Vacation Rental Companies', 'Top Short-Term Rental Platforms to Watch', 'Best Places to Stay in Miami' (company just listed).\n"
        "- Industry trend pieces that mention the company in passing: 'Why the Short-Term Rental Market Is Cooling', 'Travel Industry Faces Headwinds' (company named once).\n"
        "- Comparisons or alternatives: 'AvantStay vs Vrbo', 'Alternatives to Airbnb for Group Travel'.\n"
        "- Travel/destination guides or SEO content: 'Things to Do in Austin', 'Weekend Guide to Nashville', 'How to Invest in Vacation Rentals' (company name in body only).\n"
        "- Review or awards fluff: 'Guest Review Roundup', 'Best Vacation Homes 2024', 'Awards We Won'.\n"
        "- Generic thought-leadership or tips: '5 Tips for Property Owners', 'Why We Love Group Travel', 'What Makes a Great Stay' (no discrete business event).\n"
        "- Third-party articles where the company is not the subject: 'Sonder Files for Bankruptcy' (only briefly mentions another company), earnings roundups that list many tickers.\n"
        "If the headline does not clearly indicate the article is *about* the target company's own business (funding, launch, partnership, exit, exec change, strategy), treat as not about company.\n\n"
        "Output a JSON array with one object per line. Each object must have:\n"
        "- \"index\" (int, the index from the line),\n"
        "- \"is_about_company\" (bool),\n"
        "- \"topic\" (string, one of the topics list),\n"
        "- \"is_promo\" (bool), which MUST be true for promo_or_brand_marketing stories and other marketing/blog content even if they mention the company.\n"
        "Return only the JSON array."
    )
    user = (
        f"Target company: {competitor_name}.\n\n"
        "Classify these press items:\n" + "\n".join(lines)
    )

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            max_tokens=4000,
            temperature=0.1,
        )
        content = (resp.choices[0].message.content or "").strip()
        if not content:
            return _classify_press_headlines_fallback(competitor_name, items)
        data = _parse_json_response(content)
        if not isinstance(data, list):
            return _classify_press_headlines_fallback(competitor_name, items)
        by_index = {int(item["index"]): item for item in data if isinstance(item, dict) and "index" in item}
        result: List[dict] = []
        for i, it in enumerate(items):
            out = dict(it)
            meta = by_index.get(i) or {}
            out["is_about_company"] = bool(meta.get("is_about_company"))
            topic = (meta.get("topic") or "").strip()
            out["topic"] = topic if topic in PRESS_TOPICS else "other_business"
            out["is_promo"] = bool(meta.get("is_promo"))
            result.append(out)
        return result
    except Exception:
        return _classify_press_headlines_fallback(competitor_name, items)


def _classify_press_headlines_fallback(competitor_name: str, items: List[dict]) -> List[dict]:
    """
    Heuristic-only press classifier used when LLM is unavailable.
    """
    name_lower = (competitor_name or "").lower()
    fund_keywords = ("fundraise", "funding", "series ", "raises", "raise", "investment")
    partner_keywords = ("partnership", "partners with", "partners up", "alliance", "distribution deal")
    opening_keywords = ("opens", "opening", "debut", "debuts", "launches", "new hotel", "new property")
    restructure_keywords = ("layoff", "restructuring", "restructure", "cut", "cuts jobs", "bankruptcy")
    exec_keywords = ("ceo", "cfo", "coo", "cto", "cmo", "chief ", "founder", "co-founder", "president", "head of", "executive")
    interview_keywords = ("interview", "q&a", "q&a:", "fireside chat", "conversation with", "talks with", "speaks with")
    promo_keywords = ("why we are", "why we’re", "why we are best", "top 10", "guide to", "how to", "tips for")

    result: List[dict] = []
    for it in items:
        out = dict(it)
        title = (it.get("title") or "").lower()
        url = (it.get("url") or it.get("link") or "").lower()
        text = f"{title} {url}"

        is_about = name_lower in text
        topic = "other_business"
        is_promo = any(pk in text for pk in promo_keywords)

        if any(k in text for k in fund_keywords):
            topic = "fundraising"
        elif any(k in text for k in partner_keywords):
            topic = "new_partnership"
        elif any(k in text for k in opening_keywords):
            topic = "new_hotel_opening"
        elif any(k in text for k in restructure_keywords):
            topic = "restructuring_or_layoffs"
        elif any(ek in text for ek in exec_keywords) and any(ik in text for ik in interview_keywords):
            topic = "executive_interview"

        # Treat classic guide/SEO content as promo marketing when not otherwise classified.
        if is_promo and topic == "other_business":
            topic = "promo_or_brand_marketing"

        if not is_about and topic == "other_business":
            topic = "irrelevant"

        out["is_about_company"] = is_about
        out["topic"] = topic
        out["is_promo"] = is_promo
        result.append(out)
    return result


# Phrases that often indicate a JS/consent/ad wall instead of article content.
_ARTICLE_WALL_PHRASES = (
    "please enable js",
    "enable javascript",
    "disable any ad blocker",
    "ad blocker",
    "enable js and disable",
    "please enable js and",
)


def _fetch_article_html(url: str, timeout: int = 20) -> Optional[str]:
    """
    Fetch article HTML, trying to get past JS/ad-block walls.
    1) Request with a browser-like User-Agent (many walls only check UA).
    2) If we get 200 but extracted text is tiny and the page contains wall phrases,
       retry with Playwright (execute JS) when available.
    Returns the HTML that produced the most extractable text, or None on failure.
    """
    if not url or not url.strip().startswith("http"):
        return None
    html_best: Optional[str] = None
    text_len_best = 0

    # 1) Fetch with browser User-Agent to get past simple UA checks.
    try:
        fetched = fetch_url(
            url,
            timeout=timeout,
            headers={"User-Agent": USER_AGENT_BROWSER},
        )
        if fetched.status_code == 200 and fetched.text:
            text = _html_to_article_text(fetched.text, max_chars=100_000)
            if len(text) > text_len_best:
                text_len_best = len(text)
                html_best = fetched.text
            # If we got very little text and the raw HTML looks like a wall, try Playwright.
            if len(text) < 500:
                lower = fetched.text.lower()
                if any(phrase in lower for phrase in _ARTICLE_WALL_PHRASES):
                    try:
                        js_fetched = fetch_url_js(url)
                        if js_fetched.text:
                            js_text = _html_to_article_text(js_fetched.text, max_chars=100_000)
                            if len(js_text) > text_len_best:
                                html_best = js_fetched.text
                            # Don't overwrite if JS didn't help
                    except Exception:
                        pass
    except Exception:
        pass

    return html_best


def _html_to_article_text(html: str, max_chars: int = 6000) -> str:
    """
    Extract text for press article summarization. Prefer <article> or <main> so
    we send the release/body to the LLM instead of nav/chrome (many sites put
    article content inside article/main). Fall back to full body if needed.
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return html[:max_chars] + "\n[... truncated]" if len(html) > max_chars else html
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(["script", "style", "noscript"]):
        tag.decompose()
    # Prefer article or main so we don't waste space on header/nav (e.g. PR Newswire).
    root = soup.find("article") or soup.find("main")
    if root:
        text = root.get_text(separator="\n", strip=True)
    else:
        body = soup.find("body") or soup
        text = body.get_text(separator="\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n[... truncated]"
    return text


def _summarize_press_article_with_llm(
    competitor_name: str,
    title: str,
    outlet: str,
    date_str: Optional[str],
    url: str,
    html_text: Optional[str],
    topic: str,
) -> dict:
    """
    Given metadata + HTML for a single article, ask LLM for a 1-line summary.
    Returns dict with keys: summary (str) and maybe refined_topic (str).
    """
    client = _openai_client()
    if not client:
        return {
            "summary": title.strip() if title.strip() else "Business news item.",
            "refined_topic": topic,
        }

    # html_text may be empty or minimal if fetch failed (401, paywall, JS-only, etc.); we then infer from title/outlet.
    text_for_llm = ""
    if html_text:
        text_for_llm = _html_to_article_text(html_text, max_chars=6000)
        # If we got almost no real content (consent wall, paywall, "enable JS"), use title-only path.
        if len(text_for_llm.strip()) < 200:
            text_for_llm = ""

    system = (
        "You are summarizing business-relevant news about hospitality / real estate / travel companies.\n"
        "Write a single, factual, non-promotional sentence that captures the core business action "
        "(funding amount, partnership, opening, restructuring, etc.).\n"
        f"Topics you may assign are: {', '.join(PRESS_TOPICS)}.\n"
        "Focus on what happened, to whom, and why it matters strategically."
    )
    meta_lines = [
        f"Company: {competitor_name}",
        f"Title: {title}",
        f"Outlet: {outlet or 'unknown'}",
    ]
    if date_str:
        meta_lines.append(f"Date: {date_str}")
    meta_lines.append(f"URL: {url}")
    if topic:
        meta_lines.append(f"Headline_topic_hint: {topic}")
    meta = "\n".join(meta_lines)

    if text_for_llm:
        user = (
            f"{meta}\n\n"
            "Article text (truncated):\n"
            f"{text_for_llm}\n\n"
            "Return a JSON object with:\n"
            '- "summary": one-sentence summary (string),\n'
            '- "topic": one of the allowed topics (string).\n'
            "Return only the JSON object."
        )
    else:
        user = (
            f"{meta}\n\n"
            "No full article text is available; infer from the title and outlet.\n"
            "Return a JSON object with:\n"
            '- "summary": one-sentence summary (string),\n'
            '- "topic": one of the allowed topics (string).\n'
            "Return only the JSON object."
        )

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            max_tokens=400,
            temperature=0.1,
        )
        content = (resp.choices[0].message.content or "").strip()
        data = _parse_json_response(content)
        if not isinstance(data, dict):
            raise ValueError("Expected JSON object")
        summary = (data.get("summary") or "").strip()
        refined_topic = (data.get("topic") or "").strip()
        if not summary:
            summary = title.strip() or "Business news item."
        if refined_topic not in PRESS_TOPICS:
            refined_topic = topic
        return {"summary": summary, "refined_topic": refined_topic}
    except Exception:
        return {
            "summary": title.strip() if title.strip() else "Business news item.",
            "refined_topic": topic,
        }


def enrich_press_items_with_llm(
    competitor_name: str,
    items: List[dict],
    max_articles_to_summarize: int = 40,
) -> List[dict]:
    """
    End-to-end press pipeline for a single competitor.

    Steps:
    - All articles are read/classified by LLM to remove nonsense and promo.
    - PR Newswire items are automatically included (press releases; high priority).
    - Company-sourced (press_endpoint) items are reviewed carefully—may be promo, so
      we require is_about_company and drop promo/irrelevant.
    - Heuristically deduplicate by normalized title; when the same article appears
      from multiple sources, choose primary by priority: press_endpoint > prnewswire
      > tier-1 outlets (Bloomberg, Reuters, etc.) > tier-2 (CNBC, etc.) > rest.
    - For up to `max_articles_to_summarize` canonical articles, fetch HTML and ask
      LLM for a 1-line summary and (optionally refined) topic.

    Returns a list of canonical press dicts with keys:
    - title, url, date, outlet, topic, summary, secondary_urls (list[str])
    """
    if not items:
        return []

    classified = _classify_press_headlines_with_llm(competitor_name, items)

    # Lightweight heuristics on top of LLM labels to aggressively drop
    # own-site marketing/blog noise (guides, itineraries, owner education, etc.)
    # for the target company. This is especially important for AvantStay-style
    # content where the blog produces many SEO/how-to pieces that mention the
    # company but are not discrete business events.
    import re as _re  # local alias to avoid confusion with top-level imports

    name_slug = _re.sub(r"[^a-z0-9]", "", (competitor_name or "").lower())

    def _looks_like_own_marketing_page(url: str) -> bool:
        u = (url or "").lower()
        if not u or not name_slug:
            return False
        if name_slug not in u:
            return False
        # Common marketing/blog path fragments for hospitality competitors.
        marketing_fragments = (
            "/blog",
            "/blogs/",
            "/destinations",
            "/destination/",
            "/itinerary",
            "/itineraries",
            "/guide",
            "/guides",
            "/owner",
            "/owners",
            "/invest",
            "/awards",
        )
        return any(fragment in u for fragment in marketing_fragments)

    def _looks_like_guide_title(title: str) -> bool:
        t = (title or "").lower()
        if not t:
            return False
        guide_keywords = (
            "guide",
            "checklist",
            "how to ",
            "how-to ",
            "itinerary",
            "roundup",
            "review roundup",
            "what ",
            "need to know",
            "roi ",
            "valuation",
            "worth?",
            "alternatives",
            "vs ",
            "best ",
        )
        return any(kw in t for kw in guide_keywords)

    for it in classified:
        url = (it.get("url") or it.get("link") or "").strip()
        title = (it.get("title") or "").strip()
        topic = (it.get("topic") or "").strip()
        is_promo = bool(it.get("is_promo"))

        if _looks_like_own_marketing_page(url) and _looks_like_guide_title(title):
            # Force marketing classification so downstream filters drop it.
            it["is_promo"] = True
            it["topic"] = "promo_or_brand_marketing"
            # Treat as not primarily about the company as a business.
            it["is_about_company"] = False

    # Filter to business-relevant items. Priority rules:
    # - PR Newswire: always include (press releases about the company; high priority).
    # - User-provided company news (press_endpoint): LLM review only—may be promo, so we
    #   require is_about_company and drop promo/irrelevant.
    # - All other sources: require is_about_company and drop promo/irrelevant.
    filtered: List[dict] = []
    for it in classified:
        provider = (it.get("provider") or "").strip().lower()
        if provider == "prnewswire":
            filtered.append(it)
            continue
        topic = (it.get("topic") or "").lower()
        if topic in {"irrelevant"}:
            continue
        if it.get("is_promo") and topic in {"promo_or_brand_marketing"}:
            continue
        if not it.get("is_about_company"):
            continue
        filtered.append(it)

    if not filtered:
        return []

    # Heuristic dedupe by normalized title
    import re

    def _norm_title(title: str) -> str:
        t = (title or "").lower()
        t = re.sub(r"[^a-z0-9]+", " ", t)
        return t.strip()

    clusters: dict[str, List[dict]] = {}
    for it in filtered:
        key = _norm_title(it.get("title") or "")
        if not key:
            key = (it.get("url") or it.get("link") or "").lower()
        clusters.setdefault(key, []).append(it)

    # Source priority for picking primary when the same article appears from multiple sources.
    # Order: user company links (press_endpoint) > PR Newswire > tier-1 outlets > tier-2 > rest.
    provider_rank = {
        "press_endpoint": 6,
        "prnewswire": 5,
    }
    outlet_rank = {
        "prnewswire.com": 4,
        "www.prnewswire.com": 4,
        "bloomberg.com": 3,
        "reuters.com": 3,
        "wsj.com": 3,
        "ft.com": 3,
        "cnbc.com": 2,
        "techcrunch.com": 2,
        "crunchbase.com": 2,
        "axios.com": 2,
    }

    def _source_score(provider: str, outlet: str, url: str) -> int:
        p = (provider or "").strip().lower()
        if p in provider_rank:
            return provider_rank[p]
        domain = (outlet or "").lower()
        if domain in outlet_rank:
            return outlet_rank[domain]
        try:
            import urllib.parse
            parsed = urllib.parse.urlparse(url)
            dom = (parsed.netloc or "").lower()
            return outlet_rank.get(dom, 1)
        except Exception:
            return 1

    canonical: List[dict] = []
    for _, group in clusters.items():
        # Choose best primary by source priority (no duplicates across sources; pick one).
        best = None
        best_score = -1
        secondary_urls: List[str] = []
        for it in group:
            url = (it.get("url") or it.get("link") or "").strip()
            outlet = _press_outlet_from_item(it)
            provider = (it.get("provider") or "").strip()
            score = _source_score(provider, outlet, url)
            if best is None or score > best_score:
                if best is not None:
                    prev_url = (best.get("url") or best.get("link") or "").strip()
                    if prev_url:
                        secondary_urls.append(prev_url)
                best = it
                best_score = score
            else:
                if url:
                    secondary_urls.append(url)
        if not best:
            continue
        title = (best.get("title") or "").strip() or "Press item"
        url = (best.get("url") or best.get("link") or "").strip()
        outlet = _press_outlet_from_item(best)
        date = (best.get("date") or "").strip() or None
        topic = (best.get("topic") or "").strip() or "other_business"

        canonical.append(
            {
                "title": title,
                "url": url or None,
                "outlet": outlet or None,
                "date": date,
                "topic": topic,
                "secondary_urls": list({u for u in secondary_urls if u}),
                "summary": None,  # to be filled in below
            }
        )

    # Summaries: per-article calls, capped for cost.
    summarized: List[dict] = []
    for i, item in enumerate(canonical):
        if i >= max_articles_to_summarize:
            # Fallback summary: use title.
            out = dict(item)
            out["summary"] = item["title"]
            summarized.append(out)
            continue

        url = item.get("url") or ""
        html_text = _fetch_article_html(url) if url else None

        meta = _summarize_press_article_with_llm(
            competitor_name=competitor_name,
            title=item["title"],
            outlet=item.get("outlet") or "",
            date_str=item.get("date"),
            url=url,
            html_text=html_text,
            topic=item.get("topic") or "other_business",
        )
        out = dict(item)
        out["summary"] = meta.get("summary") or item["title"]
        refined_topic = meta.get("refined_topic") or out.get("topic")
        if refined_topic in PRESS_TOPICS:
            out["topic"] = refined_topic
        summarized.append(out)

    return summarized
