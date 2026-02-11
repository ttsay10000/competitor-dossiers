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
import os
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
    # Common variants the model might return (reduces spurious Other)
    if v_lower in ("business & strategy", "business and strategy", "strategy", "operations", "hr", "human resources", "finance", "legal", "corporate"):
        return "Business & Strategy"
    if v_lower in ("property operations", "property ops", "hospitality", "on-property", "guest services", "hotel operations", "front desk", "housekeeping"):
        return "Property operations"
    if v_lower in ("sales / growth", "sales", "growth", "business development", "revenue"):
        return "Sales / Growth"
    if v_lower in ("ai / data", "ai", "data", "analytics", "data science"):
        return "AI / Data"
    if v_lower in ("product", "product management", "ux", "design"):
        return "Product"
    if v_lower in ("engineering", "software", "technology", "r&d", "development"):
        return "Engineering"
    if v_lower in ("marketing", "brand", "communications", "demand gen"):
        return "Marketing"
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
Map US regions and cities to the correct state (e.g. Central Oregon → Oregon, Emerald Coast → Florida, Coachella Valley → California). Do NOT put US cities or regions into "Other". Only use "Other" for non-property pages (career site, privacy, legal), non-US, or genuinely unclear. Return only the JSON array, no markdown."""
        user = f"Page text:\n\n{page_text}\n\nProperties (assign state from page text above; use Other if not clearly in a US state):\n" + "\n".join(lines)
    else:
        # Infer from url/name/market only (no page context)
        system = """You are a data enricher for US real estate/hospitality property lists. Locations will be summarized by state only.
Given a list of properties (index, url, name, market), output a JSON array with one object per property.
Each object must have: "index" (integer), "state" (US state full name only, e.g. "Texas"—no city), "city" (optional, omit if unknown).
Map US regions and cities to the correct state (e.g. Central Oregon → Oregon, Emerald Coast → Florida, Coachella Valley → California). Do NOT put US cities or regions into "Other". Only use "Other" for career site, privacy, non-property URL, non-US, or genuinely unclear. Return only the JSON array, no markdown."""
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
        region = (p.get("region") or "").strip()[:100]
        url_derived = infer_location_for_property(p)
        details = (p.get("details") or "").strip()[:80]
        parts = [f"{i}: name={name!r} url={url!r} market={market!r} url_derived_location={url_derived!r}"]
        if region:
            parts.append(f"region={region!r}")
        parts.append(f"details={details!r}")
        lines.append(" ".join(parts))

    system = """You are a data enricher for US real estate/hospitality property lists. Your job is to bucket properties by state.
Given the full list of properties below (each line: index, name, url, market, url_derived_location, optional region, details), assign a US state to each.
- Use full US state names only (e.g. "California", "Texas", "Florida"). No city in the state field.
- Use name, market, region, and url_derived_location to infer state. url_derived_location may be a state name, a destination/city (e.g. "Newport Beach", "Coachella Valley"), or "Unspecified". Map US regions and cities to the correct state (e.g. Central Oregon, Bend → Oregon; Emerald Coast, Destin → Florida; Hudson Valley, Hamptons → New York; Lake Tahoe, Palm Springs → California; Poconos → Pennsylvania).
- Do NOT put US cities or regions into "Other". Only use "Other" for: non-US, career site, privacy, non-property URLs, or genuinely unclear.
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
            region = (p.get("region") or "").strip()[:100]
            url_derived = infer_location_for_property(p)
            details = (p.get("details") or "").strip()[:80]
            parts = [f"{i}: name={name!r} url={url!r} market={market!r} url_derived_location={url_derived!r}"]
            if region:
                parts.append(f"region={region!r}")
            parts.append(f"details={details!r}")
            lines.append(" ".join(parts))

        system = """You are assigning a US state to each property for a real estate/hospitality competitor dashboard.
Given property name, url, market, optional region, and url_derived_location (hint from URL parsing), assign the correct state for each.
- Use full US state names only (e.g. "California", "Texas", "Florida"). No city in the state field.
- Use name, market, region, and url_derived_location to infer state. Map US regions and cities to the correct state (e.g. Central Oregon, Bend → Oregon; Emerald Coast, Destin → Florida; Hudson Valley, Hamptons → New York; Lake Tahoe, Palm Springs → California; Poconos → Pennsylvania).
- Do NOT put US cities or regions into "Other". Only use "Other" for: non-US, career site, privacy, non-property URLs, or genuinely unclear.
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
        region = (p.get("region") or "").strip()[:100]
        details = (p.get("details") or "").strip()[:150]
        parts = [f"{local_i} (orig={orig_i}): name={name!r} url={url!r} market={market!r}"]
        if region:
            parts.append(f"region={region!r}")
        parts.append(f"details={details!r}")
        lines.append(" ".join(parts))

    system = """You are a location researcher for US real estate/hospitality properties. Some properties are tagged "Other" because their location could not be determined from URL or metadata. Your task is to infer the correct US state (or Washington DC) from the property name, market, region, url, and any available context.

