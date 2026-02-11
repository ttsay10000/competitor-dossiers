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
from urllib.parse import urlparse

from .collectors.http import fetch_url, fetch_url_js, USER_AGENT_BROWSER
from .diff.asset_diff import (
    NON_LOCATION_PATH_SEGMENTS,
    _parse_avantstay_style_path,
    resolve_destination_slug_to_state,
    infer_location_for_property,
)

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


def _normalize_functional_area(llm_value: str) -> str:
    """Map LLM response to canonical functional area (case-insensitive, handles variants)."""
    if not llm_value or not isinstance(llm_value, str):
        return "Other"
    v = llm_value.strip()
    v_lower = v.lower()
    for canonical in TALENT_FUNCTIONAL_AREAS:
        if canonical.lower() == v_lower:
            return canonical
    # Common variants the model might return
    if v_lower in ("business & strategy", "business and strategy"):
        return "Business & Strategy"
    if v_lower in ("property operations", "property ops"):
        return "Property operations"
    if v_lower in ("sales / growth", "sales", "growth"):
        return "Sales / Growth"
    if v_lower in ("ai / data", "ai", "data"):
        return "AI / Data"
    return "Other"


def _openai_client():
    """Return OpenAI client from app config (single OPENAI_API_KEY for all LLM calls)."""
    from .config import get_openai_client
    return get_openai_client()


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


def _assign_state_from_url(prop: dict) -> dict:
    """
    If property has an Avantstay-style URL (/{id}/{destination}/{slug}), resolve destination
    to state and set state + market. Returns a copy with state/market set when resolved.
    """
    out = dict(prop)
    if (out.get("state") or "").strip():
        return out
    url = (out.get("url") or "").strip()
    if not url:
        return out
    path = urlparse(url.split("?")[0]).path or ""
    dest_slug = _parse_avantstay_style_path(path)
    if not dest_slug or dest_slug.lower() in NON_LOCATION_PATH_SEGMENTS:
        return out
    state = resolve_destination_slug_to_state(dest_slug)
    if state:
        out["state"] = state
        place_name = dest_slug.replace("-", " ").title()
        out["market"] = f"{place_name}, {state}"
    return out


def enrich_properties_with_llm(
    properties: List[dict],
    raw_content: Optional[str] = None,
) -> List[dict]:
    """
    Use LLM to assign state (and optionally city) to each property. On failure or no API key, return list unchanged.
    When raw_content is provided, the LLM reads the page text and assigns state/city from page context; otherwise
    it infers from url/name/market only.
    First assigns state from URL when possible (Avantstay-style /{id}/{destination}/{slug} -> lookup destination to state).
    """
    if not properties:
        return properties

    # Assign state (and market) from URL for Avantstay-style URLs so we tag locations without LLM when possible
    working = [_assign_state_from_url(p) for p in properties]

    client = _openai_client()
    if not client:
        return working

    # Keep batch small for speed; skip page text when property count is large to avoid huge context.
    _ENRICH_BATCH_SIZE = 50
    _ENRICH_SKIP_RAW_CONTENT_ABOVE = 250
    use_raw = bool(raw_content and raw_content.strip() and len(working) <= _ENRICH_SKIP_RAW_CONTENT_ABOVE)

    batch = working[:_ENRICH_BATCH_SIZE]
    lines = []
    for i, p in enumerate(batch):
        url = (p.get("url") or "").strip()
        name = (p.get("name") or "").strip()
        market = (p.get("market") or "").strip()
        lines.append(f"{i}: url={url!r} name={name!r} market={market!r}")

    if use_raw:
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
            return working
        data = _parse_json_response(content)
        if not isinstance(data, list):
            return working
        by_index = {int(item["index"]): item for item in data if isinstance(item, dict) and "index" in item}
        result = []
        for i, p in enumerate(working):
            out = dict(p)
            if i in by_index:
                # Only fill state/city when missing (don't overwrite URL-derived or collector-set values with "Other")
                enricher_state = (by_index[i].get("state") or "Other").strip() or "Other"
                enricher_city = (by_index[i].get("city") or "").strip() or None
                if not (out.get("state") or "").strip():
                    out["state"] = enricher_state
                if enricher_city and not (out.get("city") or "").strip():
                    out["city"] = enricher_city
            result.append(out)
        return result
    except Exception:
        return working


# Max properties to send in one LLM call for bucket-by-state (single call is fast; above this fall back to batches).
_DOSSIER_STATE_SINGLE_CALL_MAX = 2000
# Batch size for dossier-time state assignment when we fall back to batched (e.g. > single-call max).
_DOSSIER_STATE_BATCH_SIZE = 80


def _bucket_properties_by_state_single_call(
    competitor_name: str,
    properties: List[dict],
    valid_states: set,
) -> Optional[List[dict]]:
    """
    One LLM call: send full list of property bullets (index, name, url, market, url_derived, details),
    get back state per index. Bucket and assign so dossier can summarize by state quickly.
    Returns list of property dicts with state/city set, or None on failure.
    """
    if not properties:
        return None
    client = _openai_client()
    if not client:
        return None

    lines = []
    for i, p in enumerate(properties):
        url = (p.get("url") or "").strip()[:200]
        name = (p.get("name") or "").strip()[:150]
        market = (p.get("market") or "").strip()[:100]
        url_derived = infer_location_for_property(p)
        details = (p.get("details") or "").strip()[:80]
        lines.append(
            f"{i}: name={name!r} url={url!r} market={market!r} url_derived_location={url_derived!r} details={details!r}"
        )

    system = """You are a data enricher for US real estate/hospitality property lists. Your job is to bucket properties by state.
Given the full list of properties below (each line: index, name, url, market, url_derived_location, details), assign a US state to each.
- Use full US state names only (e.g. "California", "Texas", "Florida"). No city in the state field.
- url_derived_location may be a state name, a destination/city name (e.g. "Newport Beach", "Coachella Valley"), or "Unspecified"—use it to infer the correct state when possible.
- For non-US, career site, privacy, or unclear, use state "Other".
Return a JSON array with one object per property in the same order: {"index": 0, "state": "California"} (city optional). Return only the JSON array, no markdown."""

    user = f"Competitor: {competitor_name}\n\nProperties (assign state from name, url, market, url_derived_location; bucket by state):\n" + "\n".join(lines)

    try:
        # One call for full list; allow enough tokens for state per property (e.g. 25 chars * N).
        max_tokens = min(16000, 30 * len(properties) + 500)
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            max_tokens=max_tokens,
            temperature=0.1,
        )
        content = (resp.choices[0].message.content or "").strip()
        if not content:
            return None
        data = _parse_json_response(content)
        if not isinstance(data, list):
            return None
        by_index = {int(item["index"]): item for item in data if isinstance(item, dict) and "index" in item}
        result = []
        for i, p in enumerate(properties):
            out = dict(p)
            if i in by_index:
                # Don't overwrite URL-derived state (e.g. Avantstay) with "Other"
                existing_state = (out.get("state") or "").strip()
                state = (by_index[i].get("state") or "").strip() or "Other"
                state = state if state in valid_states else "Other"
                if not existing_state or existing_state == "Other":
                    out["state"] = state
                city = (by_index[i].get("city") or "").strip()
                if city and not (out.get("city") or "").strip():
                    out["city"] = city
            result.append(out)
        return result
    except Exception:
        return None


