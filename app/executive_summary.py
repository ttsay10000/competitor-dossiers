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
    from .config import get_openai_client
    client = get_openai_client()
    if not client:
        return None
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

    # Send enough rows so large portfolios (e.g. AvantStay 2300+ properties across many cities) are fully represented.
    max_location_rows = 150
    raw_total = sum(r.get("count", 0) for r in properties_by_location)
    counts_text = "; ".join(_loc_count_keys(r) for r in properties_by_location[:max_location_rows])
    deltas_text = "; ".join(f"{r['location']}: +{r['added']}/−{r['removed']}" for r in asset_delta_by_city[:25])
    if not counts_text and not deltas_text and not other_sub_bullets_text:
        return None

    system = """You are organizing property location data for a real estate/hospitality competitor dashboard.

OUTPUT RULE: The output must show ONLY US state names (and at most one "Other" row). No regions, areas, or city names may appear in the output—every "location" in your response must be a single state (e.g. "California", "Texas") or "Other".

YOUR TWO TASKS:
1. Map every input row to exactly one US state. Search each city, region, or area and assign it to the correct state. If a region spans multiple states (e.g. Lake Tahoe = CA/NV, Poconos = PA/NJ, Four Corners), pick the single state that is closest or most representative and assign the whole count to that state. Consolidate so the output has one row per state (plus at most one "Other" row).
2. When you combine multiple input bullets into one state row, ADD the numbers: the output "count" for that state must be the sum of the "count" values from every input row you assigned to that state; the output "keys" for that state must be the sum of the "keys" values from those same rows. Do not drop or invent numbers.

Input you receive:
1. "Current counts by location (raw)" — a semicolon-separated list of "Location: N" or "Location: N (K keys)". Locations may be state names, city names, or region/area names (e.g. Temecula, Newport Beach, Central Oregon, Emerald Coast, Lake Tahoe). Your job is to map every one to a single state and sum the numbers when you merge.
2. Optionally "Other sub-bullets": lines with URLs. Use the URL path (e.g. temecula, central-oregon) to infer US state and add 1 property to that state for each line (unless genuinely unclear, then "Other").

Rules:
- Every input bullet must be assigned to exactly one state (or Other). No output row may be a region or area—only state names. Examples: Temecula, Paso Robles, Lake Arrowhead, Newport Beach, Coachella Valley, Palm Springs, Joshua Tree, Lake Tahoe, Malibu, Sonoma, Big Bear, San Diego → California. Central Oregon, Bend, Sunriver, Oregon Coast → Oregon. Hudson Valley, Hamptons, Catskills, Berkshires → New York. Austin, Hill Country, South Padre Island, Corpus Christi, Port Aransas → Texas. Coastal Charleston → South Carolina. Emerald Coast 30A, Key West, Fort Lauderdale, St Augustine, Marco Island, Fort Myers, Orlando, Pensacola, Destin → Florida. Poconos → Pennsylvania. Lake Norman → North Carolina. Whidbey Island → Washington. Use full US state names only.
- For regions that span multiple states, choose the one state that is closest or most representative (e.g. Lake Tahoe → California; Poconos → Pennsylvania) and assign the full count to that state.
- Do NOT put US cities or regions into "Other". Only use "Other" for: Unspecified, career site, privacy, non-property URLs, or genuinely non-US/unclear.
- When combining bullets into one state row: output "count" = sum of counts; output "keys" = sum of keys. The grand total of all output "count" values MUST equal the total property count in the user message.
- For asset_delta_by_city: one row per state; "added" and "removed" are the sums of deltas you merged into that state.

Return JSON only, no markdown: {"properties_by_location": [{"location": "...", "count": n, "keys": k}, ...], "asset_delta_by_city": [{"location": "...", "added": a, "removed": r}, ...]}.
Each "location" in properties_by_location must be a US state name or "Other". Include "keys" (number, 0 if not provided). List states first (e.g. California, Colorado, Florida, ...), then "Other" last if needed."""

    num_bullets = len(properties_by_location[:max_location_rows])
    user = f"Competitor: {competitor_name}\n\nTotal property count (your output counts MUST sum to this): {raw_total}\nThere are {num_bullets} location bullets below; assign every one to a state and ensure the sum of your state counts equals {raw_total}.\n\nCurrent counts by location (raw):\n{counts_text or 'none'}\n\nChanges by location (raw):\n{deltas_text or 'none'}"
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
        # Normalize: if LLM returned any region/area name instead of a state, map to state and re-aggregate so output is states only.
        by_state: Dict[str, Dict[str, Any]] = {}
        for r in counts:
            loc = r.get("location", "")
            state = _location_to_state_extended(loc) if loc not in _US_STATES and (loc or "").strip() != "Other" else (loc or "").strip()
            cnt = r.get("count", 0)
            keys = r.get("keys", 0)
            if state not in by_state:
                by_state[state] = {"location": state, "count": 0, "keys": 0}
            by_state[state]["count"] += cnt
            by_state[state]["keys"] += keys
        counts = sorted(by_state.values(), key=lambda x: (1 if (x.get("location") or "").strip() == "Other" else 0, -x["count"], x["location"]))
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
            for r in properties_by_location[:max_location_rows]
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
