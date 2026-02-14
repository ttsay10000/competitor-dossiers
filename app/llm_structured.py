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
import logging
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
    infer_state_from_name_and_market,
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


# Max chars of old/new page text sent to LLM for website change interpretation (control cost).
_WEBSITE_CHANGE_TEXT_MAX = 2500


def interpret_website_change(
    competitor_name: str,
    url: str,
    url_context: dict,
    old_text_snippet: Optional[str],
    new_text_snippet: Optional[str],
    coming_soon_phrases: Optional[List[str]] = None,
) -> Optional[dict]:
    """
    Ask LLM to interpret a website change: what changed and whether it's competitively important.
    url_context should have subdomain, path, host (e.g. from utils.parse_url_context).
    Returns dict with: summary (str), is_important (bool), reason (str), suggested_title (str)
    or None if API unavailable / failure.
    """
    client = _openai_client()
    if not client:
        return None
    old_text = (old_text_snippet or "")[:_WEBSITE_CHANGE_TEXT_MAX]
    new_text = (new_text_snippet or "")[:_WEBSITE_CHANGE_TEXT_MAX]
    subdomain = url_context.get("subdomain") or ""
    path = url_context.get("path") or ""
    host = url_context.get("host") or ""
    phrases = coming_soon_phrases or []
    system = """You are a competitive intelligence analyst for short-term rental / hospitality companies.
Given a competitor's name, the URL (and its subdomain/path) where a change was detected, and optional before/after visible text, you must:
1. summary: One short line only (max ~80 chars). State only the major change—e.g. "New locations list added", "Pricing section updated", "Coming soon messaging added". No filler. If only new text is given, use "Content changed; no previous version available" only if you cannot infer anything else.
2. is_important: true if new markets, product/positioning, pricing, partnerships, or material messaging; false if trivial (dates, copyright, typos, nav, cookie text).
3. reason: One brief sentence for importance or unimportance.
4. suggested_title: Short event title (max 80 chars) for an executive.

Reply with exactly this JSON (no markdown, no extra text):
{"summary": "...", "is_important": true or false, "reason": "...", "suggested_title": "..."}"""

    user_parts = [
        f"Competitor: {competitor_name}",
        f"URL: {url}",
        f"Location on site: subdomain={subdomain!r} path={path!r} host={host!r}",
    ]
    if phrases:
        user_parts.append(f"Detected phrases on page: {phrases}")
    if old_text:
        user_parts.append(f"Previous page text (excerpt):\n{old_text}")
    else:
        user_parts.append("Previous page text: (not available)")
    if new_text:
        user_parts.append(f"Current page text (excerpt):\n{new_text}")
    else:
        user_parts.append("Current page text: (empty or not available)")
    user_content = "\n\n".join(user_parts)

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ],
            temperature=0.2,
            max_tokens=400,
        )
        content = (resp.choices[0].message.content or "").strip()
        if not content:
            return None
        data = _parse_json_response(content)
        if not isinstance(data, dict):
            return None
        return {
            "summary": (data.get("summary") or "").strip() or "Content changed.",
            "is_important": bool(data.get("is_important", True)),
            "reason": (data.get("reason") or "").strip(),
            "suggested_title": (data.get("suggested_title") or "Website or product page updated")[:255],
        }
    except Exception:
        return None


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


def _infer_state_from_name_if_missing(prop: dict) -> dict:
    """
    When state is still missing, infer from name/market using rule-based city/region lookup
    (e.g. 'Beach House in Destin' -> Florida, 'Condo in Bend' -> Oregon). No LLM.
    """
    out = dict(prop)
    if (out.get("state") or "").strip():
        return out
    state = infer_state_from_name_and_market(out.get("name"), out.get("market"))
    if state:
        out["state"] = state
        if not (out.get("market") or "").strip():
            out["market"] = state
    return out


