"""
Generate a short, executive-level AI summary of competitor activity for the top of the dossier.
Uses OpenAI when OPENAI_API_KEY is set; otherwise returns None so the UI can show a fallback.
Also provides LLM-cleaned location display (State - City) for properties by location.
"""
import json
import re
from typing import Any, Dict, List, Optional

from .diff.asset_diff import resolve_destination_slug_to_state


def _extract_first_json_object(text: str) -> Optional[Dict[str, Any]]:
    """If the LLM returns JSON followed by extra text, parse only the first complete object."""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def _extract_properties_by_location_array(text: str) -> Optional[List[Dict[str, Any]]]:
    """When full JSON parse fails (e.g. truncated or missing comma), try to extract the array or parse complete entries."""
    key = '"properties_by_location"'
    i = text.find(key)
    if i < 0:
        return None
    j = text.find("[", i)
    if j < 0:
        return None
    depth = 1
    k = j + 1
    while k < len(text) and depth > 0:
        if text[k] == "[":
            depth += 1
        elif text[k] == "]":
            depth -= 1
        k += 1
    if depth == 0:
        try:
            arr = json.loads(text[j:k])
            return arr if isinstance(arr, list) else None
        except json.JSONDecodeError:
            pass
    # Truncated or malformed: try to parse each complete {"location":"...","count":N,"keys":K} entry, then merge by state.
    by_loc: Dict[str, Dict[str, Any]] = {}
    rest = text[j + 1:]
    pattern = re.compile(
        r'\{\s*"location"\s*:\s*"([^"]*)"\s*,\s*"count"\s*:\s*(\d+)\s*,\s*"keys"\s*:\s*(\d+)\s*\}'
    )
    for m in pattern.finditer(rest):
        loc, cnt, keys = m.group(1), int(m.group(2)), int(m.group(3))
        if loc not in by_loc:
            by_loc[loc] = {"location": loc, "count": 0, "keys": 0}
        by_loc[loc]["count"] += cnt
        by_loc[loc]["keys"] += keys
    arr = list(by_loc.values())
    return arr if arr else None

# Hard caps so exec summary stays one fast LLM call; can relax after baseline redo.
MAX_LOCATION_ROWS_FOR_SUMMARY = 25
MAX_EVENTS_FOR_SUMMARY = 8
MAX_NEWS_FOR_SUMMARY = 15  # Include more press to surface new markets, expansion coverage
MAX_DELTA_BY_CITY_ROWS = 15

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


def _location_to_state_extended(loc: str) -> str:
    """
    Map location label to state for aggregation. Handles state names, "State - City", and
    AvantStay-style destination labels (e.g. "Newport Beach" -> newport-beach -> California).
    """
    state = _location_label_to_state(loc)
    if state != "Other":
        return state
    # Try destination-slug form (e.g. "Newport Beach" -> "newport-beach") so AvantStay-style
    # rows get merged by state in code instead of relying on the LLM.
    slug = (loc or "").lower().replace(" ", "-").strip()
    if slug:
        resolved = resolve_destination_slug_to_state(slug)
        if resolved:
            return resolved
    return "Other"