def assign_states_to_properties_for_dossier(
    competitor_name: str,
    properties: List[dict],
) -> Optional[List[dict]]:
    """
    Assign state (and optionally city) to each property so dossier can bucket by state for location bullets.
    Uses a single LLM call with the full list of property bullets when possible (fast); falls back to
    batched calls only if the list exceeds _DOSSIER_STATE_SINGLE_CALL_MAX.
    Returns a new list of property dicts (copies) with state/city set, or None if no API key / empty input.
    """
    if not properties:
        return None
    client = _openai_client()
    if not client:
        return None

    from .diff.asset_diff import US_STATE_ABBREV
    valid_states = set(US_STATE_ABBREV.values()) | {"Washington DC", "Other"}

    # Pre-fill state from URL where possible (Avantstay-style /{id}/{destination}/{slug} -> state).
    properties = [_assign_state_from_url(p) for p in properties]

    # Prefer one fast call with full list (bucket and summarize by state in one go).
    if len(properties) <= _DOSSIER_STATE_SINGLE_CALL_MAX:
        result = _bucket_properties_by_state_single_call(competitor_name, properties, valid_states)
        if result is not None:
            return result
        # Fall through to batched if single call failed (e.g. token limit, API error)

    # Fallback: batched per-property assignment (e.g. list too large or single call failed).
    result = []
    for start in range(0, len(properties), _DOSSIER_STATE_BATCH_SIZE):
        batch = properties[start : start + _DOSSIER_STATE_BATCH_SIZE]
        lines = []
        for i, p in enumerate(batch):
            url = (p.get("url") or "").strip()[:200]
            name = (p.get("name") or "").strip()[:150]
            market = (p.get("market") or "").strip()[:100]
            url_derived = infer_location_for_property(p)
            details = (p.get("details") or "").strip()[:80]
            lines.append(
                f"{i}: name={name!r} url={url!r} market={market!r} url_derived_location={url_derived!r} details={details!r}"
            )

        system = """You are assigning a US state to each property for a real estate/hospitality competitor dashboard.
Given property name, url, market, and url_derived_location (hint from URL parsing), assign the correct state for each.
- Use full US state names only (e.g. "California", "Texas", "Florida"). No city in the state field.
- url_derived_location may be a state name, a destination/city name (e.g. "Newport Beach", "Coachella Valley"), or "Unspecified"—use it to infer the correct state when possible.
- For non-US, career site, privacy, or unclear, use state "Other".
Output a JSON array with one object per line item: {"index": 0, "state": "California", "city": "optional city or omit"}.
Return only the JSON array, no markdown."""

        user = f"Competitor: {competitor_name}\n\nProperties (assign state from name, url, market, and url_derived_location):\n" + "\n".join(lines)

        try:
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                max_tokens=4000,
                temperature=0.1,
            )
            content = (resp.choices[0].message.content or "").strip()
            if not content:
                for p in batch:
                    result.append(dict(p))
                continue
            data = _parse_json_response(content)
            if not isinstance(data, list):
                for p in batch:
                    result.append(dict(p))
                continue
            by_index = {int(item["index"]): item for item in data if isinstance(item, dict) and "index" in item}
            for i, p in enumerate(batch):
                out = dict(p)
                if i in by_index:
                    existing_state = (out.get("state") or "").strip()
                    state = (by_index[i].get("state") or "").strip() or "Other"
                    state = state if state in valid_states else "Other"
                    if not existing_state or existing_state == "Other":
                        out["state"] = state
                    city = (by_index[i].get("city") or "").strip()
                    if city and not (out.get("city") or "").strip():
                        out["city"] = city
                result.append(out)
        except Exception:
            for p in batch:
                result.append(dict(p))

    return result if result else None


_MAX_OTHER_PROPERTIES_FOR_RESEARCH = 100


def research_and_assign_states_for_other_properties(
    competitor_name: str,
    properties: List[dict],
) -> Optional[List[dict]]:
    """
    For properties with state="Other", use LLM knowledge to infer location from property names.
    E.g. "Placemakr Dupont Circle" -> Washington DC (Dupont Circle is a DC neighborhood).
    Only runs when there are < 100 Other properties to avoid hanging. Returns updated list or None.
    """
    other_indices = [
        i for i, p in enumerate(properties)
        if ((p.get("state") or "").strip() or "Other") == "Other"
    ]
    if len(other_indices) >= _MAX_OTHER_PROPERTIES_FOR_RESEARCH or not other_indices:
        return None
    client = _openai_client()
    if not client:
        return None

    from .diff.asset_diff import US_STATE_ABBREV
    valid_states = set(US_STATE_ABBREV.values()) | {"Washington DC", "Other"}

    lines = []
    for local_i, orig_i in enumerate(other_indices):
        p = properties[orig_i]
        name = (p.get("name") or "").strip()[:150]
        url = (p.get("url") or "").strip()[:200]
        market = (p.get("market") or "").strip()[:100]
        details = (p.get("details") or "").strip()[:150]
        lines.append(
            f"{local_i} (orig={orig_i}): name={name!r} url={url!r} market={market!r} details={details!r}"
        )

    system = """You are a location researcher for US real estate/hospitality properties. Some properties are tagged "Other" because their location could not be determined from URL or metadata. Your task is to infer the correct US state (or Washington DC) from the property name and any available context.

Use your knowledge of US geography: neighborhood names (e.g. Dupont Circle -> Washington DC, Brooklyn -> New York), city names, landmarks, regions. Property names often include the neighborhood or city (e.g. "Placemakr Dupont Circle" is in Washington DC).

Rules:
- Use full US state names only (e.g. "California", "New York", "Texas"). For Washington DC use "Washington DC".
- If you can confidently infer the state from the name or context, assign it. Otherwise keep "Other".
- Return a JSON array with one object per property: {"index": <local_i>, "state": "StateName", "city": "optional city or omit"}.
- Only include entries where you inferred a state different from Other.
Return only the JSON array, no markdown."""

    user = f"Competitor: {competitor_name}\n\nProperties with unknown location (index=local index, orig=original list index). Infer state from property names when possible:\n" + "\n".join(lines)

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            max_tokens=4000,
            temperature=0.1,
        )
        content = (resp.choices[0].message.content or "").strip()
        if not content:
            return None
        data = _parse_json_response(content)
        if not isinstance(data, list):
            return None
        # Build orig_index -> {state, city} from LLM response
        updates: dict[int, dict] = {}
        for item in data:
            if not isinstance(item, dict) or "index" not in item:
                continue
            local_i = int(item["index"])
            if local_i < 0 or local_i >= len(other_indices):
                continue
            orig_i = other_indices[local_i]
            state = (item.get("state") or "").strip() or "Other"
            if state not in valid_states or state == "Other":
                continue
            city = (item.get("city") or "").strip() or None
            updates[orig_i] = {"state": state, "city": city}
        if not updates:
            return None
        # Apply updates to a copy of properties
        result = []
        for i, p in enumerate(properties):
            out = dict(p)
            if i in updates:
                out["state"] = updates[i]["state"]
                if updates[i].get("city"):
                    out["city"] = updates[i]["city"]
            result.append(out)
        return result
    except Exception:
        return None