def enrich_properties_url_and_rules_only(properties: List[dict]) -> List[dict]:
    """
    Enrich properties with state only using URL (Avantstay-style path) and rule-based
    name/market inference. No LLM. Used for Vacasa and other large portfolios where we
    only need state-level location; HTML/data-* from the collector (e.g. data-state on
    links) is already on each property, and this fills in missing state from URL or name.
    """
    if not properties:
        return properties
    working = [_assign_state_from_url(p) for p in properties]
    working = [_infer_state_from_name_if_missing(p) for p in working]
    return working


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
    # Rule-based state from name/market (e.g. Vacasa "Beach House in Destin" -> Florida) when still missing
    working = [_infer_state_from_name_if_missing(p) for p in working]

    client = _openai_client()
    if not client:
        return working

    # Process in batches to handle 1000s of properties; raw page text only for first batch when count is small.
    _ENRICH_BATCH_SIZE = 150
    _ENRICH_SKIP_RAW_CONTENT_ABOVE = 250
    # Cap LLM enrichment to avoid 20–60+ min runs on huge lists (e.g. Vacasa ~26k properties → 174 batches).
    # Properties beyond this keep URL-derived state only (no LLM calls).
    _ENRICH_MAX_PROPERTIES = 2000
    to_enrich = working if len(working) <= _ENRICH_MAX_PROPERTIES else working[:_ENRICH_MAX_PROPERTIES]
    if len(working) > _ENRICH_MAX_PROPERTIES:
        print(f"[asset] enrich_properties_with_llm: capping LLM enrichment to first {_ENRICH_MAX_PROPERTIES} of {len(working)} properties (rest keep URL-derived state)", flush=True)
    use_raw = bool(raw_content and raw_content.strip() and len(to_enrich) <= _ENRICH_SKIP_RAW_CONTENT_ABOVE)

    by_index: dict[int, dict] = {}
    batch_ranges = [
        (start, min(start + _ENRICH_BATCH_SIZE, len(to_enrich)))
        for start in range(0, len(to_enrich), _ENRICH_BATCH_SIZE)
    ]

    for batch_start, batch_end in batch_ranges:
        batch = to_enrich[batch_start:batch_end]
        lines = []
        for i, p in enumerate(batch):
            url = (p.get("url") or "").strip()
            name = (p.get("name") or "").strip()
            market = (p.get("market") or "").strip()
            lines.append(f"{i}: url={url!r} name={name!r} market={market!r}")

        if use_raw and batch_start == 0:
            # LLM reads raw page and assigns state (and optional city) from page context (first batch only)
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
                continue
            data = _parse_json_response(content)
            if not isinstance(data, list):
                continue
            for item in data:
                if isinstance(item, dict) and "index" in item:
                    global_idx = batch_start + int(item["index"])
                    by_index[global_idx] = item
        except Exception:
            continue

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
- Use "Property operations" for on-property, guest-facing or property-level roles: front desk, housekeeping, maintenance, F&B (cook, server, bartender), concierge, night auditor, room attendant, valet, bellman, property management, field ops, hotel/restaurant operations. When in doubt and the title suggests on-site hospitality or property-level execution, choose Property operations.
- Use "Business & Strategy" for corporate/central roles: strategy, growth, product, engineering, data, marketing, sales, HR, finance, AND for guest experience associate, leasing agent, overnight guest experience associate (these are customer success, internal reservations, or virtual assistant roles, not on-property).
- Use "Business & Strategy", "Sales / Growth", "Marketing", "AI / Data", "Product", "Engineering" for other corporate/central roles.
- Use "Other" only when the role clearly does not fit any of the above.