Use your knowledge of US geography: neighborhood names (e.g. Dupont Circle -> Washington DC, Brooklyn -> New York), city names, landmarks, regions. Property names often include the neighborhood or city. Map US regions to states (e.g. Central Oregon, Bend -> Oregon; Emerald Coast, 30A -> Florida; Hudson Valley, Hamptons -> New York; Lake Tahoe, Coachella Valley -> California; Poconos -> Pennsylvania).

Rules:
- Use full US state names only (e.g. "California", "New York", "Texas"). For Washington DC use "Washington DC".
- If you can confidently infer the state from the name, market, region, or URL path, assign it. Otherwise keep "Other". Do NOT leave as Other when the name or region clearly indicates a US location.
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

    from .rules.talent_rules import is_senior_role, job_functional_area as rule_based_functional_area

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
            else:
                # Jobs beyond batch (e.g. index >= 150) or missing from LLM response: use rule-based so they don't stay unset
                out["functional_area"] = rule_based_functional_area(j)
                out["is_senior"] = is_senior_role(j.get("title"))
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


def _headline_looks_like_exec_appointment(competitor_name: str, title: str) -> bool:
    """True if headline is clearly about the company appointing/hiring an executive (EVP, CFO, etc.)."""
    if not title or not competitor_name:
        return False
    t = title.lower().strip()
    # Company name (or first word, e.g. 'lark' for 'Lark Hotels') in headline
    name_bits = [w for w in (competitor_name or "").lower().split() if len(w) > 1]
    if not name_bits:
        return False
    company_in_title = any(b in t for b in name_bits)
    if not company_in_title:
        return False
    # Appointment/hire language
    appoint_words = ("appoints", "appointed", "names", "named", "hires", "hired", "joins as")
    exec_indicators = ("evp", "executive vice president", "cfo", "ceo", "cto", "coo", "cmo", "chief ", "commercial strategy", "vp of")
    has_appoint = any(w in t for w in appoint_words)
    has_exec = any(e in t for e in exec_indicators)
    return bool(has_appoint and has_exec)


def _headline_looks_like_hospitality_or_real_estate(competitor_name: str, title: str) -> bool:
    """True if headline clearly suggests hotel/real estate business — with or without company name (e.g. hotel, furnished apartment, hotel conversion)."""
    if not title:
        return False
    t = title.lower().strip()
    # Strong industry terms: relevant even when target company is not named (industry context)
    industry_terms = (
        "hotel conversion", "hotel ", "hotels ", "furnished apartment", "apartment collection",
        "hospitality", "real estate", "short-term rental",
    )
    if any(term in t for term in industry_terms):
        if _headline_looks_like_wrong_entity(competitor_name or "", title, ""):
            return False
        return True
    # Else require company name + business terms
    if not competitor_name:
        return False
    name_bits = [w for w in (competitor_name or "").lower().split() if len(w) > 1]
    if not name_bits:
        return False
    company_in_title = any(b in t for b in name_bits)
    if not company_in_title:
        return False
    business_terms = (
        "hotel", "hotels", "hospitality", "real estate", "property", "properties",
        "rental", "rentals", "apartment", "apartments", "short-term rental", "partnership",
        "placemakr", "avantstay", "lark hotels", "new property", "hotel opening", "opens ", " to open ",
        "opening ", "openings", "debut",
    )
    if not any(term in t for term in business_terms):
        return False
    if _headline_looks_like_wrong_entity(competitor_name, title, ""):
        return False
    return True


def _headline_looks_like_wrong_entity(competitor_name: str, title: str, outlet: str = "") -> bool:
    """True if headline/outlet clearly indicates non-hospitality (music, artistry, theater, healthcare, etc.) — wrong entity."""
    if not title:
        return False
    t = title.lower().strip()
    o = (outlet or "").lower().strip()
    name_lower = (competitor_name or "").lower()
    # Music/artistry: violin, concerto, symphony, Tessa Lark → person/performance, not hotel
    music_artistry = ("violin", "violinist", "concerto", "symphony", "tessa lark", "bluegrass", "mendelssohn")
    if any(m in t for m in music_artistry) and ("lark" in name_lower and ("lark" in t or any(b in t for b in name_lower.split()))):
        return True
    if "symphony" in o or "symphony.org" in o:
        if "lark" in t and "lark" in name_lower:
            return True
    # Healthcare: Lark + healthcare/digital healthcare/BIG CARiNG = Lark Health
    healthcare_signal = ("healthcare", "digital healthcare", "big caring", "lark health", "medical ")
    if "lark" in name_lower and any(n in t for n in name_lower.split()):
        if any(h in t for h in healthcare_signal):
            return True
    # Theater/venue: Lark Theater, Lark Theatre (arts venue)
    if ("lark theater" in t or "lark theatre" in t) and "lark" in name_lower:
        return True
    return False


