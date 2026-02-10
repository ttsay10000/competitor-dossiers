"""
Generate a short, executive-level AI summary of competitor activity for the top of the dossier.
Uses OpenAI when OPENAI_API_KEY is set; otherwise returns None so the UI can show a fallback.
Also provides LLM-cleaned location display (State - City) for properties by location.
"""
import json
import re
from typing import Any, Dict, List, Optional

# Hard caps so exec summary stays one fast LLM call; can relax after baseline redo.
MAX_LOCATION_ROWS_FOR_SUMMARY = 25
MAX_EVENTS_FOR_SUMMARY = 8
MAX_NEWS_FOR_SUMMARY = 5
MAX_DELTA_BY_CITY_ROWS = 10

# US state full names for state-level aggregation (no LLM).
_US_STATES = frozenset({
    "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado", "Connecticut",
    "Delaware", "Florida", "Georgia", "Hawaii", "Idaho", "Illinois", "Indiana", "Iowa",
    "Kansas", "Kentucky", "Louisiana", "Maine", "Maryland", "Massachusetts", "Michigan",
    "Minnesota", "Mississippi", "Missouri", "Montana", "Nebraska", "Nevada", "New Hampshire",
    "New Jersey", "New Mexico", "New York", "North Carolina", "North Dakota", "Ohio",
    "Oklahoma", "Oregon", "Pennsylvania", "Rhode Island", "South Carolina", "South Dakota",
    "Tennessee", "Texas", "Utah", "Vermont", "Virginia", "Washington", "Washington DC",
    "West Virginia", "Wisconsin", "Wyoming",
})


def _location_label_to_state(loc: str) -> str:
    """Map a location label (e.g. 'California - Palm Springs' or 'Texas') to state for aggregation."""
    s = (loc or "").strip()
    if not s:
        return "Other"
    if s in _US_STATES or s == "Other":
        return s
    if " - " in s:
        part = s.split(" - ", 1)[0].strip()
        if part in _US_STATES:
            return part
    return "Other"