Treat as senior only: C-level (CEO, CFO, etc.), VP, Vice President, Head of, or corporate Director. Do not treat as senior: General Manager, GM, or other property/venue-level or operations managers. Otherwise is_senior is false.
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
    combined = f"{t} {o}"
    name_lower = (competitor_name or "").lower()
    # Wrong company with same ticker/name: Landmark Bancorp (NASDAQ:LARK) is a bank, not Lark Hotels
    if "lark" in name_lower and ("landmark bancorp" in combined or "nasdaq:lark" in combined):
        return True
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
    # Place: Lark Street (Albany, NY corridor) — community events, street festivals, not Lark Hotels
    if "lark" in name_lower and "lark street" in combined:
        return True
    # Community/street events on Lark Street: "community event on lark", Santa Speedo Sprint, etc.
    if "lark" in name_lower and ("lark" in t or "lark street" in t) and any(
        p in combined
        for p in (
            "community event on lark",
            "event on lark street",
            "santa speedo sprint",
        )
    ):
        return True
    # Rove: Karl Rove (GOP strategist), Rove Miles (loyalty/rewards program) — not Rove Travel
    if (competitor_name or "").strip().lower() == "rove":
        if "karl rove" in combined or "rove miles" in combined:
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
    # Unrelated proper names: Little Lark (restaurant), Meadow Lark (school), Lark Creek (shops), Lark Street (Albany)
    if "little lark" in t or "meadow lark" in t or "lark creek" in t or "lark street" in (t + " " + o):
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
    elif (competitor_name or "").strip().lower() == "rove":
        company_note = (
            "Target company: Rove (Rove Travel / rovetravel.com) — furnished rental/hospitality company only.\n"
            "IRRELEVANT (set topic=irrelevant): Karl Rove (GOP/Republican strategist, political commentator); "
            "Rove Miles (loyalty/rewards program, airline miles, hotel points); political news, elections, midterms; "
            "any article about a person or program that is not Rove Travel.\n"
            "RELEVANT: Rove Travel properties, rovetravel.com, furnished rentals, hospitality, openings, partnerships."
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
            # Override: Karl Rove, Rove Miles — Rove-specific (not Rove Travel)
            elif (competitor_name or "").strip().lower() == "rove":
                _rove_text = ((out.get("title") or "") + " " + (out.get("snippet") or "") + " " + (out.get("outlet") or "") + " " + (out.get("source") or "")).lower()
                if "karl rove" in _rove_text or "rove miles" in _rove_text:
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
            # Rescue: Blueground (furnished apartments) — avoid over-filtering; headlines with blueground + housing terms are relevant
            elif (out.get("topic") or "").strip().lower() == "irrelevant" and (competitor_name or "").strip().lower() == "blueground":
                title_lower = ((out.get("title") or "") + " " + (out.get("outlet") or "") + " " + (out.get("source") or "")).lower()
                if "blueground" in title_lower and any(
                    term in title_lower for term in ("apartment", "rental", "furnished", "property", "real estate", "expansion", "partnership", "hire", "funding", "series")
                ):
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
    # Rove-specific: Karl Rove (GOP strategist), Rove Miles (loyalty/rewards) — not Rove Travel
    wrong_entity_rove = ("karl rove", "rove miles", "gop strategist", "republican strategist")
    use_lark_phrases = "lark" in name_lower
    use_rove_phrases = (competitor_name or "").strip().lower() == "rove"
    wrong_entity_phrases = (
        wrong_entity_lark + wrong_entity_generic
        if use_lark_phrases
        else wrong_entity_rove + wrong_entity_generic
        if use_rove_phrases
        else wrong_entity_generic
    )
    result: List[dict] = []
    for it in items:
        out = dict(it)
        title = (it.get("title") or "").lower()
        url = (it.get("url") or it.get("link") or "").lower()
        snippet = (it.get("snippet") or it.get("feed_snippet") or "").lower()
        text = f"{title} {snippet} {url}"

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
        # Rescue: Blueground — headlines with blueground + housing/business terms are relevant
        if topic == "irrelevant" and (competitor_name or "").strip().lower() == "blueground":
            title_lower = ((it.get("title") or "") + " " + (it.get("outlet") or "") + " " + (it.get("source") or "")).lower()
            if "blueground" in title_lower and any(
                term in title_lower for term in ("apartment", "rental", "furnished", "property", "real estate", "expansion", "partnership", "hire", "funding", "series")
            ):
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
        "You group press articles by the same story or event. Input: N items (index 0 to N-1). Each line: INDEX | DATE | OUTLET | TITLE. Use only titles to decide grouping.\n\n"
        "CITY/GEOGRAPHY RULE (critical): If an article mentions a city or region (Miami, Austin, Florida, Texas, Edgewater, etc.), that article is about that place. "
        "Articles about the SAME city/region especially if about a new location (e.g. hotel opening) MUST be grouped together. Treat as same location: Miami = Florida = Edgewater (Miami neighborhood); Austin = Texas. City names and their states are equivalent. "
        "Different cities are different stories: Miami and Austin are NOT the same—never group Austin articles with Miami articles. "
        "Example: \"AvantStay opens Sense28 in Miami\", \"Sense28 in Florida\", and \"Hotel to make Miami debut in Edgewater\" are ALL the same story—group them together. \"The Code in Austin\" is a different story (Austin ≠ Miami). Never put a city-specific article in Other coverage when a group about that city's opening exists. City matching is decisive.\n\n"
        "LATE COVERAGE: Articles about the same event often appear 1–3 weeks apart. Group by story, not by date. Late press about the same city + opening belongs in the same group.\n\n"
        "Your job: put articles that cover the SAME story into one group. Each group gets a short headline (group_title) and a brief summary (one_line_summary). "
        "The TARGET COMPANY name is provided below—use it in group_title when relevant (e.g. \"Placemakr and Hilton launch partnership\", \"AvantStay expands in Austin\", \"Lark Hotels partnership with Mews\"). "
        "Examples of group_title style for any hospitality company:\n"
        "- Partnership/launch: \"[Company] and [Partner] launch partnership\" or \"[Company] expands in [market]\"\n"
        "- Executive hire: \"[Company] hires new EVP [name]\" or \"[Company] appoints [role]\" when titles mention a specific hire\n"
        "- Openings/expansion: \"[Company] opens property in [city]\" or \"New [Company] locations\"\n"
        "- Other: one clear headline that describes the story (e.g. \"New property opening in Phoenix\").\n\n"
        "Other coverage: Reserve \"Other coverage\" ONLY for articles that are clearly unrelated to any grouped story (different city, different topic). Never put city-specific articles into Other when they match an existing group's city and story type.\n\n"
        "For one_line_summary: write a short but informative summary (1–2 sentences) based on the article titles. Include key details: what happened, who was involved, and any outcome or context (e.g. market, role, partner name). "
        "Avoid one-word or fragment summaries; aim for 15–40 words so a reader understands the story without opening the articles. "
        "Example: \"Placemakr and Hilton announced a partnership to bring Hilton’s hotel brands to Placemakr’s extended-stay properties; coverage highlighted the expansion of the company’s distribution.\"\n\n"
        "Return a JSON object with one key: \"groups\". Value is an array of objects, each with:\n"
        "  \"group_title\": short headline for this story (see examples above),\n"
        "  \"one_line_summary\": 1–2 sentence summary with key details from the titles (15–40 words),\n"
        "  \"article_indices\": array of 0-based indices of items in this group.\n\n"
        "Rules: (1) Every index 0 to N-1 must appear in exactly one article_indices array. Do not drop any item. "
        "(2) Group by story using only the article titles: same event/deal/hire/opening = same group; unrelated = separate groups (or group of one). "
        "Return only valid JSON, no markdown or extra text."
    )
    user = (
        f"Target company: {competitor_name} (use this name in group_title when it fits the story).\n\n"
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
        n_items = len(items)
        print(f"[press] Group LLM: calling API for {n_items} items", file=sys.stderr)
        sys.stderr.flush()
        # Scale tokens with item count so large lists don't truncate (Placemakr/AvantStay)
        max_tokens_group = min(8192, 400 + 200 * n_items)
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            max_tokens=max_tokens_group,
            temperature=0.1,
        )
        content = (resp.choices[0].message.content or "").strip()
        if not content:
            print(f"[press] Group LLM: empty response, using single group for {n_items} items", file=sys.stderr)
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