def enrich_jobs_with_llm(jobs: List[dict]) -> List[dict]:
    """
    Use LLM to assign functional_area and is_senior to each job. On failure or no API key, return list unchanged.
    LLM output is normalized (case-insensitive match to canonical areas). When LLM returns "Other", we fall back
    to rule-based job_functional_area so keyword-based categorization (e.g. Property operations) still applies.
    """
    if not jobs:
        return jobs
    client = _openai_client()
    if not client:
        return jobs

    from .rules.talent_rules import job_functional_area as rule_based_functional_area

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

Functional area rules:
- Use "Property operations" for on-property, guest-facing or property-level roles: front desk, housekeeping, maintenance, F&B (cook, server, bartender), concierge, guest experience, night auditor, room attendant, valet, bellman, property management, field ops, hotel/restaurant operations. When in doubt and the title suggests on-site hospitality or property-level execution, choose Property operations.
- Use "Business & Strategy", "Sales / Growth", "Marketing", "AI / Data", "Product", "Engineering" for corporate/central roles (strategy, growth, product, engineering, data, marketing, sales, HR, finance, etc.).
- Use "Other" only when the role clearly does not fit any of the above.

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
                fa_raw = (by_index[i].get("functional_area") or "Other").strip()
                fa = _normalize_functional_area(fa_raw)
                # When LLM said Other, try rule-based so keyword-matched roles (e.g. Property operations) are used
                if fa == "Other":
                    rule_fa = rule_based_functional_area(j)
                    if rule_fa != "Other":
                        fa = rule_fa
                out["functional_area"] = fa
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

# Classification: fetch article body so LLM can assign topic from content (body-only, no header/ads).
_CLASSIFY_BODY_MAX_FETCHES = 50
_CLASSIFY_BODY_MAX_CHARS = 2800
_CLASSIFY_BODY_FETCH_TIMEOUT = 12


