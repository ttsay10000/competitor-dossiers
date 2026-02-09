"""
Generate a short, executive-level AI summary of competitor activity for the top of the dossier.
Uses OpenAI when OPENAI_API_KEY is set; otherwise returns None so the UI can show a fallback.
"""
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
    Return 2–4 sentences of executive-level summary for the competitor dossier.
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

    system = """You are an executive briefing analyst. Given factual data about a competitor, write 2–4 short sentences that summarize what is happening at a high level for a leadership reader. Focus on:
- Talent: e.g. new or changed roles, which areas are growing (partnerships, engineering, etc.), or "no change on talent; job count stable."
- Assets: e.g. where they added or removed properties (states/cities), and if one location stands out (e.g. "notable push in Texas based on number of additions").
- Only mention news if there is notable press; otherwise omit.
Write in a calm, executive tone. Be specific (numbers, locations) when the data provides them. If nothing notable changed, say so clearly. Output only the summary, no preamble or bullet points."""

    user = f"Competitor: {competitor_name}\n\nData:\n{context_text}"

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=300,
            temperature=0.3,
        )
        choice = resp.choices[0] if resp.choices else None
        if choice and choice.message and choice.message.content:
            return choice.message.content.strip()
    except Exception:
        pass
    return None