def _headline_looks_like_common_word_or_other_entity(competitor_name: str, title: str, outlet: str = "") -> bool:
    """True if headline suggests 'lark' as common word (bird, adventure) or unrelated entity (restaurant, school, etc.). Lark-specific."""
    if (competitor_name or "").strip().lower() not in ("lark", "lark hotels"):
        return False
    t = (title or "").lower().strip()
    o = (outlet or "").lower().strip()
    # Bird: Rusty Bush Lark, bird sighting, birdguides (title or outlet; "lark" must appear)
    if "lark" not in t and "lark" not in o:
        pass  # skip bird checks
    elif "rusty bush lark" in t or ("rusty" in t and "bush" in t and "lark" in t) or ("lark" in t and "bird" in t) or "birdguides" in t or ("birdguides" in o and "lark" in t):
        return True
    # Bird sighting phrasing (e.g. "X seen for first time in N years")
    if "lark" in t and "seen for first time" in t and ("year" in t or "years" in t):
        return True
    # "a lark" = adventure/caper (e.g. opium lark, delightful lark)
    if " a lark" in t or " lark in " in t or "opium lark" in t or "delightfully dark lark" in t:
        return True
    # Unrelated proper names: Little Lark (restaurant), Meadow Lark (school), Lark Creek (shops)
    if "little lark" in t or "meadow lark" in t or "lark creek" in t:
        return True
    # Theater company "The Lark" (not Lark Theater venue)
    if "the lark takes wing" in t or "the lark review" in t:
        return True
    # TV show / entertainment: Lark Rise to Candleford (period drama), etc.
    if "rise to candleford" in t or "lark rise to candleford" in t:
        return True
    return False


def _headline_looks_like_tv_or_hobby(competitor_name: str, title: str, outlet: str = "") -> bool:
    """True if headline is about TV/film/entertainment review or hobbyist/nature (e.g. birding) — not hospitality."""
    if not title:
        return False
    t = (title or "").lower().strip()
    o = (outlet or "").lower().strip()
    # TV / film / culture review (e.g. "X review – tender, evocative tribute")
    if " review" in t or " review –" in t or " review -" in t:
        if any(w in t for w in ("tender", "evocative", "tribute", "drama", "tv ", "series", "episode")):
            return True
    # Lark-specific: TV show "Lark Rise to Candleford"
    if (competitor_name or "").strip().lower() in ("lark", "lark hotels") and "rise to candleford" in t:
        return True
    # Hobbyist / nature: "X seen for first time in N years" (e.g. bird sightings) when company name in title — likely nature/hobby, not hotel
    name_bits = [w for w in ((competitor_name or "").lower().split()) if len(w) > 1]
    if name_bits and any(b in t for b in name_bits) and "seen for first time" in t and ("year" in t or "years" in t):
        # Exclude hotel/hospitality phrasing so we don't drop "Hotel X reopening seen for first time in 10 years"
        if not any(h in t for h in ("hotel", "hospitality", "reopen", "opening", "property")):
            return True
    return False