def _press_outlet_from_item(item: dict) -> str:
    # Prefer explicit outlet (e.g. from Google News RSS entry.source.title) so LLM can distinguish outlets for dedupe.
    outlet = (item.get("outlet") or item.get("source") or "").strip()
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
    Classify press items. When available, fetches article body (main text only, no header/ads/nav)
    and asks the LLM to assign topic and relevance from the body; otherwise uses headline/metadata only.

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
    # Fetch article body (body-only text from <article>/<main> paragraphs) for classification.
    bodies: List[Optional[str]] = [None] * len(batch)
    fetches_done = 0
    for i, it in enumerate(batch):
        if fetches_done >= _CLASSIFY_BODY_MAX_FETCHES:
            break
        url = (it.get("url") or it.get("link") or "").strip()
        if not url or not url.startswith("http"):
            continue
        try:
            html = _fetch_article_html(url, timeout=_CLASSIFY_BODY_FETCH_TIMEOUT)
            if html:
                # Body-only: paragraphs inside article/main, no header/ads/nav.
                text = _html_to_article_text(html, max_chars=_CLASSIFY_BODY_MAX_CHARS)
                if len(text.strip()) > 80:
                    bodies[i] = text.strip()[: _CLASSIFY_BODY_MAX_CHARS]
                    fetches_done += 1
        except Exception:
            pass

    lines = []
    for i, it in enumerate(batch):
        title = (it.get("title") or "").strip()
        url = (it.get("url") or it.get("link") or "").strip()
        outlet = _press_outlet_from_item(it)
        lines.append(f"{i}: title={title!r} outlet={outlet!r} url={url!r}")
        if bodies[i]:
            body = (bodies[i] or "").replace("\n", " ").strip()[: _CLASSIFY_BODY_MAX_CHARS]
            lines.append(f"  body: {body}")
        elif it.get("snippet") or it.get("feed_snippet"):
            snip = (it.get("snippet") or it.get("feed_snippet") or "").replace("\n", " ").strip()[: _CLASSIFY_BODY_MAX_CHARS]
            if snip:
                lines.append(f"  snippet: {snip}")

    topics_str = ", ".join(PRESS_TOPICS)
    system = (
        "You are classifying news articles about hospitality / real estate / travel companies.\n"
        f"Possible topics are: {topics_str}.\n\n"
        "When a 'body' line is provided for an item, the body is the ARTICLE MAIN TEXT ONLY (no site header, ads, nav, or metadata). "
        "Classify based on the BODY content: assign topic and is_about_company from what the article is actually about. "
        "Headlines can be misleading; ignore header/metadata and use the body to decide.\n"
        "When no body is provided, classify from the title, outlet, and URL only.\n\n"
        "For each item decide if it is primarily about the target company AS A BUSINESS (its funding, strategy, "
        "corporate actions, or portfolio), and whether it is a business-focused story.\n"
        "Business-focused examples: fundraising rounds, new property or market openings, new partnerships or distribution deals, "
        "restructuring/layoffs, significant market expansion or exits, major product or strategy shifts, "
        "executive appointments and senior hires (e.g. 'Company appoints new EVP', 'X names new CFO') — set is_about_company=true, topic=other_business. "
        "or in-depth interviews / Q&A with senior executives (topic 'executive_interview').\n"
        "VERY IMPORTANT: Articles that are guides, checklists, ROI explainers, destination or travel guides, "
        "homeowner education, generic investment advice, thought-leadership, comparisons like 'X vs Y', "
        "or awards/roundups should be topic 'promo_or_brand_marketing' and is_promo=true, is_about_company=false "
        "unless the body is principally about a discrete corporate action by the company.\n"
        "When the URL looks like the company's own marketing site or blog (/blog, /destinations, /guides, /owners, /awards, /itinerary), "
        "only treat as business-focused if the BODY clearly describes a concrete business event (raises, acquires, launches, partners).\n\n"
        "Examples of NONSENSE (is_about_company=false; promo_or_brand_marketing/is_promo where applicable):\n"
        "- Listicles/roundups where the company is one of many; industry trend pieces that mention the company in passing;\n"
        "- Comparisons or alternatives; travel/destination guides or SEO content; review or awards fluff;\n"
        "- Generic thought-leadership or tips; third-party articles where the company is not the subject.\n"
        "PARTNERSHIP RULE: When the body describes a partnership, deal, or launch that NAMES the target company, set is_about_company=true, topic=new_partnership.\n"
        "EXECUTIVE HIRE RULE: When the body is about the company appointing or hiring an executive, set is_about_company=true, topic=other_business.\n\n"
        "WRONG ENTITY / NOT HOSPITALITY: If the body is about a different entity (same name: person, street, other industry), set topic=irrelevant, is_about_company=false. "
        "For 'Lark' only the hotel company (Lark Hotels, Lark Hospitality); exclude persons/places named Lark.\n\n"
        "Output a JSON array with one object per line. Each object must have:\n"
        "- \"index\" (int, the index from the line),\n"
        "- \"is_about_company\" (bool),\n"
        "- \"topic\" (string, one of the topics list),\n"
        "- \"is_promo\" (bool), true for promo_or_brand_marketing and other marketing/blog content.\n"
        "Return only the JSON array."
    )
    # When competitor is Lark, clarify we mean the hotel company only (not persons/places named Lark).
    company_note = f"Target company: {competitor_name}."
    if (competitor_name or "").strip().lower() == "lark":
        company_note = "Target company: Lark (the hotel/hospitality company—Lark Hotels, Lark Hospitality. Mark as irrelevant any article about a person or place named Lark, e.g. Lark Davis, Tessa Lark, Lark Street.)"
    user = (
        f"{company_note}\n\n"
        "Items (index: title | outlet | url; when present, 'body:' is the article main text only—use it to assign topic and relevance).\n\n"
        + "\n".join(lines)
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
    appointment_keywords = ("appoints", "appointed", "names", "named", "evp", "executive vice president", "joins as", "hired as", "new cfo", "new ceo", "new coo", "new evp")
    interview_keywords = ("interview", "q&a", "q&a:", "fireside chat", "conversation with", "talks with", "speaks with")
    promo_keywords = ("why we are", "why we’re", "why we are best", "top 10", "guide to", "how to", "tips for")

    # URL patterns that suggest listicle/comparison, not a story about the company.
    url_noise = ("vs-", "versus-", "alternatives", "comparison", "vs.", "alternatives-to-")
    # Phrases that indicate wrong entity (same name, different person/place/industry)—mark irrelevant.
    wrong_entity_phrases = (
        "crypto expert", "lark davis", "lark street", "tessa lark", "violinist", "violin ",
        "street bid", "street corridor", "bluegrass", "mendelssohn", "crypto bottom",
    )
    result: List[dict] = []
    for it in items:
        out = dict(it)
        title = (it.get("title") or "").lower()
        url = (it.get("url") or it.get("link") or "").lower()
        text = f"{title} {url}"

        is_about = name_lower in title or name_lower in url
        if is_about and any(n in url for n in url_noise):
            is_about = False  # e.g. "X vs Avantstay" / "alternatives to Avantstay" = not about company
        if is_about and any(phrase in text for phrase in wrong_entity_phrases):
            is_about = False
            topic = "irrelevant"
            out["is_about_company"] = False
            out["topic"] = topic
            out["is_promo"] = False
            result.append(out)
            continue
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
        elif any(ak in text for ak in appointment_keywords) and is_about:
            topic = "other_business"  # executive appointment / senior hire
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


def _assign_press_story_keys(competitor_name: str, items: List[dict]) -> List[str]:
    """
    Assign a short story key to each item so that items about the SAME news event
    get the SAME key. Used to dedupe multiple articles (different headlines/outlets/dates)
    covering the same story.

    "Same story" = same underlying event: same deal, same announcement, same expansion, etc.
    We pass headline + outlet + publication date so the model can use general thrust and
    time window (e.g. articles within days/weeks about the same announcement → same key).

    Returns list of strings, one per item. On failure or missing API, returns empty list.
    """
    if not items or len(items) > 150:
        return []
    client = _openai_client()
    if not client:
        return []

    def _date_short(it: dict) -> str:
        raw = it.get("date")
        if raw is None:
            return "no date"
        if hasattr(raw, "strftime"):
            return raw.strftime("%Y-%m-%d")
        s = (raw if isinstance(raw, str) else str(raw)).strip()
        if not s:
            return "no date"
        # Keep only first 10 chars for ISO date
        if len(s) >= 10 and s[4] == "-" and s[7] == "-":
            return s[:10]
        return s[:16] if len(s) > 16 else s

    lines = []
    for i, it in enumerate(items):
        title = (it.get("title") or "").strip()
        outlet = _press_outlet_from_item(it)
        date_str = _date_short(it)
        lines.append(f"{i}: {title!r} | {outlet} | {date_str}")

    system = (
        "You are grouping news items about a single company. Items that describe the SAME news event MUST get the EXACT SAME story key.\n\n"
        "Same story = same underlying event (same deal, same announcement, same expansion). "
        "Headline wording and exact publication date often differ across outlets; use the GENERAL THRUST and ROUGH TIME WINDOW. "
        "Articles published within a few days or weeks that cover the same announcement or event should get the SAME key.\n\n"
        "Assign a short story key (2–6 words, lowercase, a-z and spaces only) for each line. "
        "Use a canonical slug: main entities + core topic.\n\n"
        "Examples (all in same key):\n"
        "- Hilton–Placemakr: 'Hilton partners with Placemakr on apartments', 'Hilton to Launch Apartment Collection Brand in 2026 With Placemakr', "
        "'Hilton debuts Apartment Collection', 'How Hilton and Placemakr are redefining hotel accommodations' (different dates OK) → 'hilton placemakr apartment collection'.\n"
        "- Lark four openings: 'Four Lark Hotels set to open in spring 2026' (Jan 7), 'Lark Hotels to Open Four New Properties in 2026' (Jan 5), "
        "'Lark Hotels Adding Four New Spots In 2026' (Jan 13), 'Four New Lark Hotels Set To Boost Hospitality In Massachusetts' (Jan 10) → same key, e.g. 'lark four properties 2026'.\n"
        "- Same executive hire across outlets: 'Lark appoints new EVP of commercial strategy' (Hotel Management), 'Lark Appoints Lynsey Kreitzer as Executive Vice President of Commercial Strategy' (Boston Real Estate Times) → same key, e.g. 'lark evp commercial strategy'.\n"
        "- Roundups that lead with this story get the same key as the main story.\n\n"
        "Use enough specificity so DIFFERENT events get different keys: do not merge unrelated stories that only share the company name (e.g. 'Lark appoints EVP' must NOT get the same key as 'Lark Street BID' or 'Crypto expert Lark Davis'). Same key only for the SAME underlying event.\n\n"
        "Return a JSON array of strings, one per line, in order (index 0 = first line). Use only a-z and spaces."
    )
    user = f"Target company: {competitor_name}.\n\nEach line is: index: title | outlet | publication date\n\n" + "\n".join(lines)

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            max_tokens=4000,
            temperature=0.1,
        )
        content = (resp.choices[0].message.content or "").strip()
        if not content:
            return []
        data = _parse_json_response(content)
        if not isinstance(data, list) or len(data) != len(items):
            return []
        keys = []
        for i, raw in enumerate(data):
            k = (raw if isinstance(raw, str) else str(raw)).strip().lower()
            k = re.sub(r"[^a-z0-9\s]+", "", k)
            k = re.sub(r"\s+", " ", k).strip()
            keys.append(k or f"_item_{i}")
        return keys
    except Exception:
        return []


# First dedupe (body-aware): fetch article bodies so LLM can drop irrelevant and merge by content.
_FIRST_DEDUPE_MAX_FETCHES = 40
_FIRST_DEDUPE_SNIPPET_CHARS = 1200
_FIRST_DEDUPE_FETCH_TIMEOUT = 12

# Final dedupe: cap on article fetches and snippet length so we don't slow down or overflow context.
_DEDUPE_MAX_SNIPPET_FETCHES = 25
_DEDUPE_SNIPPET_CHARS = 450
_DEDUPE_FETCH_TIMEOUT = 10


