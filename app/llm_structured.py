"""
LLM-based structuring for asset (state/city) and talent (functional_area, is_senior).
Primary path: always call OpenAI; on missing key or API failure return input unchanged (rules used as fallback downstream).
"""
import json
import re
from typing import Any, List, Optional

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


def _html_to_text_for_enricher(html: str, max_chars: int = _ENRICH_RAW_MAX_CHARS) -> str:
    """Reduce HTML to plain text for LLM (strip scripts, get body text, truncate). Mirrors asset collector logic."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return html[:max_chars] + "\n[... truncated]" if len(html) > max_chars else html
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(["script", "style", "noscript"]):
        tag.decompose()
    body = soup.find("body") or soup
    text = body.get_text(separator="\n", strip=True)
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
        # LLM reads raw page and assigns state/city from page context
        page_text = _html_to_text_for_enricher(raw_content.strip())
        system = """You are a data enricher for US real estate/hospitality property lists.
Given the page text below and a list of properties (index, url, name, market), assign state and city to each property using only information from the page text (e.g. cards, addresses, subheadings).
Output a JSON array with one object per property. Each object must have: "index" (integer), "state" (full US state name, e.g. "Texas", or "Other" if not US/unclear), "city" (optional, city name if evident from the page, else omit).
Use only standard US state names. For non-location or unclear entries use state "Other". Return only the JSON array, no markdown."""
        user = f"Page text:\n\n{page_text}\n\nProperties (assign state/city from page text above):\n" + "\n".join(lines)
    else:
        # Infer from url/name/market only (no page context)
        system = """You are a data enricher for US real estate/hospitality property lists.
Given a list of properties (index, url, name, market), output a JSON array with one object per property.
Each object must have: "index" (integer), "state" (US state full name, e.g. "Texas", or "Other" if not US/unclear), "city" (optional, city name if evident from url/name/market, else omit).
Use only standard US state names. For non-location or unclear entries use state "Other".
Return only the JSON array, no markdown."""
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
                out["state"] = (by_index[i].get("state") or "Other").strip() or "Other"
                if by_index[i].get("city"):
                    out["city"] = str(by_index[i].get("city", "")).strip()
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