def _headline_looks_like_sports_or_non_hospitality(competitor_name: str, title: str, outlet: str = "") -> bool:
    """True if headline is about sports/athletics or a non-hotel sibling (e.g. restaurant) — not the hospitality company."""
    if not title:
        return False
    t = (title or "").lower().strip()
    o = (outlet or "").lower().strip()
    # Sports: soccer, football, goalkeeper, coach, staff directory (athletics), gopoly, etc.
    sports_signals = (
        "goalkeeper", "soccer", "football", "basketball", "women's soccer", "men's soccer",
        "athletics", "staff directory", "gopoly", "sports staff", "coach - ", " - coach",
    )
    if any(s in t for s in sports_signals) or any(s in o for s in ("gopoly", "athletics")):
        return True
    # Non-hotel sibling / restaurant: "lark sibling", "slab sandwich", etc. — not hotel press
    if "lark sibling" in t or "slab sandwich" in t or ("sibling" in t and "sandwich" in t):
        return True
    return False


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
    # Prefer fetching body for ambiguous headlines (company name present but no clear hospitality signal)
    # so the LLM can use article content to decide relevance.
    name_bits = [w for w in (competitor_name or "").lower().split() if len(w) > 1]
    hospitality_hint = (
        "hotel", "hotels", "hospitality", "opening", "openings", "opens", "partners", "partnership",
        "evp", "cfo", "ceo", "appoints", "property", "properties", "mews", "olive", "sonder",
    )
    def _headline_ambiguous(it: dict) -> bool:
        t = (it.get("title") or "").lower()
        if not name_bits or not any(b in t for b in name_bits):
            return False
        return not any(h in t for h in hospitality_hint)
    fetch_order = sorted(range(len(batch)), key=lambda i: (0 if _headline_ambiguous(batch[i]) else 1, i))
    bodies: List[Optional[str]] = [None] * len(batch)
    fetches_done = 0
    for i in fetch_order:
        if fetches_done >= _CLASSIFY_BODY_MAX_FETCHES:
            break
        it = batch[i]
        url = (it.get("url") or it.get("link") or "").strip()
        if not url or not url.startswith("http"):
            continue
        try:
            html = _fetch_article_html(url, timeout=_CLASSIFY_BODY_FETCH_TIMEOUT)
            if html:
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
        "You classify news items for a hospitality/real estate COMPANY. "
        "Your job is to decide: is this article ABOUT that company (relevant) or not (irrelevant)?\n\n"
        "RELEVANT = The article is primarily about the TARGET COMPANY as a business: "
        "its hotels, properties, openings, partnerships (e.g. Mews, olive), exec appointments (EVP, CFO), "
        "press releases from the company, or hospitality/real-estate moves by that company. "
        "When body text is provided, read it: if it clearly describes this company's hotels, openings, or leadership, mark relevant.\n\n"
        "IRRELEVANT = The article is NOT about the target company. Set topic=irrelevant and is_about_company=false when:\n"
        "(1) Wrong entity: A different person, place, or business that happens to share the name "
        "(e.g. LARK Toys, Tessa Lark violinist, Lark Health/digital healthcare, Lark Theater, Lark Street corridor, Lark Davis crypto, LARK Distilling, Landmark Bancorp, obituaries).\n"
        "(2) Common word 'lark': The word appears as the bird (e.g. Rusty Bush Lark, bird sightings), "
        "or as the phrase 'a lark' meaning a fun adventure/caper (e.g. 'opium lark', 'a delightful lark'), "
        "or as part of an unrelated business/place name (e.g. Little Lark restaurant, Meadow Lark school, The Lark theater company, Lark Creek shops).\n"
        "(3) TV shows, film/theater reviews, hobbyists (e.g. birding, nature sightings), entertainment, or general culture — set irrelevant. Not hotel/hospitality.\n"
        "(4) Headline mentions the name but the ARTICLE BODY is about something else. When body: is present, use it as the main signal: "
        "if the body clearly describes a bird, a person, a school, a restaurant, a theater company, TV/film, hobbyist activity, or any non-hospitality subject, set irrelevant even if the headline is ambiguous.\n\n"
        f"Topics: {topics_str}\n\n"
        "STEP 1 — Is this article about the target company? If NO → topic=irrelevant, is_about_company=false. Skip Step 2.\n"
        "If YES (article is about the hospitality company's hotels, openings, partnerships, exec hires, or press), go to Step 2.\n"
        "EXCEPTION: 'Company appoints EVP/CFO/Executive Vice President' is about the company → topic=other_business, is_about_company=true.\n\n"
        "STEP 2 — Assign one topic: new_hotel_opening, new_partnership, fundraising, restructuring_or_layoffs, executive_interview, promo_or_brand_marketing, or other_business.\n"
        "other_business: exec appointments, key hires, strategy. Guides/roundups/listicles = promo_or_brand_marketing.\n\n"
        "Output: JSON array of objects with index (int), is_about_company (bool), topic (string), is_promo (bool). One per line."
    )
    company_note = f"Target company: {competitor_name}."
    if (competitor_name or "").strip().lower() in ("lark", "lark hotels"):
        company_note = (
            "Target company: Lark (Lark Hotels / Lark Hospitality) — the hotel/hospitality company only.\n"
            "IRRELEVANT (set topic=irrelevant): LARK Toys, Tessa Lark/violin/symphony, Lark Health/BIG CARiNG, Lark Theater, Lark Street BID, Lark Davis crypto, LARK Distilling, Landmark Bancorp; "
            "birds (Rusty Bush Lark, bird sightings), hobbyists/nature; TV shows (e.g. Lark Rise to Candleford), film/theater reviews, entertainment; "
            "'a lark' = adventure (e.g. opium lark); Little Lark restaurant, Meadow Lark school, The Lark theater company, Lark Creek; obituaries; "
            "any article whose body is about a different subject.\n"
            "RELEVANT: Lark Hotels openings, Lark Hospitality, Lark partners with Mews/olive, Lark appoints EVP/CFO, take over property, press releases about Lark hotels."
        )
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
        _topic_lower_to_canonical = {t.lower(): t for t in PRESS_TOPICS}
        for i, it in enumerate(items):
            out = dict(it)
            meta = by_index.get(i) or {}
            out["is_about_company"] = bool(meta.get("is_about_company"))
            topic_raw = (meta.get("topic") or "").strip()
            # Normalize LLM topic case so "Irrelevant" / "irrelevant" both map to canonical "irrelevant"
            out["topic"] = _topic_lower_to_canonical.get(topic_raw.lower(), "other_business")
            out["is_promo"] = bool(meta.get("is_promo"))
            # Override: wrong-entity (music, healthcare, theater) → irrelevant
            if _headline_looks_like_wrong_entity(competitor_name, out.get("title") or "", out.get("outlet") or out.get("source") or ""):
                out["is_about_company"] = False
                out["topic"] = "irrelevant"
            # Override: explicit bird headline (Rusty Bush Lark) — Lark-specific
            elif (competitor_name or "").strip().lower() in ("lark", "lark hotels") and "rusty bush lark" in ((out.get("title") or "") + " " + (out.get("outlet") or "") + " " + (out.get("source") or "")).lower():
                out["is_about_company"] = False
                out["topic"] = "irrelevant"
            # Override: common-word/other entity (bird, "a lark", Little Lark restaurant, Meadow Lark, etc.) → irrelevant
            elif _headline_looks_like_common_word_or_other_entity(competitor_name, out.get("title") or "", out.get("outlet") or out.get("source") or ""):
                out["is_about_company"] = False
                out["topic"] = "irrelevant"
            # Override: sports/athletics or non-hotel sibling (e.g. restaurant) → irrelevant
            elif _headline_looks_like_sports_or_non_hospitality(competitor_name, out.get("title") or "", out.get("outlet") or out.get("source") or ""):
                out["is_about_company"] = False
                out["topic"] = "irrelevant"
            # Override: TV/film review or hobbyist/nature (e.g. bird sighting) → irrelevant
            elif _headline_looks_like_tv_or_hobby(competitor_name, out.get("title") or "", out.get("outlet") or out.get("source") or ""):
                out["is_about_company"] = False
                out["topic"] = "irrelevant"
            # Override: exec appointment headlines → always other_business
            elif _headline_looks_like_exec_appointment(competitor_name, out.get("title") or ""):
                out["is_about_company"] = True
                out["topic"] = "other_business"
            # Rescue: if still irrelevant but headline clearly hotel/real estate + company, keep as relevant
            elif (out.get("topic") or "").strip().lower() == "irrelevant" and _headline_looks_like_hospitality_or_real_estate(competitor_name, out.get("title") or ""):
                out["is_about_company"] = True
                out["topic"] = "other_business"
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
    # Same story type = same topic: all "new locations/properties" wording -> new_hotel_opening
    opening_keywords = (
        "opens", "opening", " open ", "debut", "debuts", "launches", "new hotel", "new property", "new properties", "new spots",
        "take over", "takeover", "taking over", "expands", "expanding", "adding", "reopening", "reopen", "hotel openings", "anticipated",
    )
    restructure_keywords = ("layoff", "restructuring", "restructure", "cut", "cuts jobs", "bankruptcy")
    exec_keywords = ("ceo", "cfo", "coo", "cto", "cmo", "chief ", "founder", "co-founder", "president", "head of", "executive")
    appointment_keywords = ("appoints", "appointed", "names", "named", "evp", "executive vice president", "joins as", "hired as", "new cfo", "new ceo", "new coo", "new evp")
    interview_keywords = ("interview", "q&a", "q&a:", "fireside chat", "conversation with", "talks with", "speaks with")
    promo_keywords = ("why we are", "why we’re", "why we are best", "top 10", "guide to", "how to", "tips for")

    # URL patterns that suggest listicle/comparison, not a story about the company.
    url_noise = ("vs-", "versus-", "alternatives", "comparison", "vs.", "alternatives-to-")
    # Generic phrases: obituaries etc. where company name may appear by coincidence.
    wrong_entity_generic = ("obituary", "funeral home", "legacy | obituary")
    # Lark-specific: many entities share the name (person, street, theater, healthcare, etc.).
    wrong_entity_lark = (
        "crypto expert", "lark davis", "lark street", "tessa lark", "violinist", "violin ",
        "street bid", "street corridor", "bluegrass", "mendelssohn", "crypto bottom",
        "lark theater", "lark theatre", "executive artistic director", "lark toys",
        "lark health", "digital healthcare", "healthcare transformation", "violin concerto",
        "symphony.org", "symphony debut", "landmark bancorp", "nasdaq:lark", "asx:lrk",
        "lark distilling", "lark ranch",
    )
    use_lark_phrases = "lark" in name_lower
    wrong_entity_phrases = wrong_entity_lark + wrong_entity_generic if use_lark_phrases else wrong_entity_generic
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

        # Override: wrong-entity (music, healthcare, theater) → irrelevant
        if _headline_looks_like_wrong_entity(competitor_name, it.get("title") or "", it.get("outlet") or it.get("source") or ""):
            is_about = False
            topic = "irrelevant"
        # Override: common-word/other entity (bird, "a lark", Little Lark, Meadow Lark, etc.) → irrelevant
        elif _headline_looks_like_common_word_or_other_entity(competitor_name, it.get("title") or "", it.get("outlet") or it.get("source") or ""):
            is_about = False
            topic = "irrelevant"
        # Override: sports/athletics or non-hotel sibling (e.g. restaurant) → irrelevant
        elif _headline_looks_like_sports_or_non_hospitality(competitor_name, it.get("title") or "", it.get("outlet") or it.get("source") or ""):
            is_about = False
            topic = "irrelevant"
        # Override: TV/film review or hobbyist/nature (e.g. bird sighting) → irrelevant
        elif _headline_looks_like_tv_or_hobby(competitor_name, it.get("title") or "", it.get("outlet") or it.get("source") or ""):
            is_about = False
            topic = "irrelevant"
        # Override: exec appointment headline → always about company, other_business (never irrelevant or restructuring)
        elif _headline_looks_like_exec_appointment(competitor_name, it.get("title") or ""):
            is_about = True
            topic = "other_business"

        if not is_about and topic == "other_business":
            topic = "irrelevant"

        # Rescue: if irrelevant but headline clearly hotel/real estate + company, keep as relevant
        if topic == "irrelevant" and _headline_looks_like_hospitality_or_real_estate(competitor_name, it.get("title") or ""):
            is_about = True
            topic = "other_business"

        out["is_about_company"] = is_about
        out["topic"] = topic
        out["is_promo"] = is_promo
        result.append(out)
    return result