def _first_dedupe_press_with_body_llm(competitor_name: str, items: List[dict]) -> List[dict]:
    """
    First dedupe pass: fetch article body text and ask LLM to (1) drop items that have nothing
    to do with hospitality, hotels, real estate, fundraising, or company/business, and (2) merge
    items that are the same article content or same story (syndicated / different outlets).
    Returns a subset of items (indices to keep). Uses body text so we catch relevance and
    content-based duplicates before story-key + Jaccard. On failure or no API, returns items unchanged.
    """
    import sys
    if not items or len(items) > 120:
        return items
    client = _openai_client()
    if not client:
        return items

    def _date_str(it: dict) -> str:
        raw = it.get("date")
        if raw is None:
            return "no date"
        if hasattr(raw, "strftime"):
            return raw.strftime("%Y-%m-%d")
        s = (raw if isinstance(raw, str) else str(raw)).strip()
        return s[:10] if len(s) >= 10 else (s or "no date")

    # Fetch article body snippets (capped) so LLM can judge relevance and same content.
    snippets: List[Optional[str]] = [None] * len(items)
    fetches_done = 0
    for i, it in enumerate(items):
        if fetches_done >= _FIRST_DEDUPE_MAX_FETCHES:
            break
        url = (it.get("url") or it.get("link") or "").strip()
        if not url or not url.startswith("http"):
            continue
        try:
            html = _fetch_article_html(url, timeout=_FIRST_DEDUPE_FETCH_TIMEOUT)
            if html:
                text = _html_to_article_text(html, max_chars=_FIRST_DEDUPE_SNIPPET_CHARS)
                if len(text.strip()) > 100:
                    snippets[i] = text.strip()[: _FIRST_DEDUPE_SNIPPET_CHARS]
                    fetches_done += 1
        except Exception:
            pass

    lines = []
    for i, it in enumerate(items):
        title = (it.get("title") or "").strip() or "—"
        date_s = _date_str(it)
        outlet = _press_outlet_from_item(it)
        url = (it.get("url") or it.get("link") or "").strip()
        if len(url) > 72:
            url = url[:69] + "..."
        lines.append(f"{i}: {title} | {date_s} | {outlet} | {url}")
        if snippets[i]:
            body = (snippets[i] or "").replace("\n", " ").strip()[: _FIRST_DEDUPE_SNIPPET_CHARS]
            lines.append(f"  body: {body}")
        elif it.get("snippet") or it.get("feed_snippet"):
            # Feed snippet (e.g. from Google News RSS) when body fetch failed — helps LLM see similarity.
            snip = (it.get("snippet") or it.get("feed_snippet") or "").replace("\n", " ").strip()[: _FIRST_DEDUPE_SNIPPET_CHARS]
            if snip:
                lines.append(f"  snippet: {snip}")

    system = (
        "You filter and deduplicate a press list for one hospitality/hotels company. "
        "Return ONLY a JSON array of 0-based indices to KEEP. Example: [0, 2, 5]. No other text.\n\n"
        "EXCLUDE (omit their indices):\n"
        "- Articles that have NOTHING to do with: hospitality, hotels, lodging, real estate, "
        "fundraising, company/business (the target company or its industry).\n"
        "- Wrong entity (e.g. person or place with same name as the company), unrelated industry, "
        "obituaries, pure entertainment/culture with no business angle.\n"
        "For 'Lark' only include Lark Hotels / Lark Hospitality; exclude persons/places named Lark.\n\n"
        "MERGE (keep one index per story):\n"
        "- Same article content (syndicated, or near-identical body) = one index.\n"
        "- Same story from different outlets (same event, same facts) = one index; prefer the best source.\n"
        "Use the body text to decide: if two items describe the same event in the same way, keep one.\n\n"
        "Your array MUST be shorter than the input when there are irrelevant items or content duplicates. "
        "Order: most recent or best source first. Return only the JSON array, e.g. [0, 2, 5]."
    )
    company_note = f"Target company: {competitor_name}.\n\n"
    if (competitor_name or "").strip().lower() == "lark":
        company_note = "Target company: Lark (the hotel/hospitality company—Lark Hotels, Lark Hospitality. Exclude persons or places named Lark.)\n\n"
    user = (
        company_note
        + "Items (index: title | date | outlet | url; optional '  body: ...'). Use body to exclude non-relevant and merge same content.\n\n"
        + "\n".join(lines)
        + "\n\nReturn ONLY a JSON array of indices to KEEP, e.g. [0,2,5]. No explanation."
    )

    def _parse_indices(content: str, n_items: int) -> Optional[List[int]]:
        if not content or n_items <= 0:
            return None
        text = content.strip()
        if "```" in text:
            text = re.sub(r"^```\w*\n?", "", text).rstrip("`\n")
        try:
            data = json.loads(text)
            if isinstance(data, list):
                indices = [int(x) for x in data if isinstance(x, (int, float)) and 0 <= int(x) < n_items]
                return list(dict.fromkeys(indices))
        except Exception:
            pass
        for match in re.finditer(r"\[[\d\s,]+\]", text):
            try:
                arr = json.loads(match.group(0))
                indices = [int(x) for x in arr if isinstance(x, (int, float)) and 0 <= int(x) < n_items]
                if indices:
                    return list(dict.fromkeys(indices))
            except Exception:
                continue
        num_match = re.search(r"[\d\s,]+", text)
        if num_match:
            indices = []
            for part in re.split(r"[\s,]+", num_match.group(0)):
                try:
                    idx = int(part)
                    if 0 <= idx < n_items and idx not in indices:
                        indices.append(idx)
                except ValueError:
                    continue
            if indices:
                return indices
        return None

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            max_tokens=2000,
            temperature=0.1,
        )
        content = (resp.choices[0].message.content or "").strip()
        if not content:
            return items
        indices = _parse_indices(content, len(items))
        if not indices:
            return items
        result = [items[i] for i in indices]
        if len(result) < len(items):
            print(f"[press] First dedupe (body): {len(items)} -> {len(result)} items", file=sys.stderr)
        return result
    except Exception:
        return items


# Title patterns that indicate roundups/listicles — exclude before or during final dedupe.
_FINAL_DEDUPE_ROUNDUP_TITLE_PATTERNS = (
    "things to know for",
    "things to know for jan",
    "things to know for feb",
    "5 things to know",
    " things to know",
    "daily brief",
    "week in review",
    "roundup:",
    "morning brief",
    "today's top",
)


def _is_obvious_roundup_title(title: str) -> bool:
    """True if the title clearly indicates a roundup/listicle (multi-topic, not a single company story)."""
    if not title or not isinstance(title, str):
        return False
    t = title.lower().strip()
    return any(p in t for p in _FINAL_DEDUPE_ROUNDUP_TITLE_PATTERNS)