def _aggregate_properties_by_state(properties_by_location: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Aggregate location rows to state-level (location = state name, count/keys summed). One row per state."""
    by_state: Dict[str, Dict[str, Any]] = {}
    for r in properties_by_location:
        loc = r.get("location", "")
        state = _location_label_to_state(loc)
        count = int(r.get("count") or 0)
        keys = int(r.get("keys") or 0)
        if state not in by_state:
            by_state[state] = {"location": state, "count": 0, "keys": 0}
        by_state[state]["count"] += count
        by_state[state]["keys"] += keys
    return sorted(by_state.values(), key=lambda x: (-x["count"], x["location"]))


def _build_context_text(context: Dict[str, Any]) -> str:
    """Turn dossier context into a concise text block for the LLM."""
    parts = []
    name = context.get("competitor", {}).get("name", "Competitor")

    # Comparison baseline: only changes/news *after* this date should be summarized.
    comparison_baseline = context.get("comparison_baseline_date")
    if comparison_baseline:
        parts.append(
            f"Comparison baseline date: {comparison_baseline}. "
            "The data below is already restricted to post-baseline: 'Events this week' and 'Top news headlines' "
            "are only items detected or added AFTER this date; 'Properties vs baseline' and location add/removal "
            "counts are deltas versus the baseline. Your summary must ONLY synthesize these post-baseline "
            "items. Do not report current snapshot totals (e.g. total roles, total properties) as new "
            "information—only mention counts when describing a change since baseline (e.g. 'X added in Y')."
        )

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

    # Events this week (titles only) — capped for exec summary speed
    events_week = context.get("events_this_week") or []
    if events_week:
        event_titles = [e.get("title", "") for e in events_week[:MAX_EVENTS_FOR_SUMMARY] if e.get("title")]
        parts.append("Events detected this week: " + "; ".join(event_titles))
    else:
        parts.append("Events this week: none.")

    # Assets: baseline delta and by location — state-level topline only, hard caps
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
        loc_changes = [f"{r['location']}: +{r['added']}/−{r['removed']}" for r in delta_by_city[:MAX_DELTA_BY_CITY_ROWS]]
        parts.append("By location (adds/removals): " + "; ".join(loc_changes))
    if props_by_loc:
        # State-level aggregation so LLM gets topline (location = state, counts) in one call
        state_rows = _aggregate_properties_by_state(props_by_loc)
        loc_lines = []
        for r in state_rows[:MAX_LOCATION_ROWS_FOR_SUMMARY]:
            loc, count, keys = r.get("location", ""), r.get("count", 0), r.get("keys", 0)
            if keys and keys > 0:
                loc_lines.append(f"{loc}: {count} properties ({keys} keys)")
            else:
                loc_lines.append(f"{loc}: {count} properties")
        parts.append("Current properties by location (state-level topline; use this format in output): " + "; ".join(loc_lines))

    # Top news headlines — capped
    top_news = context.get("top_news") or []
    if top_news:
        lines = []
        for n in top_news[:MAX_NEWS_FOR_SUMMARY]:
            title = (n.get("title") or "Untitled")[:80]
            date_str = n.get("date")
            lines.append(f"({date_str}) {title}" if date_str else title)
        parts.append("Top news headlines: " + " | ".join(lines))

    return "\n\n".join(parts)


def generate_executive_summary(context: Dict[str, Any]) -> Optional[str]:
    """
    Return an executive summary as bullet points for the competitor dossier "Executive summary" block,
    focused on what has changed recently and what is happening now.
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

    system = """You are an executive briefing analyst. You must ONLY output bullets that compare against the baseline (seed run)—i.e. changes or new items since that date. Do not summarize all data you see; only synthesize post-baseline signals.

Rules:
- The data you receive is already filtered: "Events this week" and "Top news headlines" are only items detected or added AFTER the comparison baseline. "Properties vs baseline" and location add/removal counts are deltas versus baseline. Use only these when writing bullets.
- Do NOT output bullets that merely describe current state (e.g. "Company has 50 open roles" or "They operate in 10 states") unless you are describing a *change* since baseline (e.g. "5 new Engineering roles since baseline" or "Entered Texas since baseline") supported by the events or deltas provided.
- If there are no post-baseline events and no post-baseline news and no meaningful asset deltas, output a single bullet such as "No material change since baseline."
- Output only a short bullet list (3–6 bullets, or 1 if no change). Each bullet = one clear takeaway about a change or new development since baseline.

Prioritize: talent changes (from events), asset/market adds or removals (from deltas), partnerships/funding/strategy (from events or news), and new press (from Top news). Use location counts only to explain a change (e.g. "Texas – 3 properties (80 keys) added since baseline"), not as standalone facts.

Format: each line starting with "- ". No sub-bullets, no intro sentence, no subheadings. Tone: calm and executive."""

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
            text = choice.message.content.strip()
            return _strip_subbullets(text)
    except Exception:
        pass
    return None


def _strip_subbullets(text: str) -> str:
    """Keep only top-level bullets (lines starting with '- ' or '* ' at column 0). Remove sub-bullets and indented lines."""
    lines = []
    for line in text.splitlines():
        s = line.rstrip()
        if not s:
            continue
        # Sub-bullet: indented then bullet (e.g. "  - " or "    * ")
        if len(s) > 2 and s[0] in " \t" and (s.lstrip().startswith("- ") or s.lstrip().startswith("* ")):
            continue
        lines.append(s)
    return "\n".join(lines)


def clean_location_display_for_dossier(
    competitor_name: str,
    properties_by_location: List[Dict[str, Any]],
    asset_delta_by_city: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """
    Use the LLM to reorganize location labels into state-only format and group anything
    not clearly in a specific state as "Other". Returns
    {"properties_by_location": [...], "asset_delta_by_city": [...]} or None if no key or API fails.
    """
    from .config import settings
    if not settings.openai_api_key:
        return None

    try:
        from openai import OpenAI
    except ImportError:
        return None

    # Include keys per location when present (e.g. "California: 5 (100 keys)")
    def _loc_count_keys(r: Dict[str, Any]) -> str:
        loc, count = r.get("location", ""), r.get("count", 0)
        keys = r.get("keys", 0)
        if keys and keys > 0:
            return f"{loc}: {count} ({keys} keys)"
        return f"{loc}: {count}"

    # Send enough rows so we don't truncate state diversity (was 25; 80 covers 50 states + Other + buffer).
    max_location_rows = 80
    raw_total = sum(r.get("count", 0) for r in properties_by_location)
    counts_text = "; ".join(_loc_count_keys(r) for r in properties_by_location[:max_location_rows])
    deltas_text = "; ".join(f"{r['location']}: +{r['added']}/−{r['removed']}" for r in asset_delta_by_city[:25])
    if not counts_text and not deltas_text:
        return None

    system = """You are organizing property location data for a real estate/hospitality competitor dashboard. Summarize by state only.
Given raw location labels with counts and optional keys (e.g. "California: 5 (100 keys)" or "Texas - Austin: 3"),
produce a cleaned list where:
1. Only real US states are kept. Use state name only (e.g. "Texas", "California")—no city. If the raw label is "State - City", collapse to the state only and merge counts/keys for that state.
2. Anything that does not neatly fit in a specific US state (Unspecified, Career Site, Cdn Cgi, Hotels, Privacy Policy, unclear) goes into a single row labeled "Other". Sum the counts and keys when merging into Other.
3. Preserve exact counts and keys; only change the location labels and grouping to state-only.
Return JSON only, no markdown: {"properties_by_location": [{"location": "...", "count": n, "keys": k}, ...], "asset_delta_by_city": [{"location": "...", "added": a, "removed": r}, ...]}.
Each properties_by_location entry must include "keys" (number, 0 if not provided). If there are no real states, still return the structure with "Other" and the totals. Include every state that appears in the raw list; do not drop or merge state rows into Other."""

    user = f"Competitor: {competitor_name}\n\nCurrent counts by location (raw):\n{counts_text or 'none'}\n\nChanges by location (raw):\n{deltas_text or 'none'}"

    try:
        client = OpenAI(api_key=settings.openai_api_key)
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            max_tokens=2000,
            temperature=0.1,
        )
        content = (resp.choices[0].message.content or "").strip()
        if content.startswith("```"):
            content = re.sub(r"^```\w*\n?", "", content).rstrip("`\n")
        data = json.loads(content)
        counts = data.get("properties_by_location")
        deltas = data.get("asset_delta_by_city")
        if isinstance(counts, list) and isinstance(deltas, list):
            # Ensure each row has "count" and "keys" as int (LLM may return strings)
            def _int(v: Any, default: int = 0) -> int:
                if isinstance(v, (int, float)):
                    return int(v)
                if isinstance(v, str) and v.strip().isdigit():
                    return int(v.strip())
                return default

            counts = [
                {
                    "location": r.get("location", ""),
                    "count": _int(r.get("count"), 0),
                    "keys": _int(r.get("keys"), 0),
                }
                for r in counts
                if isinstance(r, dict) and r.get("location") is not None
            ]
            cleaned_total = sum(r.get("count", 0) for r in counts)
            # If LLM collapsed everything into a single "Other" or returned far fewer properties than raw, keep raw breakdown.
            only_other = len(counts) == 1 and (counts[0].get("location") or "").strip() == "Other"
            if only_other or (raw_total > 0 and cleaned_total < 0.5 * raw_total):
                return None
            return {"properties_by_location": counts, "asset_delta_by_city": deltas}
    except Exception:
        pass
    return None