def _group_press_into_clusters_llm(competitor_name: str, items: List[dict]) -> List[dict]:
    """
    Group relevant press items by title, date, and general topic. Does NOT drop any article—
    every item is assigned to exactly one cluster. Each cluster gets a group_title and
    one_line_summary from the LLM based on the article titles in that group.

    Returns list of dicts: [{ "group_title": str, "one_line_summary": str, "articles": [ { "title", "url", "date", "outlet" }, ... ] }, ...].
    On failure or no API, returns a single group containing all items.
    """
    import sys
    if not items:
        return []
    client = _openai_client()
    if not client:
        print(f"[press] Group LLM: no API client, using single group for {len(items)} items", file=sys.stderr)
        sys.stderr.flush()
        return _fallback_single_group(items)

    def _date_str(it: dict) -> str:
        raw = it.get("date")
        if raw is None:
            return "no date"
        if hasattr(raw, "strftime"):
            return raw.strftime("%Y-%m-%d")
        s = (raw if isinstance(raw, str) else str(raw)).strip()
        return s[:10] if len(s) >= 10 else (s or "no date")

    def _to_article(it: dict) -> dict:
        return {
            "title": (it.get("title") or "").strip() or "—",
            "url": (it.get("url") or it.get("link") or "").strip(),
            "date": _date_str(it),
            "outlet": _press_outlet_from_item(it),
        }

    lines = []
    for i, it in enumerate(items):
        title = (it.get("title") or "").strip() or "—"
        date_s = _date_str(it)
        outlet = _press_outlet_from_item(it)
        lines.append(f"{i}: {date_s} | {outlet} | {title}")

    system = (
        "You are given the full list of filtered press articles. Read every article TITLE and group them by the same story or event.\n\n"
        "Input: N items (index 0 to N-1). Each line is INDEX | DATE | OUTLET | TITLE. Use only what you see in the TITLEs to decide grouping—same partnership, same hire, same opening = one group.\n\n"
        "Your job: put articles that cover the SAME story into one group. Each group gets a short headline (group_title) and optional one-line summary. Examples of group_title style:\n"
        "- Partnership/launch: \"Placemakr and Hilton launch partnership\" or \"Hilton and Placemakr launch Apartment Collection\"\n"
        "- Executive hire: \"Placemakr hires new EVP [person name]\" when titles mention a specific hire\n"
        "- Other: one clear headline that describes the story (e.g. \"New property opening in Phoenix\").\n\n"
        "Return a JSON object with one key: \"groups\". Value is an array of objects, each with:\n"
        "  \"group_title\": short headline for this story (see examples above),\n"
        "  \"one_line_summary\": one sentence summarizing the story based on the titles,\n"
        "  \"article_indices\": array of 0-based indices of items in this group.\n\n"
        "Rules: (1) Every index 0 to N-1 must appear in exactly one article_indices array. Do not drop any item. "
        "(2) Group by story using only the article titles: same event/deal/hire/opening = same group; unrelated = separate groups (or group of one). "
        "Return only valid JSON, no markdown or extra text."
    )
    user = (
        f"Target company: {competitor_name}.\n\n"
        "Below are ALL filtered articles (each line: INDEX | DATE | OUTLET | TITLE). Read every title and group by same story. Assign every index to exactly one group. Return JSON with \"groups\" array.\n\n"
        + "\n".join(lines)
        + '\n\nReturn only valid JSON: {"groups": [{"group_title": "...", "one_line_summary": "...", "article_indices": [0,1]}, ...]}'
    )

    def _parse_group_response(content: str, n_items: int) -> Optional[List[dict]]:
        if not content or n_items <= 0:
            return None
        text = content.strip()
        if "```" in text:
            text = re.sub(r"^```\w*\n?", "", text).rstrip("`\n")
        # If there is leading/trailing text, try to extract a JSON object
        if not (text.startswith("{") and text.strip().endswith("}")):
            match = re.search(r"\{[\s\S]*\"groups\"[\s\S]*\}", text)
            if match:
                text = match.group(0)
        # Fix common LLM JSON mistakes: trailing commas before ] or }
        text = re.sub(r",\s*([}\]])", r"\1", text)
        try:
            data = json.loads(text)
            groups_raw = data.get("groups") if isinstance(data, dict) else None
            if not isinstance(groups_raw, list):
                return None
            seen = set()
            result = []
            for g in groups_raw:
                if not isinstance(g, dict):
                    continue
                indices_raw = g.get("article_indices") or g.get("indices") or []
                indices = []
                for x in indices_raw:
                    try:
                        i = int(x) if isinstance(x, (int, float)) else int(x)
                        if 0 <= i < n_items and i not in seen:
                            indices.append(i)
                            seen.add(i)
                    except (ValueError, TypeError):
                        pass
                if not indices:
                    continue
                result.append({
                    "group_title": (g.get("group_title") or g.get("title") or "News").strip() or "News",
                    "one_line_summary": (g.get("one_line_summary") or g.get("summary") or "").strip() or "",
                    "article_indices": indices,
                })
            missing = [i for i in range(n_items) if i not in seen]
            if missing:
                result.append({
                    "group_title": "Other coverage",
                    "one_line_summary": "",
                    "article_indices": missing,
                })
            return result
        except Exception:
            return None

    try:
        print(f"[press] Group LLM: calling API for {len(items)} items", file=sys.stderr)
        sys.stderr.flush()
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            max_tokens=4000,
            temperature=0.1,
        )
        content = (resp.choices[0].message.content or "").strip()
        if not content:
            print(f"[press] Group LLM: empty response, using single group for {len(items)} items", file=sys.stderr)
            sys.stderr.flush()
            return _fallback_single_group(items)
        parsed = _parse_group_response(content, len(items))
        if not parsed:
            snippet = (content[:500] + "..." if len(content) > 500 else content)
            print(f"[press] Group LLM: could not parse response, using single group for {len(items)} items. Response snippet: {snippet!r}", file=sys.stderr)
            sys.stderr.flush()
            return _fallback_single_group(items)
        def _article_date_sort_key(art: dict) -> str:
            d = (art.get("date") or "").strip()
            if d and d != "no date" and len(d) >= 10:
                return d[:10]
            return "0000-00-00"

        out = []
        for g in parsed:
            arts = [items[i] for i in g["article_indices"]]
            group_articles = [_to_article(it) for it in arts]
            group_articles.sort(key=_article_date_sort_key, reverse=True)
            out.append({
                "group_title": g["group_title"],
                "one_line_summary": g["one_line_summary"],
                "articles": group_articles,
            })
        print(f"[press] Group LLM: {len(items)} items -> {len(out)} groups", file=sys.stderr)
        sys.stderr.flush()
        return out
    except Exception as e:
        print(f"[press] Group LLM error: {e}", file=sys.stderr)
        sys.stderr.flush()
        return _fallback_single_group(items)