def summarize_top_news_llm(competitor_name: str, press_groups: List[dict], *, days: int = 14) -> Optional[List[dict]]:
    """
    After groupings are final: have the LLM read each group (topic + summaries + recent press)
    and output 3-5 key bullets of the most interesting news from a business perspective,
    with relevant dates, from the past `days` (default 14).

    press_groups: list of { group_title, one_line_summary, group_latest_date, articles: [{ title, date, outlet }] }.
    Returns list of { "bullet": str, "date": "YYYY-MM-DD" } or None on no client/parse failure.
    """
    from datetime import datetime, timedelta, timezone
    if not press_groups:
        return []
    client = _openai_client()
    if not client:
        return None

    now = datetime.now(timezone.utc)
    cutoff_date = (now - timedelta(days=days)).date()

    def _parse_group_date(g: dict):
        gd = (g.get("group_latest_date") or "").strip()
        if not gd or len(gd) < 10:
            return None
        try:
            return datetime.strptime(gd[:10], "%Y-%m-%d").date()
        except ValueError:
            return None

    # Only include groups with at least one article in the past `days` (e.g. 14 for "last 2 weeks").
    recent_groups = []
    for g in press_groups:
        gdate = _parse_group_date(g)
        if gdate is not None and gdate >= cutoff_date:
            recent_groups.append(g)
    # If no groups in window, allow fallback only for groups within 2x the window (e.g. 28 days when days=14).
    # Do not pull in very old articles as "top news".
    if not recent_groups:
        max_fallback_date = (now - timedelta(days=min(days * 2, 60))).date()
        with_date = [(g, _parse_group_date(g)) for g in press_groups if _parse_group_date(g) is not None]
        with_date = [(g, gd) for g, gd in with_date if gd >= max_fallback_date]
        with_date.sort(key=lambda x: -(x[1].toordinal() if x[1] else 0))
        recent_groups = [g for g, _ in with_date[:15]]

    if not recent_groups:
        return []

    lines = []
    for idx, g in enumerate(recent_groups[:25]):  # cap to avoid huge context
        title = (g.get("group_title") or "News").strip()
        summary = (g.get("one_line_summary") or "").strip()
        gdate = g.get("group_latest_date") or ""
        arts = g.get("articles") or []
        art_bits = []
        for a in arts[:5]:
            t = (a.get("title") or a.get("display_title") or "").strip() or "—"
            d = (a.get("date") or "").strip()[:10] if (a.get("date") or "").strip() else "no date"
            art_bits.append(f"  - {d} | {t}")
        block = f"Group index {idx}: {title}\n  Date: {gdate}\n  Summary: {summary}\n" + "\n".join(art_bits)
        lines.append(block)

    system = (
        "You are a business analyst summarizing competitor press for an executive dashboard.\n\n"
        "You receive the final grouped press: each block is ONE STORY/TOPIC (group_title, one_line_summary, "
        "latest article date, and article titles). Each block = one distinct story (e.g. one partnership, one opening, one hire). "
        "Each block is labeled with \"Group index N\" (0-based) — you must return that same index for the bullet so we can link to the article.\n\n"
        "Output 3-5 key bullets of the most interesting news from a business perspective (partnerships, "
        "expansion, funding, leadership, openings, strategy). Each bullet = ONE STORY/TOPIC: use the group headline and summary; "
        "do NOT list individual article titles. Synthesize one clear sentence per distinct story. "
        "Include the relevant date and the group_index (the N from \"Group index N\") for that story.\n\n"
        "Return a JSON object with one key: \"bullets\". Value is an array of objects, each with:\n"
        "  \"bullet\": one sentence summarizing that story (business-focused, from the group),\n"
        "  \"date\": \"YYYY-MM-DD\" (the relevant date for that news),\n"
        "  \"group_index\": number (0-based index of the group this bullet summarizes; must match one of the Group index N labels above).\n\n"
        "Rules: Only use information from the input. One bullet per group/story; prefer most recent and most business-relevant. "
        "Return only valid JSON, no markdown or extra text."
    )
    user = (
        f"Company: {competitor_name}\n\n"
        f"Below are press groups from the past {days} days. Summarize the most interesting business news as 3-5 bullets with dates and group_index.\n\n"
        + "\n\n".join(lines)
        + '\n\nReturn only valid JSON: {"bullets": [{"bullet": "...", "date": "YYYY-MM-DD", "group_index": 0}, ...]}'
    )

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=600,
            temperature=0.3,
        )
        choice = resp.choices[0] if resp.choices else None
        if not choice or not choice.message or not choice.message.content:
            return None
        text = choice.message.content.strip()
        if "```" in text:
            text = re.sub(r"^```\w*\n?", "", text).rstrip("`\n")
        if not (text.startswith("{") and text.strip().endswith("}")):
            match = re.search(r"\{[\s\S]*\"bullets\"[\s\S]*\}", text)
            if match:
                text = match.group(0)
        text = re.sub(r",\s*([}\]])", r"\1", text)
        data = json.loads(text)
        bullets_raw = data.get("bullets") if isinstance(data, dict) else None
        if not isinstance(bullets_raw, list):
            return None
        result = []
        n_groups = len(recent_groups)
        for b in bullets_raw[:5]:
            if not isinstance(b, dict):
                continue
            bullet = (b.get("bullet") or b.get("text") or "").strip()
            date_val = (b.get("date") or "").strip()[:10]
            group_index = b.get("group_index")
            if isinstance(group_index, (int, float)):
                gi = int(group_index)
            else:
                gi = -1
            url = None
            if 0 <= gi < n_groups:
                arts = (recent_groups[gi].get("articles") or [])
                if arts:
                    first = arts[0]
                    url = (first.get("url") or first.get("link") or "").strip()
                    if url and not url.startswith("http"):
                        url = None
            if bullet:
                result.append({
                    "bullet": bullet,
                    "date": date_val if len(date_val) >= 10 else None,
                    "url": url or None,
                })
        return result if result else None
    except Exception:
        return None


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
        company_domains_norm = {_normalize_domain(d) for d in company_domains if d}
        filtered_items: List[dict] = []
        for it in items:
            url = (it.get("url") or it.get("link") or "").strip()
            if not url:
                filtered_items.append(it)
                continue
            try:
                parsed = urllib.parse.urlparse(url)
                host = (parsed.netloc or "").strip()
                if not host or _normalize_domain(host) not in company_domains_norm:
                    filtered_items.append(it)
                # else drop: on company domain (including press_endpoint)
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
    - Company-domain filter: drop third-party items whose URL is on the competitor's domain.
      Keep items from the company's own press/blog (provider=press_endpoint) so new competitors
      with only a blog still get a "Company blog" group; otherwise groupings would be empty.
    - Classify all items (topic, irrelevant, promo) via LLM; apply heuristics; business-relevance filter.
      PR Newswire and Google News use the same filter (drop irrelevant and promo); no time window for PR.
    - Group the full filtered third-party list (PR Newswire + Google News, etc.) via LLM. Ungrouped
      items (LLM "Other coverage") are shown under "Press releases". Then append "Company blog" if present.
    - Returns a list of groups: [{ "group_title", "one_line_summary", "articles": [...] }, ...].
    - previous_items / previous_canonical are accepted for API compatibility but not used.
    """
    if not items:
        return []

    import sys
    import urllib.parse

    company_domains_normalized: set = set()
    if company_domains:
        company_domains_normalized = {_normalize_domain(d) for d in company_domains if d}

    def _url_on_company_domain(it: dict) -> bool:
        if not company_domains_normalized:
            return False
        url = (it.get("url") or it.get("link") or "").strip()
        if not url:
            return False
        try:
            host = (urllib.parse.urlparse(url).netloc or "").strip()
            if not host:
                return False
            host_norm = _normalize_domain(host)
            if host_norm in company_domains_normalized:
                return True
            if any(d and (host_norm == d or host_norm.endswith("." + d)) for d in company_domains_normalized):
                return True
        except Exception:
            pass
        return False

    # Drop third-party items (non press_endpoint) whose URL is on the competitor's domain.
    # Keep press_endpoint items (company blog) so new competitors get at least one group.
    if company_domains_normalized:
        n_before_domain_filter = len(items)
        filtered_items = []
        for it in items:
            provider = (it.get("provider") or "").strip().lower()
            if provider == "press_endpoint":
                filtered_items.append(it)
                continue
            if _url_on_company_domain(it):
                continue
            filtered_items.append(it)
        items = filtered_items
        dropped_domain = n_before_domain_filter - len(items)
        print(
            f"[press] Company-domain filter: {n_before_domain_filter} -> {len(items)} items ({dropped_domain} third-party on own domain dropped; press_endpoint kept)",
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

    # Filter to business-relevant items. Same rules for PR Newswire and Google News (drop
    # irrelevant and promo); no time window for PR—all PR that pass this filter are kept for display.
    # - PR Newswire and Google News: drop irrelevant and promo_or_brand_marketing.
    # - User-provided company news (press_endpoint): require is_about_company and drop promo/irrelevant.
    # - All other sources: require is_about_company and drop promo/irrelevant.
    filtered: List[dict] = []
    for it in classified:
        provider = (it.get("provider") or "").strip().lower()
        if provider in ("prnewswire", "google_news"):
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

    # Split: (1) company blog = press_endpoint on company domain; (2) rest = third-party + PR for grouping.
    company_blog_items = [
        it for it in filtered
        if (it.get("provider") or "").strip().lower() == "press_endpoint" and _url_on_company_domain(it)
    ]
    rest = [it for it in filtered if it not in company_blog_items]

    # Group full third-party list (PR Newswire + Google News, etc.) via LLM; ungrouped items go under Press releases.
    press_groups: List[dict] = []
    if rest:
        press_groups = _group_press_into_clusters_llm(competitor_name, rest)
        # Rename "Other coverage" (ungrouped items) to "Press releases" so ungrouped/standalone items appear there.
        for g in press_groups:
            if (g.get("group_title") or "").strip() == "Other coverage":
                g["group_title"] = "Press releases"
                g["one_line_summary"] = "Company press releases."
                break
    if company_blog_items:
        blog_articles = [_item_to_article(it) for it in company_blog_items]
        blog_articles.sort(key=lambda a: (a.get("date") or "0000-00-00")[:10] if (a.get("date") or "").strip() and (a.get("date") or "").strip() != "no date" else "0000-00-00", reverse=True)
        press_groups.append({
            "group_title": "Company blog",
            "one_line_summary": "Press and blog posts from the company.",
            "articles": blog_articles,
        })

    print(
        f"[press] Press pipeline done: {len(press_groups)} groups",
        file=sys.stderr,
    )
    return press_groups


# --- Social: promotion vs executive-relevant ------------------------------


SOCIAL_CLASSIFY_SYSTEM = """You classify company social media posts (Twitter/LinkedIn) for an executive audience.
For each post, output exactly one of: "promotion" or "executive".
- promotion: Marketing fluff, generic "we're hiring", seasonal campaigns, product plugs with no strategic signal. Do not alert executives.
- executive: New strategy, partnership, market entry, leadership change, product/positioning shift, funding/restructuring hints. Worth a one-line summary for the executive summary.
If executive, also provide a one-line summary (max 120 chars)."""


def enrich_social_posts_with_llm(posts: List[dict], batch_size: int = 12) -> List[dict]:
    """
    Classify each post as promotion vs executive-relevant. Add relevance and executive_summary.
    Returns posts with "relevance" (executive|promotion|unknown) and optional "executive_summary".
    """
    if not posts:
        return []
    client = _openai_client()
    if not client:
        for p in posts:
            p.setdefault("relevance", "unknown")
        return posts

    result: List[dict] = []
    for i in range(0, len(posts), batch_size):
        batch = posts[i : i + batch_size]
        batch_with_index = [(j, p) for j, p in enumerate(batch)]
        user_parts = [
            "Posts to classify (one per block). For each, reply with the block number, then 'promotion' or 'executive', and if executive add a one-line summary.",
        ]
        for j, p in batch_with_index:
            text = (p.get("text") or p.get("title") or "")[:800]
            url = (p.get("url") or "")[:200]
            user_parts.append(f"[{j}] {text}\nURL: {url}")
        user_text = "\n\n".join(user_parts)
        try:
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": SOCIAL_CLASSIFY_SYSTEM},
                    {"role": "user", "content": user_text},
                ],
                temperature=0.1,
            )
            content = (resp.choices[0].message.content or "").strip()
            # Parse lines like "0 executive Company announced partnership with X" or "1 promotion"
            by_idx: dict[int, dict] = {}
            for line in content.split("\n"):
                line = line.strip()
                if not line:
                    continue
                # Expect: number then executive|promotion then optional summary
                parts = line.split(None, 2)
                if len(parts) >= 2:
                    try:
                        idx = int(parts[0].rstrip(".)"))
                        rel = (parts[1] or "").lower()
                        if rel not in ("executive", "promotion"):
                            rel = "unknown"
                        summary = parts[2].strip()[:200] if len(parts) > 2 and rel == "executive" else ""
                        by_idx[idx] = {"relevance": rel, "executive_summary": summary}
                    except (ValueError, IndexError):
                        pass
            for j, p in batch_with_index:
                out = dict(p)
                info = by_idx.get(j, {})
                out["relevance"] = info.get("relevance", "unknown")
                out["executive_summary"] = info.get("executive_summary", "")
                result.append(out)
        except Exception:
            for _, p in batch_with_index:
                out = dict(p)
                out.setdefault("relevance", "unknown")
                out.setdefault("executive_summary", "")
                result.append(out)
    return result


def research_operating_model_llm(
    competitor_name: str,
    primary_domain: Optional[str] = None,
) -> Optional[str]:
    """
    Use OpenAI to describe the company's operating model for furnished rental / hospitality:
    - Revenue share (split % with property owners)
    - Owned and managed (they own and operate; typically no revenue share)
    - Master lease / lease arbitrage (they lease from owners and sublease)

    Returns 1-3 sentences or None if API unavailable.
    """
    client = _openai_client()
    if not client:
        return None
    name = (competitor_name or "").strip() or "the company"
    domain = (primary_domain or "").strip()
    context = f"Company: {name}"
    if domain:
        context += f" (website: {domain})"
    system = (
        "You are a researcher summarizing how furnished rental or short-term rental operators make money. "
        "Based on public information (website, press, industry knowledge), classify the operating model in 1-3 short sentences. "
        "Use one or more of: (1) Revenue share — state the typical split with owners if known (e.g. 70/30). "
        "(2) Owned and managed — they own and operate their own properties; no revenue share. "
        "(3) Master lease / lease arbitrage — they lease from owners and sublease to guests. "
        "If unclear, say so. Be concise; no bullet lists."
    )
    user = (
        f"{context}\n\n"
        "Describe this company's current operating model (revenue share, owned/managed, or master lease / lease arbitrage). "
        "One to three sentences only."
    )
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            max_tokens=300,
        )
        content = (resp.choices[0].message.content or "").strip()
        return content if content else None
    except Exception:
        return None


def generate_short_description_llm(
    competitor_name: str,
    primary_domain: Optional[str] = None,
) -> Optional[str]:
    """
    Generate a one-line short description for a competitor including business model.
    Uses: lease arbitrage / master lease, owned/managed, or revenue share.
    Returns a single line (e.g. "Furnished rental operator; revenue share with owners")
    or None if API unavailable.
    """
    client = _openai_client()
    if not client:
        logging.debug("Short description not generated for %s: OPENAI_API_KEY not set", competitor_name or "competitor")
        return None
    name = (competitor_name or "").strip() or "the company"
    domain = (primary_domain or "").strip()
    context = f"Company: {name}"
    if domain:
        context += f" (website: {domain})"
    system = (
        "You are a researcher writing a one-line description for a furnished rental or short-term rental operator. "
        "Write exactly one short sentence that includes: (1) what they do (e.g. furnished rental operator, corporate housing), "
        "and (2) their business model using one of: revenue share (with owners), owned/managed (they own and operate), "
        "or master lease / lease arbitrage (lease from owners, sublease to guests). "
        "Keep the whole line under 100 characters. No bullet points, no extra sentences."
    )
    user = (
        f"{context}\n\n"
        "Write one short line: what they do + business model (revenue share, owned/managed, or master lease / lease arbitrage)."
    )
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            max_tokens=150,
        )
        content = (resp.choices[0].message.content or "").strip()
        # Take only first sentence / line if model returned more
        if content:
            first_line = content.split("\n")[0].strip()
            first_sentence = first_line.split(". ")[0].strip()
            if first_sentence.endswith("."):
                return first_sentence
            return first_sentence + "." if first_sentence else None
        return None
    except Exception as e:
        logging.warning("Short description LLM failed for %s: %s", competitor_name or "competitor", e)
        return None
