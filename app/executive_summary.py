"""
Generate a short, executive-level AI summary of competitor activity for the top of the dossier.
Uses OpenAI when OPENAI_API_KEY is set; otherwise returns None so the UI can show a fallback.
Also provides LLM-cleaned location display (State - City) for properties by location.
"""
import json
import re
from typing import Any, Dict, List, Optional


def _build_context_text(context: Dict[str, Any]) -> str:
    """Turn dossier context into a concise text block for the LLM."""
    parts = []
    name = context.get("competitor", {}).get("name", "Competitor")

    # Talent: job counts by function (business + property ops)
    jobs_by_function = context.get("jobs_by_function") or []
    jobs_property = context.get("jobs_by_function_property") or []
    total_jobs = sum(r.get("total", 0) for r in jobs_by_function) + sum(r.get("total", 0) for r in jobs_property)
    if total_jobs > 0:
        talent_lines = [f"Total open roles: {total_jobs}"]
        for r in jobs_by_function:
            talent_lines.append(f"  {r.get('function', '')}: {r.get('total', 0)} (senior: {r.get('senior', 0)})")
        for r in jobs_property:
            talent_lines.append(f"  {r.get('function', '')}: {r.get('total', 0)} (senior: {r.get('senior', 0)})")
        parts.append("Talent (current snapshot):\n" + "\n".join(talent_lines))
    else:
        parts.append("Talent: no job data in current snapshot.")

    # Events this week (titles only)
    events_week = context.get("events_this_week") or []
    if events_week:
        event_titles = [e.get("title", "") for e in events_week if e.get("title")]
        parts.append("Events detected this week: " + "; ".join(event_titles[:15]))
    else:
        parts.append("Events this week: none.")

    # Assets: baseline delta and by location
    added = context.get("asset_added_since_baseline") or 0
    removed = context.get("asset_removed_since_baseline") or 0
    baseline_date = context.get("asset_baseline_date")
    delta_by_city = context.get("asset_delta_by_city") or []
    props_by_loc = context.get("properties_by_location") or []

    if baseline_date:
        parts.append(
            f"Properties vs baseline ({baseline_date}): {added} added, {removed} removed."
        )
    if delta_by_city:
        loc_changes = [f"{r['location']}: +{r['added']}/−{r['removed']}" for r in delta_by_city[:15]]
        parts.append("By location (adds/removals): " + "; ".join(loc_changes))
    if props_by_loc:
        loc_counts = [f"{r['location']}: {r['count']}" for r in props_by_loc[:15]]
        parts.append("Current property counts by location: " + "; ".join(loc_counts))

    # Top news headlines
    top_news = context.get("top_news") or []
    if top_news:
        headlines = [n.get("title", "Untitled")[:80] for n in top_news]
        parts.append("Top news headlines: " + " | ".join(headlines))

    return "\n\n".join(parts)


def generate_executive_summary(context: Dict[str, Any]) -> Optional[str]:
    """
    Return strategic takeaways as bullet points for the competitor dossier "At a glance" block.
    Returns None if OPENAI_API_KEY is unset or the API call fails.
    """
    from .config import settings
    if not settings.openai_api_key:
        return None

    try:
        from openai import OpenAI
    except ImportError:
        return None

    client = OpenAI(api_key=settings.openai_api_key)
    context_text = _build_context_text(context)
    competitor_name = context.get("competitor", {}).get("name", "Competitor")

    system = """You are an executive briefing analyst. Given factual data about a competitor, write strategic takeaways for a leadership reader. Output only a short bullet list (3–6 bullets). Each bullet should be one clear, actionable takeaway based on the data.

Include takeaways that draw from:
- Talent: hiring focus (e.g. partnerships, engineering), senior roles, or stability ("No notable change on talent; job count stable").
- Assets: where they added or removed properties, standout markets, or footprint changes.
- News: only if there is notable press; otherwise omit.

Be specific (numbers, locations) when the data provides them. Tone: calm and executive. Format: each line starting with a bullet (use "- "). No intro sentence, no subheadings."""

    user = f"Competitor: {competitor_name}\n\nData:\n{context_text}"

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=400,
            temperature=0.3,
        )
        choice = resp.choices[0] if resp.choices else None
        if choice and choice.message and choice.message.content:
            return choice.message.content.strip()
    except Exception:
        pass
    return None


def clean_location_display_for_dossier(
    competitor_name: str,
    properties_by_location: List[Dict[str, Any]],
    asset_delta_by_city: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """
    Use the LLM to reorganize location labels into "State - City" (or "State") and group
    non-geographic labels (e.g. Career Site, Unspecified) as "Other". Returns
    {"properties_by_location": [...], "asset_delta_by_city": [...]} or None if no key or API fails.
    """
    from .config import settings
    if not settings.openai_api_key:
        return None

    try:
        from openai import OpenAI
    except ImportError:
        return None

    counts_text = "; ".join(f"{r['location']}: {r['count']}" for r in properties_by_location[:25])
    deltas_text = "; ".join(f"{r['location']}: +{r['added']}/−{r['removed']}" for r in asset_delta_by_city[:25])
    if not counts_text and not deltas_text:
        return None

    system = """You are organizing property location data for a real estate/hospitality competitor dashboard.
Given raw location labels and counts (some labels are noise like "Career Site", "Unspecified", "Privacy Policy"),
produce a cleaned list where:
1. Only real US geographic locations are kept, formatted as "State - City" (e.g. "Texas - Austin") or just "State" (e.g. "Texas") when city is not known.
2. Merge any non-location or unclear entries (Unspecified, Career Site, Cdn Cgi, Hotels, Privacy Policy, etc.) into a single row labeled "Other".
3. Preserve the exact counts and added/removed numbers; only change the location labels and grouping.
Return JSON only, no markdown: {"properties_by_location": [{"location": "...", "count": n}, ...], "asset_delta_by_city": [{"location": "...", "added": a, "removed": r}, ...]}.
If there are no real locations, still return the structure with "Other" and the totals."""

    user = f"Competitor: {competitor_name}\n\nCurrent counts by location (raw):\n{counts_text or 'none'}\n\nChanges by location (raw):\n{deltas_text or 'none'}"

    try:
        client = OpenAI(api_key=settings.openai_api_key)
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            max_tokens=800,
            temperature=0.1,
        )
        content = (resp.choices[0].message.content or "").strip()
        if content.startswith("```"):
            content = re.sub(r"^```\w*\n?", "", content).rstrip("`\n")
        data = json.loads(content)
        counts = data.get("properties_by_location")
        deltas = data.get("asset_delta_by_city")
        if isinstance(counts, list) and isinstance(deltas, list):
            return {"properties_by_location": counts, "asset_delta_by_city": deltas}
    except Exception:
        pass
    return None