def _fallback_single_group(items: List[dict]) -> List[dict]:
    """Single group containing all items when grouping LLM fails or is unavailable."""
    def _date_str(it: dict) -> str:
        raw = it.get("date")
        if raw is None:
            return "no date"
        if hasattr(raw, "strftime"):
            return raw.strftime("%Y-%m-%d")
        s = (raw if isinstance(raw, str) else str(raw)).strip()
        return s[:10] if len(s) >= 10 else (s or "no date")
    articles = [
        {
            "title": (it.get("title") or "").strip() or "—",
            "url": (it.get("url") or it.get("link") or "").strip(),
            "date": _date_str(it),
            "outlet": _press_outlet_from_item(it),
        }
        for it in items
    ]
    articles.sort(key=lambda a: (a.get("date") or "0000-00-00")[:10] if (a.get("date") or "").strip() and (a.get("date") or "").strip() != "no date" else "0000-00-00", reverse=True)
    return [{"group_title": "Press coverage", "one_line_summary": "", "articles": articles}]


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


def _normalize_domain(host: str) -> str:
    """Lowercase and strip leading www. for domain comparison."""
    if not host:
        return ""
    h = (host or "").lower().strip()
    if h.startswith("www."):
        h = h[4:]
    return h


def get_press_classification_for_inspection(
    competitor_name: str,
    items: List[dict],
    company_domains: Optional[List[str]] = None,
) -> List[dict]:
    """
    Classify press items and return each with topic, is_about_company, is_promo,
    and _included (bool), _drop_reason (str or None). Use for local inspection/debug
    to see full list and what is marked irrelevant or not. Does not run grouping.
    """
    if not items:
        return []
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
                if not host or _normalize_domain(host) not in allowed_domains:
                    filtered_items.append(it)
                # else drop: on company domain
            except Exception:
                filtered_items.append(it)
        items = filtered_items
        if not items:
            return []

    classified = _classify_press_headlines_with_llm(competitor_name, items)

    import re as _re
    name_slug = _re.sub(r"[^a-z0-9]", "", (competitor_name or "").lower())

    def _looks_like_own_marketing_page(url: str) -> bool:
        u = (url or "").lower()
        if not u or not name_slug or name_slug not in u:
            return False
        fragments = (
            "/blog", "/blogs/", "/category", "/categories/", "/destinations", "/destination/",
            "/itinerary", "/itineraries/", "/guide", "/guides/", "/owner", "/owners/", "/invest", "/awards",
        )
        return any(f in u for f in fragments)

    def _looks_like_guide_title(title: str) -> bool:
        t = (title or "").lower()
        if not t:
            return False
        kw = (
            "guide", "checklist", "how to ", "how-to ", "itinerary", "roundup", "review roundup",
            "what ", "need to know", "roi ", "valuation", "worth?", "alternatives", "vs ", "best ",
        )
        return any(k in t for k in kw)

    for it in classified:
        url = (it.get("url") or it.get("link") or "").strip()
        title = (it.get("title") or "").strip()
        if _looks_like_own_marketing_page(url):
            it["is_promo"] = True
            it["topic"] = "promo_or_brand_marketing"
            it["is_about_company"] = False
        elif _looks_like_guide_title(title):
            it["is_promo"] = True
            it["topic"] = "promo_or_brand_marketing"
            it["is_about_company"] = False

    result: List[dict] = []
    for it in classified:
        out = dict(it)
        provider = (out.get("provider") or "").strip().lower()
        topic = (out.get("topic") or "").strip().lower()
        drop_reason: Optional[str] = None
        if provider == "prnewswire":
            pass
        elif provider == "google_news":
            if topic == "irrelevant":
                drop_reason = "irrelevant"
            elif topic == "promo_or_brand_marketing":
                drop_reason = "promo"
        else:
            if topic == "irrelevant":
                drop_reason = "irrelevant"
            elif out.get("is_promo") and topic == "promo_or_brand_marketing":
                drop_reason = "promo"
            elif not out.get("is_about_company"):
                drop_reason = "not_about_company"
        out["_included"] = drop_reason is None
        out["_drop_reason"] = drop_reason
        result.append(out)
    return result


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

    Steps:
    - Drop items whose URL is on the competitor's own domain (company_domains). Exception: keep press_endpoint.
    - Classify all items (topic, irrelevant, promo) via LLM; apply heuristics; business-relevance filter.
    - Split by provider: PR Newswire vs rest. Group the rest via LLM (by title, date, topic) into clusters
      with group_title and one_line_summary; no articles dropped. Append one cluster "Press releases" for PR items.
    - Returns a list of groups: [{ "group_title", "one_line_summary", "articles": [ { "title", "url", "date", "outlet" }, ... ] }, ...].
    - previous_items / previous_canonical are accepted for API compatibility but not used; we re-group the full filtered list each run.
    """
    if not items:
        return []

    import sys

    # Drop items whose URL is on the competitor's own domain, so we only want Google News
    # (and other external sources) when the link is not the competitor's own site.
    # Exception: keep press_endpoint items (user-added company links) even if on company domain.
    if company_domains:
        import urllib.parse
        n_before_domain_filter = len(items)
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
        dropped_domain = n_before_domain_filter - len(items)
        print(
            f"[press] Company-domain filter: {n_before_domain_filter} -> {len(items)} items ({dropped_domain} on own domain dropped)",
            file=sys.stderr,
        )
        if not items:
            return []

    classified = _classify_press_headlines_with_llm(competitor_name, items)

    # Log how many were marked irrelevant (and promo) by classification — before filter and first dedupe.
    by_topic: dict = {}
    for it in classified:
        t = (it.get("topic") or "").strip() or "_empty"
        by_topic[t] = by_topic.get(t, 0) + 1
    n_irrelevant = by_topic.get("irrelevant", 0)
    n_promo = by_topic.get("promo_or_brand_marketing", 0)
    print(
        f"[press] Classify: {len(classified)} items — irrelevant={n_irrelevant}, promo_or_brand_marketing={n_promo}, other={len(classified) - n_irrelevant - n_promo}",
        file=sys.stderr,
    )

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
            topic = (it.get("topic") or "").strip().lower()
            if topic in {"irrelevant", "promo_or_brand_marketing"}:
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

    n_dropped = len(classified) - len(filtered)
    print(
        f"[press] Business filter: {len(classified)} -> {len(filtered)} items ({n_dropped} dropped). Grouping input: {len(filtered)} items",
        file=sys.stderr,
    )

    if not filtered:
        return []

    def _item_to_article(it: dict) -> dict:
        raw = it.get("date")
        if raw is None:
            date_s = "no date"
        elif hasattr(raw, "strftime"):
            date_s = raw.strftime("%Y-%m-%d")
        elif isinstance(raw, str):
            date_s = raw.strip()[:10] if len(raw.strip()) >= 10 else (raw.strip() or "no date")
        else:
            date_s = str(raw).strip()[:10] if str(raw).strip() else "no date"
        return {
            "title": (it.get("title") or "").strip() or "—",
            "url": (it.get("url") or it.get("link") or "").strip(),
            "date": date_s,
            "outlet": _press_outlet_from_item(it),
        }

    # Split PR Newswire vs rest; group only non-PR items, then append Press releases group.
    pr_items = [it for it in filtered if (it.get("provider") or "").strip().lower() == "prnewswire"]
    rest = [it for it in filtered if (it.get("provider") or "").strip().lower() != "prnewswire"]

    press_groups: List[dict] = []
    if rest:
        press_groups = _group_press_into_clusters_llm(competitor_name, rest)
    if pr_items:
        pr_articles = [_item_to_article(it) for it in pr_items]
        pr_articles.sort(key=lambda a: (a.get("date") or "0000-00-00")[:10] if (a.get("date") or "").strip() and (a.get("date") or "").strip() != "no date" else "0000-00-00", reverse=True)
        press_groups.append({
            "group_title": "Press releases",
            "one_line_summary": "Company press releases.",
            "articles": pr_articles,
        })

    print(
        f"[press] Press pipeline done: {len(press_groups)} groups",
        file=sys.stderr,
    )
    return press_groups