def _dedupe_canonical_press_with_llm(competitor_name: str, items: List[dict]) -> List[dict]:
    """
    Final dedupe pass: LLM reads titles (and optional snippets) and returns indices to KEEP.
    (1) Removes items not relevant to hotel/short-term stay business based on the title.
    (2) Merges items that are published close in date AND on a similar topic (same story type:
        e.g. same hotel opening, same partnership, same executive hire)—keep one index per story.
    Returns a list of items (subset, order preserved). On failure, returns the original list unchanged.
    """
    import sys
    if not items or len(items) > 150:
        return items
    client = _openai_client()
    if not client:
        return items

    # Drop obvious roundup/listicle titles so they never reach the LLM (and to reduce prompt size).
    non_roundup: List[dict] = []
    for it in items:
        title = (it.get("title") or "").strip()
        if _is_obvious_roundup_title(title):
            continue
        non_roundup.append(it)
    if len(non_roundup) < len(items):
        print(f"[press] Final dedupe: dropped {len(items) - len(non_roundup)} obvious roundup(s) by title", file=sys.stderr)
    items = non_roundup
    if not items:
        return []

    def _date_str(it: dict) -> str:
        raw = it.get("date")
        if raw is None:
            return "no date"
        if hasattr(raw, "strftime"):
            return raw.strftime("%Y-%m-%d")
        s = (raw if isinstance(raw, str) else str(raw)).strip()
        return s[:10] if len(s) >= 10 else (s or "no date")

    # Optional: fetch short snippets for context when title alone is ambiguous (capped).
    snippets: List[Optional[str]] = [None] * len(items)
    fetches_done = 0
    for i, it in enumerate(items):
        if fetches_done >= _DEDUPE_MAX_SNIPPET_FETCHES:
            break
        url = (it.get("url") or it.get("link") or "").strip()
        if not url or not url.startswith("http"):
            continue
        try:
            html = _fetch_article_html(url, timeout=_DEDUPE_FETCH_TIMEOUT)
            if html:
                text = _html_to_article_text(html, max_chars=_DEDUPE_SNIPPET_CHARS)
                if len(text.strip()) > 80:
                    snippets[i] = text.strip()[: _DEDUPE_SNIPPET_CHARS]
                    fetches_done += 1
        except Exception:
            pass

    # Format each item as a clear block. Always include both title and summary so the LLM can compare them across items.
    blocks = []
    for i, it in enumerate(items):
        title = (it.get("title") or "").strip() or "—"
        date_s = _date_str(it)
        outlet = _press_outlet_from_item(it) or "—"
        summary = (it.get("summary") or "").strip()
        if not summary or summary == title:
            summary = title  # So we always have something to compare; model uses title when summary was missing
        block_lines = [
            f"[{i}]",
            f"title: {title}",
            f"date: {date_s}",
            f"outlet: {outlet}",
            f"summary: {summary[:400]}",
        ]
        if snippets[i]:
            snip = (snippets[i] or "").replace("\n", " ").strip()[: _DEDUPE_SNIPPET_CHARS]
            block_lines.append(f"snippet: {snip}")
        elif it.get("snippet") or it.get("feed_snippet"):
            snip = (it.get("snippet") or it.get("feed_snippet") or "").replace("\n", " ").strip()[: _DEDUPE_SNIPPET_CHARS]
            if snip:
                block_lines.append(f"snippet: {snip}")
        blocks.append("\n".join(block_lines))

    user_body = "\n---\n".join(blocks)
    print(f"[press] Final dedupe: sending {len(items)} items to LLM (user payload {len(user_body)} chars)", file=sys.stderr)
    print(f"[press] Final dedupe: first 1000 chars of items:\n{user_body[:1000]}", file=sys.stderr)

    system = (
        "You are cleaning a press list for a company in the hotel or short-term stay business. "
        "You will receive a list of items. Each item is a block with [INDEX], title, date, outlet, and summary (and sometimes snippet). "
        "You MUST read both title and summary for every item to compare with others.\n\n"
        "Return ONLY a JSON array of the 0-based indices to KEEP. Example: [0, 2, 5]. No other text, no explanation.\n\n"
        "1) EXCLUDE (omit their index): Items not relevant to hotels/short-term stay/hospitality; roundups/listicles (e.g. '5 things to know', 'daily brief'); wrong company or person with same name. For Lark, only Lark Hotels / Lark Hospitality. Use both title and summary to decide.\n\n"
        "2) MERGE: Compare items using BOTH title and summary. If two or more items are the SAME story (same opening, same partnership, same deal) and dates are close, keep only ONE index: the one with the MOST RECENT date (the most up-to-date article). Same property name or same partner name + similar summary = same story; then keep the latest.\n\n"
        "3) Your array must list only indices to KEEP, in order (most recent first). The array MUST be shorter than the number of items when there are irrelevant items or duplicate stories."
    )
    company_note = ""
    if (competitor_name or "").strip().lower() == "lark":
        company_note = "Target company: Lark (Lark Hotels / Lark Hospitality). Exclude persons or places named Lark.\n\n"
    else:
        company_note = f"Target company: {competitor_name}.\n\n"
    user = (
        company_note
        + "Read each block below. Each block is one item with title, date, outlet, and summary. "
        "For every item, use BOTH title and summary to compare with other items. "
        "Exclude irrelevant/roundups. When items are the same story (by title + summary), merge to one and keep the index with the most recent date. "
        "Return a JSON array of indices to KEEP.\n\n"
        + user_body
        + "\n\nReturn ONLY a JSON array of indices to KEEP, e.g. [0,2,5]."
    )

    def _parse_dedupe_indices(content: str, n_items: int) -> Optional[List[int]]:
        """Extract list of indices from LLM response. Prefer shorter list when multiple arrays found (deduped result)."""
        if not content or n_items <= 0:
            return None
        text = content.strip()
        # Strip markdown code blocks
        if "```" in text:
            text = re.sub(r"^```\w*\n?", "", text).rstrip("`\n")
        # Try full JSON parse first
        try:
            data = json.loads(text)
            if isinstance(data, list):
                indices = [int(x) for x in data if isinstance(x, (int, float)) and 0 <= int(x) < n_items]
                return list(dict.fromkeys(indices))  # preserve order, dedupe
        except Exception:
            pass
        # Find all [n, n, n] style arrays
        candidates = []
        for match in re.finditer(r"\[[\d\s,]+\]", text):
            try:
                arr = json.loads(match.group(0))
                indices = [int(x) for x in arr if isinstance(x, (int, float)) and 0 <= int(x) < n_items]
                if indices:
                    candidates.append(list(dict.fromkeys(indices)))
            except Exception:
                continue
        if candidates:
            # Prefer the shortest list (most aggressive dedupe); if tied, take first
            return min(candidates, key=len)
        # Comma- or space-separated numbers without brackets
        num_match = re.search(r"[\d\s,]+", text)
        if num_match:
            indices = []
            for part in re.split(r"[\s,]+", num_match.group(0)):
                try:
                    idx = int(part)
                    if 0 <= idx < n_items and idx not in indices:
                        indices.append(idx)
                except ValueError:
                    continue
            if indices:
                return indices
        return None

    from .config import settings
    model = getattr(settings, "press_final_dedupe_model", "gpt-4o") or "gpt-4o"
    print(f"[press] Final dedupe: using model={model}", file=sys.stderr)
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            max_tokens=2000,
            temperature=0,
        )
        content = (resp.choices[0].message.content or "").strip()
        if not content:
            print(f"[press] Final dedupe: no LLM response", file=sys.stderr)
            return items
        print(f"[press] Final dedupe: LLM response ({len(content)} chars), preview: {content[:500]!r}", file=sys.stderr)
        indices = _parse_dedupe_indices(content, len(items))
        if not indices:
            print(f"[press] Final dedupe: could not parse indices (response len={len(content)}, full preview={content[:400]!r})", file=sys.stderr)
            return items
        print(f"[press] Final dedupe: parsed {len(indices)} indices to keep: {indices[:20]}{'...' if len(indices) > 20 else ''}", file=sys.stderr)
        result = [items[i] for i in indices]
        if len(result) < len(items):
            print(f"[press] Final dedupe: {len(items)} -> {len(result)} items", file=sys.stderr)
        # If model returned same count, retry once with minimal prompt to force consolidation
        if len(indices) == len(items) and len(items) >= 2:
            print(f"[press] Final dedupe: no reduction ({len(items)} items), retrying with minimal prompt", file=sys.stderr)
            retry_system = (
                "Clean a press list for hotel/short-term stay. Return ONLY a JSON array of 0-based indices to KEEP. "
                "(1) Exclude items not relevant to hotels or short-term stay. "
                "(2) Merge items that are close in date AND same topic (same story). Your array MUST be shorter than the input. Example: [0,3,7]."
            )
            retry_user = (
                f"Company: {competitor_name}. {len(items)} items below (each block is one item; [N] is its index). "
                f"Remove non-relevant; merge same story. Return only a JSON array of indices to keep (must be fewer than {len(items)}).\n\n"
                + user_body
            )
            try:
                retry_resp = client.chat.completions.create(
                    model=model,
                    messages=[{"role": "system", "content": retry_system}, {"role": "user", "content": retry_user}],
                    max_tokens=2000,
                    temperature=0,
                )
                retry_content = (retry_resp.choices[0].message.content or "").strip()
                retry_indices = _parse_dedupe_indices(retry_content, len(items))
                if retry_indices and len(retry_indices) < len(items):
                    result = [items[i] for i in retry_indices]
                    print(f"[press] Final dedupe (retry): {len(items)} -> {len(result)} items", file=sys.stderr)
            except Exception:
                pass
        return result
    except Exception:
        return items


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
    Extract text for press article summarization. Prefer text from <p> paragraphs
    inside article/main so we skip leading metadata (title, date, byline, share links)
    that often appears before the body. Fall back to full root text if few paragraphs.
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return html[:max_chars] + "\n[... truncated]" if len(html) > max_chars else html
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(["script", "style", "noscript"]):
        tag.decompose()
    root = soup.find("article") or soup.find("main")
    if not root:
        root = soup.find("body") or soup
    # Prefer paragraph content so we don't feed metadata (title, date, byline, share) to the LLM.
    paragraphs = root.find_all("p")
    if paragraphs:
        body_from_p = "\n\n".join(p.get_text(separator=" ", strip=True) for p in paragraphs if p.get_text(strip=True))
        if len(body_from_p.strip()) >= 150:
            text = body_from_p
            text = re.sub(r"\n{3,}", "\n\n", text)
            if len(text) > max_chars:
                text = text[:max_chars] + "\n[... truncated]"
            return text
    text = root.get_text(separator="\n", strip=True)
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