def aggregate_state_and_state_city_rows(
    properties_by_location: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Merge rows into one per state (count and keys summed). Recognizes: state names, "State - City"
    (Lark/Placemakr), and destination labels that resolve to a state (AvantStay slugs, e.g. Newport Beach -> CA).
    Rows that still map to Other are left as-is for the LLM to map.
    """
    by_state: Dict[str, Dict[str, Any]] = {}
    other_rows: List[Dict[str, Any]] = []
    for r in properties_by_location:
        loc = (r.get("location") or "").strip()
        state = _location_to_state_extended(loc)
        count = int(r.get("count") or 0)
        keys = int(r.get("keys") or 0)
        if state != "Other":
            if state not in by_state:
                by_state[state] = {"location": state, "count": 0, "keys": 0}
            by_state[state]["count"] += count
            by_state[state]["keys"] += keys
        else:
            other_rows.append({"location": loc, "count": count, "keys": keys})
    state_rows = sorted(by_state.values(), key=lambda x: (-x["count"], x["location"]))
    return state_rows + other_rows


def _build_context_text(context: Dict[str, Any]) -> str:
    """Turn dossier context into a concise text block for the LLM."""
    parts = []
    name = context.get("competitor", {}).get("name", "Competitor")

    # Comparison baseline: only changes/news *after* this date should be summarized.
    comparison_baseline = context.get("comparison_baseline_date")
    if comparison_baseline:
        parts.append(
            f"Comparison baseline date: {comparison_baseline}. "
            "All data below is post-baseline: property/role deltas and news are changes or additions since baseline."
        )

    # 1. New properties: count + areas (from delta_by_city where added > 0)
    added = context.get("asset_added_since_baseline") or 0
    removed = context.get("asset_removed_since_baseline") or 0
    delta_by_city = context.get("asset_delta_by_city") or []
    added_areas = [f"{r['location']}: {r['added']}" for r in delta_by_city if r.get("added", 0) > 0]
    removed_areas = [f"{r['location']}: {r['removed']}" for r in delta_by_city if r.get("removed", 0) > 0]
    if added > 0:
        n_areas = len(added_areas) or 1
        parts.append(f"New properties: {added} properties in {n_areas} areas — " + "; ".join(added_areas[:MAX_DELTA_BY_CITY_ROWS]))
    else:
        parts.append("New properties: 0.")
    if removed > 0:
        n_areas = len(removed_areas) or 1
        parts.append(f"Removed properties: {removed} properties in {n_areas} areas — " + "; ".join(removed_areas[:MAX_DELTA_BY_CITY_ROWS]))
    else:
        parts.append("Removed properties: 0.")

    # 2. New roles posted / roles removed + current role mix (for significance)
    jobs_added = context.get("jobs_added_since_baseline") or 0
    jobs_removed = context.get("jobs_removed_since_baseline") or 0
    parts.append(f"New roles posted since baseline: {jobs_added}.")
    parts.append(f"Roles removed since baseline: {jobs_removed}.")
    jobs_bf = context.get("jobs_by_function") or []
    jobs_prop = context.get("jobs_by_function_property") or []
    if jobs_bf or jobs_prop:
        role_parts = []
        for row in (jobs_bf + jobs_prop)[:12]:
            fn = row.get("function") or "Other"
            total = row.get("total") or 0
            senior = row.get("senior") or 0
            s = f"{fn}: {total}" + (f" ({senior} senior)" if senior else "")
            role_parts.append(s)
        if role_parts:
            parts.append("Current open roles by function (use to interpret significance of new/removed counts): " + "; ".join(role_parts))

    # 3. Recent news: feed top_news + press_90d so LLM sees full picture (new markets, expansion coverage)
    top_news = context.get("top_news") or []
    press_90d = context.get("press_90d") or []
    news_pool = top_news if top_news else press_90d[:MAX_NEWS_FOR_SUMMARY]
    if news_pool:
        lines = []
        for n in news_pool[:MAX_NEWS_FOR_SUMMARY]:
            title = (n.get("bullet") or n.get("title") or n.get("display_title") or "Untitled")[:100]
            date_str = n.get("date")
            group = n.get("group_title")
            lines.append(f"({date_str}) {title}" + (f" [{group}]" if group else ""))
        parts.append("Recent news (derive bullets from these): " + " | ".join(lines))
    else:
        parts.append("Recent news: none.")

    # Events this week (additional signal)
    events_week = context.get("events_this_week") or []
    if events_week:
        event_titles = [e.get("title", "") for e in events_week[:MAX_EVENTS_FOR_SUMMARY] if e.get("title")]
        parts.append("Events detected this week: " + "; ".join(event_titles))

    return "\n\n".join(parts)


def generate_executive_summary(context: Dict[str, Any]) -> Optional[str]:
    """
    Return an executive summary as bullet points for the competitor dossier "Executive summary" block,
    focused on what has changed recently and what is happening now.
    Returns None if OPENAI_API_KEY is unset or the API call fails.
    """
    from .config import get_openai_client
    client = get_openai_client()
    if not client:
        return None
    context_text = _build_context_text(context)
    competitor_name = context.get("competitor", {}).get("name", "Competitor")

    system = """You are an executive briefing analyst. Output a structured summary with clear sections and spacing. Interpret the significance of hiring and job changes, not just counts.

OUTPUT FORMAT (follow this structure exactly; use a blank line between each numbered section for readability):

1. New properties: XX properties in YY areas — list the areas (e.g. California; Texas; Florida). If 0, say "0 properties."

2. Removed properties: XX properties in YY areas — list the areas. If 0, say "0 properties."

3. Talent / hiring: State the counts (new roles posted, roles removed). Then in 1–2 sentences explain what it likely means: e.g. net growth in headcount, focus areas (engineering vs property ops), senior vs junior mix, or possible restructuring if many removals. Use the "Current open roles by function" data when provided to interpret where they are hiring (e.g. "Heavy hiring in Engineering and Sales suggests product scaling and go-to-market push").

4. Recent news: Derive 1–3 bullet points from the "Recent news" items. Focus on expansion, new markets, partnerships, funding, strategy, executive appointments. If no news, say "No notable press since baseline."

Then add a blank line, then this exact section header on its own line:
Key takeaways

Under "Key takeaways", list 2–4 short bullet points (each starting with "- ") that an executive would care about most: strategic shifts, risks, opportunities, or recommended follow-ups.

Then add one more blank line and a final summary paragraph (2–4 sentences): bottom-line meaning, any new strategic shifts, and what to watch. Tone: calm and executive.

Rules:
- Use the exact numbers and areas from the data provided. Do not invent counts.
- For talent, always comment on significance (what the new/removed role counts imply), not just repeat the numbers.
- Format: each bullet with "- ". No sub-bullets. Use blank lines between sections. The line "Key takeaways" must appear exactly as written (no bold/asterisks in your output)."""

    user = f"Competitor: {competitor_name}\n\nData:\n{context_text}"

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=900,
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
    """Keep only top-level bullets (lines starting with '- ' or '* ' at column 0). Remove sub-bullets; preserve blank lines for spacing."""
    lines = []
    for line in text.splitlines():
        s = line.rstrip()
        if not s:
            lines.append("")  # preserve blank lines for section spacing
            continue
        # Sub-bullet: indented then bullet (e.g. "  - " or "    * ")
        if len(s) > 2 and s[0] in " \t" and (s.lstrip().startswith("- ") or s.lstrip().startswith("* ")):
            continue
        lines.append(s)
    return "\n".join(lines)


def format_executive_summary_for_display(text: Optional[str]) -> Optional[str]:
    """
    Escape summary text and bold the standalone "Key takeaways" line for HTML display.
    Returns None if text is None; otherwise returns HTML-safe string with that one line as <strong>.
    """
    if not text or not isinstance(text, str):
        return text
    import html
    out = []
    for line in text.splitlines():
        if line.strip() == "Key takeaways":
            out.append("<strong>Key takeaways</strong>")
        else:
            out.append(html.escape(line))
    return "\n".join(out)


def clean_location_display_for_dossier(
    competitor_name: str,
    properties_by_location: List[Dict[str, Any]],
    asset_delta_by_city: List[Dict[str, Any]],
    *,
    other_sub_bullets_text: Optional[str] = None,
    debug_return_parsed: bool = False,
) -> Optional[Dict[str, Any]]:
    """
    Use the LLM to bucket the existing list (location - count) by state. Input is only the
    list text and optional Other sub-bullets (URLs with path hints like temecula, central-oregon);
    no per-property data. Returns state-only properties_by_location and asset_delta_by_city.
    """
    from .config import get_openai_client
    client = get_openai_client()
    if not client:
        if debug_return_parsed:
            return {"_rejected": True, "_reason": "no_openai_api_key", "properties_by_location": [], "asset_delta_by_city": [], "cleaned_total": 0, "location_totals_match": False}
        return None

    # Include keys per location when present (e.g. "California: 5 (100 keys)")
    def _loc_count_keys(r: Dict[str, Any]) -> str:
        loc, count = r.get("location", ""), r.get("count", 0)
        keys = r.get("keys", 0)
        if keys and keys > 0:
            return f"{loc}: {count} ({keys} keys)"
        return f"{loc}: {count}"

    # Send the full summarized list (location bullets with count and keys) so the LLM can map every row.
    # Cap at 2000 rows to stay within context; typically this is the full list.
    max_location_rows = 2000
    rows_sent = properties_by_location[:max_location_rows]
    raw_total = sum(r.get("count", 0) for r in rows_sent)
    raw_keys_total = sum(r.get("keys", 0) for r in rows_sent)
    counts_text = "; ".join(_loc_count_keys(r) for r in rows_sent)
    deltas_text = "; ".join(f"{r['location']}: +{r['added']}/−{r['removed']}" for r in asset_delta_by_city[:25])
    if not counts_text and not deltas_text and not other_sub_bullets_text:
        return None

    system = """You are organizing property location data for a real estate/hospitality competitor dashboard.

INPUT: You receive the full summarized list of "Current counts by location (raw)" — each bullet is "Location: N" or "Location: N (K keys)". Map as many as you can to a US state; when you cannot confidently assign a location to a state, keep the original location label in your output (do not put it in Other).

OUTPUT:
- Prefer one row per US state (and at most one "Other" row for truly unclear/non-US/career/privacy).
- If you cannot select a state for a location, output a row with the same location label and the same count and keys — keep the region that was there before.
- When you merge multiple input bullets into one state row, ADD both numbers: "count" = sum of all counts you merged; "keys" = sum of all keys you merged. Property counts and key counts must both be summed when merging.

RULES:
- Map city/region names to state when you know them (e.g. Temecula, Coachella Valley, Newport Beach → California; Central Oregon, Bend → Oregon; Emerald Coast, Destin → Florida; Hudson Valley, Hamptons → New York; Poconos → Pennsylvania). Use full US state names only.
- For regions that span multiple states, pick the single state that is closest or most representative (e.g. Lake Tahoe → California; Poconos → Pennsylvania).
- Do NOT put US cities or regions into "Other". Only use "Other" for: Unspecified, career site, privacy, non-property URLs, or genuinely non-US/unclear.
- If you are unsure about a location, keep it as its own row with the original location label and its count and keys unchanged.
- Grand total of output "count" MUST equal the total property count in the user message. Sum "keys" correctly when merging.
- For asset_delta_by_city: one row per state; "added" and "removed" are the sums of deltas you merged into that state.

Return JSON only, no markdown: {"properties_by_location": [{"location": "...", "count": n, "keys": k}, ...], "asset_delta_by_city": [{"location": "...", "added": a, "removed": r}, ...]}.
Include "keys" (number, 0 if not provided) on every properties_by_location row. List states first, then any unchanged region labels, then "Other" last if needed."""

    num_bullets = len(rows_sent)
    user = f"Competitor: {competitor_name}\n\nTotal property count (your output counts MUST sum to this): {raw_total}\nTotal keys (sum of keys when merging): {raw_keys_total}\nThere are {num_bullets} location bullets below; assign every one to a state (or keep the original location if unsure) and ensure the sum of your output counts equals {raw_total}. When merging rows into one state, add both count and keys.\n\nCurrent counts by location (raw):\n{counts_text or 'none'}\n\nChanges by location (raw):\n{deltas_text or 'none'}"
    if other_sub_bullets_text and other_sub_bullets_text.strip():
        user += f"\n\nOther sub-bullets (use URL path to assign state when possible, then merge counts):\n{other_sub_bullets_text.strip()}"

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            max_tokens=4096,
            temperature=0.1,
        )
        content = (resp.choices[0].message.content or "").strip()
        if content.startswith("```"):
            content = re.sub(r"^```\w*\n?", "", content).rstrip("`\n")
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            data = _extract_first_json_object(content)
            if data is None:
                arr = _extract_properties_by_location_array(content)
                if arr is not None:
                    data = {"properties_by_location": arr, "asset_delta_by_city": []}
        if data is None:
            if debug_return_parsed:
                return {"_rejected": True, "_reason": "json_decode_or_no_object", "properties_by_location": [], "asset_delta_by_city": [], "cleaned_total": 0, "location_totals_match": False, "_raw_content_preview": content[:500] if content else ""}
            return None
        counts = data.get("properties_by_location")
        deltas = data.get("asset_delta_by_city")
        if not (isinstance(counts, list) and isinstance(deltas, list)):
            if debug_return_parsed:
                return {
                    "_rejected": True,
                    "_reason": "response_shape",
                    "properties_by_location": [],
                    "asset_delta_by_city": [],
                    "cleaned_total": 0,
                    "location_totals_match": False,
                    "_raw_content_preview": content[:500] if content else "",
                }
            return None
        # Ensure each row has "count" and "keys" as int (LLM may return strings)
        def _int(v: Any, default: int = 0) -> int:
            if isinstance(v, (int, float)):
                return int(v)
            if isinstance(v, str) and v.strip().isdigit():
                return int(v.strip())
            return default

        counts = [
            {
                "location": (r.get("location") or "").strip(),
                "count": _int(r.get("count"), 0),
                "keys": _int(r.get("keys"), 0),
            }
            for r in counts
            if isinstance(r, dict) and r.get("location") is not None
        ]
        # Sum by exact location label from LLM (same label may appear multiple times). Keep original
        # region labels when LLM could not map to a state — do not force them to "Other".
        by_loc: Dict[str, Dict[str, Any]] = {}
        for r in counts:
            loc = (r.get("location") or "").strip()
            if not loc:
                continue
            cnt = r.get("count", 0)
            keys = r.get("keys", 0)
            if loc not in by_loc:
                by_loc[loc] = {"location": loc, "count": 0, "keys": 0}
            by_loc[loc]["count"] += cnt
            by_loc[loc]["keys"] += keys
        # Sort: US states first (by count desc), then "Other", then any other labels (unchanged regions).
        def _sort_key(x: Dict[str, Any]) -> tuple:
            loc = (x.get("location") or "").strip()
            if loc in _US_STATES:
                return (0, -x.get("count", 0), loc)
            if loc == "Other":
                return (1, -x.get("count", 0), loc)
            return (2, -x.get("count", 0), loc)
        counts = sorted(by_loc.values(), key=_sort_key)
        cleaned_total = sum(r.get("count", 0) for r in counts)
        location_totals_match = raw_total == cleaned_total
        # If LLM collapsed everything into a single "Other" or returned far fewer properties than raw, keep raw breakdown.
        only_other = len(counts) == 1 and (counts[0].get("location") or "").strip() == "Other"
        if only_other or (raw_total > 0 and cleaned_total < 0.5 * raw_total):
            if debug_return_parsed:
                return {"_rejected": True, "_reason": "only_other_or_under_half", "properties_by_location": counts, "asset_delta_by_city": deltas, "cleaned_total": cleaned_total, "location_totals_match": location_totals_match}
            return None
        # Do not reject when totals don't match: use the state summary and set location_totals_match=False
        # so the UI can show the breakdown plus a note that totals may not sum.
        # If raw input had state names but LLM returned only a single "Other" row, reject (useless).
        raw_has_state = any(
            (r.get("location") or "").strip() in _US_STATES
            for r in rows_sent
        )
        only_other_row = len(counts) == 1 and (counts[0].get("location") or "").strip() == "Other"
        if raw_has_state and only_other_row:
            if debug_return_parsed:
                return {"_rejected": True, "_reason": "no_state_row_in_output", "properties_by_location": counts, "asset_delta_by_city": deltas, "cleaned_total": cleaned_total, "location_totals_match": location_totals_match}
            return None
        # Fix shortfall: if LLM total is less than raw, add the difference to Other so displayed breakdown sums to raw_total.
        if raw_total > cleaned_total:
            shortfall = raw_total - cleaned_total
            other_row = next((r for r in counts if (r.get("location") or "").strip() == "Other"), None)
            if other_row is not None:
                other_row["count"] = other_row.get("count", 0) + shortfall
            else:
                counts.append({"location": "Other", "count": shortfall, "keys": 0})
            location_totals_match = True
        return {
            "properties_by_location": counts,
            "asset_delta_by_city": deltas,
            "location_totals_match": location_totals_match,
        }
    except json.JSONDecodeError as e:
        if debug_return_parsed:
            return {"_rejected": True, "_reason": f"json_decode: {e}", "properties_by_location": [], "asset_delta_by_city": [], "cleaned_total": 0, "location_totals_match": False, "_raw_content_preview": content[:500] if content else ""}
        return None
    except Exception as e:
        if debug_return_parsed:
            return {"_rejected": True, "_reason": f"exception: {e}", "properties_by_location": [], "asset_delta_by_city": [], "cleaned_total": 0, "location_totals_match": False}
        pass
    return None