def _normalize_domain(host: str) -> str:
    """Lowercase and strip leading www. for domain comparison."""
    if not host:
        return ""
    h = (host or "").lower().strip()
    if h.startswith("www."):
        h = h[4:]
    return h


def enrich_press_items_with_llm(
    competitor_name: str,
    items: List[dict],
    max_articles_to_summarize: int = 40,
    company_domains: Optional[List[str]] = None,
    previous_items: Optional[List[dict]] = None,
    previous_canonical: Optional[List[dict]] = None,
) -> List[dict]:
    """
    End-to-end press pipeline for a single competitor.

    When previous_items and previous_canonical are provided, only items not in previous_items
    are sent to the LLM (classify, story key, filter); results are merged with previous_canonical
    and only new canonical rows get summaries.

    Deduping (same story = one row with primary + secondary_urls): we use (1) LLM-assigned story
    keys from headline + outlet + publication date (same event/thrust and rough time window →
    same key), then (2) merging clusters whose titles are very similar (Jaccard). So articles
    that are the same story but different headlines/dates (e.g. "Lark opening four properties
    in 2026" across several outlets) are grouped into one canonical item.

    Steps:
    - Drop items whose URL is on the competitor's own domain (company_domains), so Google News
      only appears when the link is not the competitor's own site. Exception: keep press_endpoint
      (user-added company links) even if on company domain.
    - All remaining items (Google News, PR Newswire, press_endpoint) are read/classified by LLM.
    - Review stage: PR Newswire and Google News are considered together; duplicates across both
      are merged into one canonical item (primary + secondary_urls). When titles match, PR Newswire
      takes precedence as primary; otherwise both sources are displayed (primary + secondary_urls).
    - Company-sourced (press_endpoint) items are reviewed carefully—may be promo, so we require
      is_about_company and drop promo/irrelevant where applicable.
    - For up to `max_articles_to_summarize` canonical articles, fetch HTML and ask
      LLM for a 1-line summary and (optionally refined) topic.

    Returns a list of canonical press dicts with keys:
    - title, url, date, outlet, topic, summary, secondary_urls (list[str])
    """
    if not items:
        return []

    # Drop items whose URL is on the competitor's own domain, so we only want Google News
    # (and other external sources) when the link is not the competitor's own site.
    # Exception: keep press_endpoint items (user-added company links) even if on company domain.
    if company_domains:
        import urllib.parse
        allowed_domains = {_normalize_domain(d) for d in company_domains if d}
        filtered_items: List[dict] = []
        for it in items:
            provider = (it.get("provider") or "").strip().lower()
            if provider == "press_endpoint":
                filtered_items.append(it)
                continue
            url = (it.get("url") or it.get("link") or "").strip()
            if not url:
                filtered_items.append(it)
                continue
            try:
                parsed = urllib.parse.urlparse(url)
                host = (parsed.netloc or "").strip()
                if not host:
                    filtered_items.append(it)
                    continue
                if _normalize_domain(host) in allowed_domains:
                    continue  # drop: external source pointing to competitor's own link
            except Exception:
                pass
            filtered_items.append(it)
        items = filtered_items
        if not items:
            return []

    # Incremental: only run LLM on items that were not in the last pull.
    if previous_items is not None and previous_canonical is not None:
        previous_urls = set()
        for it in previous_items:
            u = (it.get("url") or it.get("link") or "").strip()
            if u:
                previous_urls.add(u)
        new_items = [it for it in items if (it.get("url") or it.get("link") or "").strip() not in previous_urls]
        if not new_items:
            return previous_canonical
        items = new_items

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
        # Common marketing/blog/category path fragments for hospitality competitors.
        marketing_fragments = (
            "/blog",
            "/blogs/",
            "/category",
            "/categories/",
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

        # Any link to company's own blog/category/marketing page is not "about company as business"
        # (so press_endpoint filter will drop it). No need for title to look like a guide.
        if _looks_like_own_marketing_page(url):
            it["is_promo"] = True
            it["topic"] = "promo_or_brand_marketing"
            it["is_about_company"] = False
        elif _looks_like_guide_title(title):
            # Guide-style title on any URL: treat as marketing so we don't show in press.
            it["is_promo"] = True
            it["topic"] = "promo_or_brand_marketing"
            it["is_about_company"] = False

    # Filter to business-relevant items. Priority rules:
    # - PR Newswire: always include (press releases about the company; high priority).
    # - Google News: always include (RSS is already scoped to quoted company name; LLM can be
    #   conservative on is_about_company and would otherwise drop valid clips).
    # - User-provided company news (press_endpoint): LLM review only—may be promo, so we
    #   require is_about_company and drop promo/irrelevant.
    # - All other sources: require is_about_company and drop promo/irrelevant.
    filtered: List[dict] = []
    for it in classified:
        provider = (it.get("provider") or "").strip().lower()
        if provider == "prnewswire":
            filtered.append(it)
            continue
        if provider == "google_news":
            # Only drop clearly irrelevant; allow through so external coverage shows in dossier.
            topic = (it.get("topic") or "").strip().lower()
            if topic in {"irrelevant"}:
                continue
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

    # --- First dedupe (body-aware): drop irrelevant and merge by same content ---
    # Fetch article bodies and ask LLM to exclude non-relevant (not hospitality/hotels/real estate/
    # fundraising/company) and merge items that are same article content or same story. Reduces
    # list before story-key + Jaccard so we catch more dedupes and irrelevant early.
    filtered = _first_dedupe_press_with_body_llm(competitor_name, filtered)
    if not filtered:
        return []

    # --- Press deduping (how we decide "same story") ---
    # We treat two items as the same story when they refer to the same underlying event (same deal,
    # announcement, expansion, etc.), not just similar words. Deduping is done in two steps:
    # 1) Story keys: LLM sees headline + outlet + publication date and assigns a short canonical key
    #    per item. Same event → same key (model uses general thrust and rough time window, so e.g.
    #    "Lark opening four properties in 2026" across Jan 5–13 from different outlets get one key).
    # 2) Title-similarity merge: clusters that got different keys but have high title word overlap
    #    (Jaccard on non-stopwords) are merged, so we catch cases where the LLM split them.
    # Result: one canonical row per story (primary URL + secondary_urls for other links).
    # Dedupe: same story from multiple outlets → one canonical item (primary + secondary_urls).
    import re

    def _norm_title(title: str) -> str:
        t = (title or "").lower()
        t = re.sub(r"[^a-z0-9]+", " ", t)
        return t.strip()

    # Stopwords to exclude from fingerprint so "Four Lark Hotels to open in 2026" ~ "Lark Hotels to Open Four New Properties in 2026"
    _STOP = frozenset(
        "a an the and or but in on at to for of with by from as is was are were been be have has had do does did will would could should may might must can shall".split()
    )

    def _title_fingerprint(title: str) -> set:
        t = _norm_title(title or "")
        words = [w for w in t.split() if w and w not in _STOP and len(w) > 1]
        return set(words)

    def _jaccard(a: set, b: set) -> float:
        if not a and not b:
            return 1.0
        if not a or not b:
            return 0.0
        return len(a & b) / len(a | b)

    story_keys = _assign_press_story_keys(competitor_name, filtered)
    clusters: dict[str, List[dict]] = {}
    for i, it in enumerate(filtered):
        key = None
        if i < len(story_keys) and story_keys[i]:
            key = story_keys[i]
        if not key:
            key = _norm_title(it.get("title") or "")
        if not key:
            key = (it.get("url") or it.get("link") or "").lower()
        clusters.setdefault(key, []).append(it)

    # Merge clusters that refer to the same story (high title word overlap), e.g. "Four Lark Hotels to open 2026" variants.
    cluster_keys = sorted(clusters.keys())
    for ka in cluster_keys:
        if ka not in clusters:
            continue
        fp_a = _title_fingerprint((clusters[ka][0].get("title") or "").strip())
        for kb in list(clusters.keys()):
            if kb <= ka or kb not in clusters:
                continue
            fp_b = _title_fingerprint((clusters[kb][0].get("title") or "").strip())
            if _jaccard(fp_a, fp_b) >= 0.38:
                clusters[ka].extend(clusters[kb])
                del clusters[kb]

    # Source priority for picking primary when the same story appears from multiple sources.
    # All PR Newswire and Google News are reviewed together; duplicates are merged (one primary + secondary_urls).
    # When titles match (same story), PR Newswire takes precedence as primary; otherwise both are displayed.
    provider_rank = {
        "prnewswire": 6,   # challenge cases: same story from both → PR Newswire primary
        "google_news": 5,
        "press_endpoint": 5,
    }
    outlet_rank = {
        "prnewswire.com": 6,
        "www.prnewswire.com": 6,
        "news.google.com": 5,
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
        topic = (best.get("topic") or "").strip() or "other_business"
        # Normalize date to ISO string so dossier _parse_press_date and template display work.
        # If primary (e.g. PR Newswire) has no date, use the best date from any item in the cluster.
        raw_date = best.get("date")
        if raw_date is None and group:
            for it in group:
                rd = it.get("date")
                if rd is not None:
                    raw_date = rd
                    break
        if raw_date is None:
            date = None
        elif hasattr(raw_date, "isoformat"):
            date = raw_date.isoformat()
        elif isinstance(raw_date, str):
            date = raw_date.strip() or None
        else:
            date = str(raw_date).strip() or None

        rec = {
            "title": title,
            "url": url or None,
            "outlet": outlet or None,
            "date": date,
            "topic": topic,
            "secondary_urls": list({u for u in secondary_urls if u}),
            "summary": None,  # to be filled in below
        }
        if best.get("snippet") or best.get("feed_snippet"):
            rec["snippet"] = (best.get("snippet") or best.get("feed_snippet") or "").strip()[:500]
        canonical.append(rec)

    # When we ran only on new items: merge new canonicals into previous, so we only summarize truly new rows.
    if previous_items is not None and previous_canonical is not None:
        result = [dict(r) for r in previous_canonical]
        new_standalone: List[dict] = []
        for c in canonical:
            ct = (c.get("title") or "").strip()
            fp_c = _title_fingerprint(ct)
            merged = False
            for r in result:
                rt = (r.get("title") or "").strip()
                if _jaccard(fp_c, _title_fingerprint(rt)) >= 0.38:
                    sec = list(r.get("secondary_urls") or [])
                    if c.get("url"):
                        sec.append(c["url"])
                    sec.extend(c.get("secondary_urls") or [])
                    r["secondary_urls"] = list({u for u in sec if u})
                    merged = True
                    break
            if not merged:
                result.append(c)
                new_standalone.append(c)
        canonical = result
        _summarize_only_items = new_standalone
    else:
        _summarize_only_items = canonical

    # Summaries: only for items that don't have one yet (new items when incremental).
    summarized: List[dict] = []
    summarized_count = 0
    for item in canonical:
        out = dict(item)
        if item in _summarize_only_items:
            if summarized_count >= max_articles_to_summarize:
                out["summary"] = item.get("title") or "Press item"
            else:
                url = item.get("url") or ""
                html_text = _fetch_article_html(url) if url else None
                meta = _summarize_press_article_with_llm(
                    competitor_name=competitor_name,
                    title=item.get("title") or "",
                    outlet=item.get("outlet") or "",
                    date_str=item.get("date"),
                    url=url,
                    html_text=html_text,
                    topic=item.get("topic") or "other_business",
                )
                out["summary"] = meta.get("summary") or item.get("title") or "Press item"
                refined_topic = meta.get("refined_topic") or out.get("topic")
                if refined_topic in PRESS_TOPICS:
                    out["topic"] = refined_topic
                summarized_count += 1
        else:
            out["summary"] = item.get("summary") or item.get("title") or "Press item"
        summarized.append(out)

    # Final LLM pass: read titles/dates/outlets/links and remove duplicate stories (same idea);
    # when multiple items are the same story, keep the latest-dated. Improves over story-key + Jaccard alone.
    summarized = _dedupe_canonical_press_with_llm(competitor_name, summarized)

    return summarized
