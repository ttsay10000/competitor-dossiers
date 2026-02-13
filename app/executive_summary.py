"""
Generate a short, executive-level AI summary of competitor activity for the top of the dossier.
Uses OpenAI when OPENAI_API_KEY is set; otherwise returns None so the UI can show a fallback.
Also provides LLM-cleaned location display (State - City) for properties by location.
"""
import json
import re
from typing import Any, Dict, List, Optional, Tuple

from .diff.asset_diff import resolve_destination_slug_to_state, US_STATE_ABBREV


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

# Caps for exec summary prompt size (location/news/delta); events are not capped.
MAX_LOCATION_ROWS_FOR_SUMMARY = 25
MAX_NEWS_FOR_SUMMARY = 5  # Only top 5 news items; exec summary synthesizes into 1-2 bullets, does not list articles
TOP_NEWS_DAYS = 7  # Only include news from past week in executive summary (recent updates only)
MAX_DELTA_BY_CITY_ROWS = 15
MAX_REVIEW_PROPERTIES_FOR_SUMMARY = 10  # Sample of review sentiment sent to LLM (only when significant)

# Event type priority for executive summary: precedent (0) = news, asset, talent, digital footprint;
# secondary (1) = social, reviews. Events sorted by this rank then by date (newest first).
EVENT_TYPE_PRIORITY_PRECEDENT = 0
EVENT_TYPE_PRIORITY_SECONDARY = 1
_EVENT_PRIORITY_ORDER = (
    "talent.",
    "asset.",
    "partner.",
    "capital.",
    "narrative.homepage_updated",
    "narrative.coming_soon",
    "narrative.priority_shift",
    "public_record.",
)

# US state full names (+ DC) for state-level aggregation (no LLM).
# Include both "Washington DC" and "District of Columbia" so either label groups under "Properties by states".
_US_STATES = frozenset({
    "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado", "Connecticut",
    "Delaware", "District of Columbia", "Florida", "Georgia", "Hawaii", "Idaho", "Illinois",
    "Indiana", "Iowa", "Kansas", "Kentucky", "Louisiana", "Maine", "Maryland", "Massachusetts",
    "Michigan", "Minnesota", "Mississippi", "Missouri", "Montana", "Nebraska", "Nevada",
    "New Hampshire", "New Jersey", "New Mexico", "New York", "North Carolina", "North Dakota",
    "Ohio", "Oklahoma", "Oregon", "Pennsylvania", "Rhode Island", "South Carolina", "South Dakota",
    "Tennessee", "Texas", "Utah", "Vermont", "Virginia", "Washington", "Washington DC",
    "West Virginia", "Wisconsin", "Wyoming",
})
# Sorted list for template/API (section splitting: "Properties by states" vs "Other properties").
US_STATES_LIST = sorted(_US_STATES)


def _event_priority_rank(etype: str) -> int:
    """Return 0 for precedent signals (talent, asset, news-related, digital footprint), 1 for secondary (social, reviews)."""
    if not etype:
        return EVENT_TYPE_PRIORITY_SECONDARY
    if etype == "narrative.social_signal":
        return EVENT_TYPE_PRIORITY_SECONDARY
    if etype.startswith("narrative.review") or "review" in etype.lower():
        return EVENT_TYPE_PRIORITY_SECONDARY
    for prefix in _EVENT_PRIORITY_ORDER:
        if etype == prefix or (prefix.endswith(".") and etype.startswith(prefix)):
            return EVENT_TYPE_PRIORITY_PRECEDENT
    return EVENT_TYPE_PRIORITY_PRECEDENT  # unknown type treat as precedent


def _is_significant_review(r: dict) -> bool:
    """True if this review row indicates a major or significant change (e.g. declining trend, negative sentiment)."""
    line = (r.get("line") or "").lower()
    trend = (r.get("trend") or "").lower()
    negative_markers = ("declining", "negative", "complaint", "complaints", "poor", "issue", "issues", "problem", "disappoint")
    if trend in ("declining", "negative", "down"):
        return True
    if any(m in line for m in negative_markers):
        return True
    return False


def _normalize_dc_label(loc: str) -> str:
    """If location is a DC variant (Washington D.C., District of Columbia, etc.), return 'Washington DC' for consistent merging."""
    s = (loc or "").strip()
    lower = s.lower()
    if lower in ("washington d.c.", "washington d. c.", "washington, d.c.", "district of columbia", "washington dc"):
        return "Washington DC"
    return s


def _location_label_to_state(loc: str) -> str:
    """Map a location label (e.g. 'California - Palm Springs' or 'Texas') to state for aggregation."""
    s = (loc or "").strip()
    if not s:
        return "Other"
    # Normalize DC so "Washington D.C.", "District of Columbia", and "Washington DC" merge into one row.
    dc_normalized = _normalize_dc_label(s)
    if dc_normalized != s:
        return dc_normalized
    if s == "District of Columbia":
        return "Washington DC"
    if s in _US_STATES or s == "Other":
        return s
    if " - " in s:
        part = s.split(" - ", 1)[0].strip()
        dc_part = _normalize_dc_label(part)
        if dc_part != part:
            return dc_part
        if part == "District of Columbia":
            return "Washington DC"
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
    Map location label to state for aggregation. Handles state names, "State - City",
    "City, ST" (e.g. Austin, TX), and AvantStay-style destination labels
    (e.g. "Newport Beach" -> newport-beach -> California).
    """
    state = _location_label_to_state(loc)
    if state != "Other":
        return state
    # "City, ST" format (e.g. Austin, TX, Miami, FL) -> merge into state
    s = (loc or "").strip()
    if s:
        m = re.search(r",\s*([a-zA-Z]{2})\s*$", s)
        if m:
            abbrev = m.group(1).lower()
            if abbrev in US_STATE_ABBREV:
                return US_STATE_ABBREV[abbrev]
    # Try destination-slug form (e.g. "Newport Beach" -> "newport-beach") so AvantStay-style
    # rows get merged by state in code instead of relying on the LLM.
    slug = (loc or "").lower().replace(" ", "-").replace(",", "-").strip()
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
    """
    Turn dossier context into a concise text block for the LLM.
    Executive summary uses only: (1) top news (last 2-3 weeks), (2) asset changes vs baseline
    or baseline footprint if no refresh, (3) job changes vs baseline or baseline count if no refresh,
    (4) website/digital footprint changes (events), (5) social/review updates (events + significant reviews).
    When only baseline exists (no refresh), we send baseline state; otherwise changes only.
    """
    from datetime import datetime, timedelta, timezone

    parts = []
    comparison_baseline = context.get("comparison_baseline_date")
    has_asset_refresh = context.get("has_asset_refresh_since_baseline", False)
    has_talent_refresh = context.get("has_talent_refresh_since_baseline", False)
    has_any_refresh = has_asset_refresh or has_talent_refresh

    # Baseline instruction: summarize only changes when we have a refresh; otherwise baseline is acceptable.
    if comparison_baseline:
        if has_any_refresh:
            parts.append(
                f"Comparison baseline date: {comparison_baseline}. "
                "Summarize only CHANGES since baseline (property/job deltas, new events). Do not restate baseline state."
            )
        else:
            parts.append(
                f"Comparison baseline date: {comparison_baseline}. "
                "Only baseline data available (no refresh since). Summarize current state where applicable."
            )

    # Detect "no changes" case: refresh happened but no property/job/event deltas.
    events_week = context.get("events_this_week") or []
    asset_added = context.get("asset_added_since_baseline") or 0
    asset_removed = context.get("asset_removed_since_baseline") or 0
    jobs_added = context.get("jobs_added_since_baseline") or 0
    jobs_removed = context.get("jobs_removed_since_baseline") or 0
    no_changes = (
        has_any_refresh
        and asset_added == 0
        and asset_removed == 0
        and jobs_added == 0
        and jobs_removed == 0
        and not events_week
    )
    if no_changes:
        parts.append(
            "No changes since last refresh: properties, jobs, and signals unchanged. "
            "Include this in your summary (e.g. a bullet noting 'No changes to properties, jobs, or signals since last refresh')."
        )

    # 1. Asset: changes vs baseline (added/removed per state) when we have a refresh; else baseline footprint only.
    if has_asset_refresh:
        added = context.get("asset_added_since_baseline") or 0
        removed = context.get("asset_removed_since_baseline") or 0
        delta_by_city = context.get("asset_delta_by_city") or []
        added_areas = [f"{r['location']}: {r['added']}" for r in delta_by_city if r.get("added", 0) > 0]
        removed_areas = [f"{r['location']}: {r['removed']}" for r in delta_by_city if r.get("removed", 0) > 0]
        if added > 0:
            n_areas = len(added_areas) or 1
            parts.append(f"Property changes since baseline — New: {added} in {n_areas} areas: " + "; ".join(added_areas[:MAX_DELTA_BY_CITY_ROWS]))
        else:
            parts.append("Property changes since baseline — New: 0.")
        if removed > 0:
            n_areas = len(removed_areas) or 1
            parts.append(f"Property changes since baseline — Removed: {removed} in {n_areas} areas: " + "; ".join(removed_areas[:MAX_DELTA_BY_CITY_ROWS]))
        else:
            parts.append("Property changes since baseline — Removed: 0.")
    else:
        total_properties = context.get("total_properties") or 0
        properties_by_location = context.get("properties_by_location") or []
        if total_properties > 0 and properties_by_location:
            loc_summary = "; ".join(f"{r.get('location', '')}: {r.get('count', 0)}" for r in properties_by_location[:MAX_DELTA_BY_CITY_ROWS])
            parts.append(f"Property footprint (baseline; no refresh yet): {total_properties} properties — {loc_summary}")
        else:
            parts.append(f"Property footprint (baseline): {total_properties} properties.")

    # 2. Talent: job count changes since baseline when we have a refresh; else baseline count only.
    if has_talent_refresh:
        jobs_added = context.get("jobs_added_since_baseline") or 0
        jobs_removed = context.get("jobs_removed_since_baseline") or 0
        total_roles = len(context.get("talent_jobs") or [])
        parts.append(f"Job changes since baseline: {jobs_added} new roles posted, {jobs_removed} removed. Current open roles: {total_roles}.")
    else:
        total_roles = len(context.get("talent_jobs") or [])
        parts.append(f"Open roles (baseline; no refresh yet): {total_roles}. No job count changes.")

    # 3. Top news: only last week (top_news only; no raw press_90d).
    top_news = context.get("top_news") or []
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=TOP_NEWS_DAYS)).date()
    news_in_window = []
    for n in top_news[:MAX_NEWS_FOR_SUMMARY]:
        date_str = (n.get("date") or "").strip()[:10]
        if date_str and len(date_str) >= 10:
            try:
                d = datetime.strptime(date_str, "%Y-%m-%d").date()
                if d >= cutoff:
                    news_in_window.append(n)
            except ValueError:
                news_in_window.append(n)  # keep if unparseable
        else:
            news_in_window.append(n)  # no date: include
    news_pool = news_in_window if news_in_window else top_news[:MAX_NEWS_FOR_SUMMARY]
    if news_pool:
        lines = []
        for n in news_pool[:MAX_NEWS_FOR_SUMMARY]:
            title = (n.get("bullet") or n.get("title") or n.get("display_title") or "Untitled")[:100]
            date_str = n.get("date")
            group = n.get("group_title")
            lines.append(f"({date_str}) {title}" + (f" [{group}]" if group else ""))
        parts.append(
            "Top news (last 7 days when available; synthesize into 1–2 bullets max—do NOT list or recite each item): "
            + " | ".join(lines)
        )
    else:
        parts.append("Top news: none.")

    # 4. Review sentiment — new/significant updates (current snapshot when no review diff; include only when significant).
    reviews_minimal = context.get("reviews_minimal") or []
    significant_reviews = [r for r in reviews_minimal if _is_significant_review(r)]
    if significant_reviews:
        lines = []
        rest = [r for r in reviews_minimal if r not in significant_reviews]
        for r in (significant_reviews + rest)[:MAX_REVIEW_PROPERTIES_FOR_SUMMARY]:
            rname = (r.get("display_name") or "Property")[:50]
            line = (r.get("line") or "").strip() or "—"
            trend = r.get("trend")
            s = f"{rname}: {line}"
            if trend:
                s += f" (trend: {trend})"
            lines.append(s)
        if lines:
            parts.append("Review sentiment (significant changes / current snapshot): " + " | ".join(lines))

    # 5. Events: website changes [digital footprint], social [social], and other signals since last refresh.
    events_week = context.get("events_this_week") or []
    if events_week:
        def _event_date_key(e: dict) -> str:
            return e.get("occurred_at_str") or e.get("detected_at_str") or "0000-00-00"
        events_week = sorted(events_week, key=_event_date_key, reverse=True)
        events_week = sorted(
            events_week,
            key=lambda e: _event_priority_rank((e.get("type") or "").strip()),
        )
        event_bits = []
        for e in events_week:
            title = (e.get("title") or "").strip()
            if not title:
                continue
            etype = (e.get("type") or "").strip()
            if etype == "narrative.social_signal":
                event_bits.append(f"{title} [social]")
            elif etype and etype.startswith("narrative."):
                event_bits.append(f"{title} [website/digital footprint]")
            elif etype:
                event_bits.append(f"{title} [{etype}]")
            else:
                event_bits.append(title)
        if event_bits:
            parts.append("Signals since last refresh (website changes, social, talent/asset events): " + "; ".join(event_bits))

    return "\n\n".join(parts)


def generate_executive_summary(context: Dict[str, Any]) -> Optional[str]:
    """
    Return a competitive intelligence brief (bullet-based) for the competitor dossier "Executive summary" block.
    Filters noise, surfaces material changes, interprets what the competitor is optimizing for, and recommends action.
    Returns None if OPENAI_API_KEY is unset or the API call fails.
    """
    from .config import get_openai_client
    client = get_openai_client()
    if not client:
        return None
    context_text = _build_context_text(context)
    competitor_name = context.get("competitor", {}).get("name", "Competitor")

    system = """You are an AI Chief of Staff writing a competitive intelligence brief for Kasa's CEO and exec team. Be concise and executive-level: scannable in 30 seconds. Filter noise, cluster related updates, and translate changes into clear implications and actions.

The data you receive contains ONLY: (1) Top news from the past week (at most 5 items); (2) Asset/property changes vs baseline (or baseline footprint if no refresh); (3) Job count changes vs baseline (or baseline count if no refresh); (4) Website/digital footprint changes since last refresh; (5) Social media and review updates (new or significant). When the input says "only baseline" or "no refresh yet", summarize current state; when it says "changes since baseline", summarize only those changes. When the input says "No changes since last refresh", include a brief bullet noting this (e.g. "No changes to properties, jobs, or signals since last refresh")—this is common and worth stating explicitly.

NEWS: Do NOT list or recite each news article. Synthesize news into at most 1–2 bullets total (the single most important development). If multiple items are the same story, one bullet only.

PRIORITY: Order bullets by importance to competitive dynamics, not by section order. Put the single most important change first (e.g. major news, market entry/exit, key hire). Then the next most important. Include only changes that materially alter competitive dynamics (market entry/exit, meaningful inventory, pricing/fees, key hiring, major product/positioning, partnerships, regulatory). Drop cosmetic or one-off items. Include social/review sentiment only when it reflects major change.

If nothing has changed: output only a brief line (e.g. "No changes to properties, jobs, or signals since last refresh") and do not write a long recap.

Return in this exact structure. Do NOT start with "EXECUTIVE SUMMARY" or any top-level header—the page already has a header. Use only single newlines between bullets and sections.

• (3–5 bullets when there are changes; 1 short bullet when there are none): most important shifts first, why it matters. One line per bullet where possible; no paragraph-length bullets. Concrete and decisive.

Format: bullet character • for every list item (never dash -). No "EXECUTIVE SUMMARY" at top. Style: bullets only, no fluff, concrete language, strong verbs, ~120–200 words total when there are changes; much shorter when there are none."""

    user = f"Competitor: {competitor_name}\n\nData:\n{context_text}"

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=550,
            temperature=0.3,
        )
        choice = resp.choices[0] if resp.choices else None
        if choice and choice.message and choice.message.content:
            text = choice.message.content.strip()
            return _strip_subbullets(text)
    except Exception:
        pass
    return None


def _format_property_counts_section(property_counts: List[dict]) -> str:
    """Format property counts per competitor for the email report."""
    if not property_counts:
        return ""
    lines = ["**Property counts (latest per competitor; changes since last refresh):**", ""]
    for p in property_counts:
        name = p.get("name") or "Unknown"
        total = p.get("total") or 0
        added = p.get("added") or 0
        removed = p.get("removed") or 0
        has_refresh = p.get("has_refresh", False)
        if has_refresh and (added > 0 or removed > 0):
            delta = f" (+{added} -{removed} since last refresh)"
        elif has_refresh:
            delta = " (no change since last refresh)"
        else:
            delta = " (no refresh yet)"
        lines.append(f" - **{name}:** {total} total{delta}")
    return "\n".join(lines)


def generate_rollup_summary(
    per_competitor_summaries: List[Tuple[int, str, str]],
    *,
    all_news: Optional[List[dict]] = None,
    property_counts: Optional[List[dict]] = None,
) -> Optional[str]:
    """
    Produce a single roll-up summary from multiple competitor executive summaries.
    Returns a blob with: (1) Recent updates paragraph, (2) per-competitor bullets,
    (3) running news list with hyperlinks, (4) property counts per competitor.
    Returns None if no summaries or LLM fails.
    """
    from .config import get_openai_client
    if not per_competitor_summaries:
        return None
    client = get_openai_client()
    if not client:
        return None
    blocks = []
    for cid, name, text in per_competitor_summaries:
        if not (name and text):
            continue
        blocks.append(f"--- {name} (id={cid}) ---\n{text.strip()}")
    if not blocks:
        return None
    combined = "\n\n".join(blocks)

    news_instruction = ""
    news_block = ""
    if all_news:
        # Group by competitor; for each competitor, first article URL is the "Read more" link.
        by_competitor: Dict[str, List[dict]] = {}
        for n in all_news:
            comp = (n.get("competitor_name") or "").strip()
            if not comp:
                continue
            url = (n.get("url") or "").strip()
            if not url or not url.startswith("http"):
                continue
            if comp not in by_competitor:
                by_competitor[comp] = []
            by_competitor[comp].append(n)
        news_lines = []
        for comp, items in by_competitor.items():
            first = items[0]
            first_url = (first.get("url") or "").strip()
            titles_summaries = []
            for n in items[:8]:
                title = (n.get("title") or "Untitled").replace("\n", " ")[:120]
                summary = (n.get("one_line_summary") or "").replace("\n", " ").strip()[:200]
                date_str = (n.get("date") or "")[:10]
                titles_summaries.append(f"  {title} | {summary} | {date_str}")
            block = f"COMPETITOR: {comp}\nFIRST_ARTICLE_URL: {first_url}\n" + "\n".join(titles_summaries)
            news_lines.append(block)
        if news_lines:
            news_block = "\n\n".join(news_lines)
            news_instruction = """

3. **Running news list** — One line per competitor that has news below. Format each line as:
   - **Competitor Name** — One-line summary of their top news. [Read more](exact_URL)
   Use the exact FIRST_ARTICLE_URL given for that competitor as the link. Put the entire [Read more](url) on the same line as the summary—no line break before the URL. Output in the same order as the competitor blocks below. Skip competitors with only promotional or low-signal items."""

    system = f"""You are an AI Chief of Staff for Kasa's exec team. You are given executive summaries for several competitors (each block below is one competitor, with a header "--- Name (id=...) ---"). Pull out concrete facts to write the roll-up.

Output exactly two parts{(" (plus a third)" if news_block else "")}:

1. **Recent updates:** — A short lead paragraph (2–3 sentences) that synthesizes major news *across* competitors at executive level. Use specific market names, regions, or cities where expansion or openings are mentioned (e.g. "Austin", "Miami", "UK")—not vague language like "new markets". Surface cross-cutting themes. End with the single most important takeaway or insight for Kasa. Keep it succinct and direct.

2. **Per-competitor bullets:** — A list, one bullet per competitor, in the same order as the input blocks:
   - **[Competitor Name]:** [1–2 sentences of specific recent news only: name actual hires or roles if mentioned, named partnerships, specific cities or markets for new openings/expansion, key press or product/strategy shifts. Do NOT include "recommended action" or generic advice—only what actually happened.]
   Use the exact competitor names from the block headers (the text after "--- " and before " (id="). Do not invent or reorder competitors.{news_instruction}

Style: bullets only; no fluff; concrete language; name real markets and moves. If a competitor's summary is thin, say so briefly.

FORMATTING: Put each part on its own line with clear section headers. Use " - **Name:**" for each bullet. For the news list, one line per competitor with [Read more](exact_first_article_url) on the same line."""

    user_parts = [f"Competitor executive summaries:\n\n{combined}"]
    if news_block:
        user_parts.append(f"\n\nTop news groupings (one block per competitor; use FIRST_ARTICLE_URL for [Read more](url)):\n\n{news_block}")

    user = "\n".join(user_parts)

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=1200,
            temperature=0.3,
        )
        choice = resp.choices[0] if resp.choices else None
        if choice and choice.message and choice.message.content:
            raw = choice.message.content.strip()
            polished = polish_rollup_summary(raw)
            body = polished if polished else raw
            if polished and polished.count("\n") < 4 and " - **" in polished:
                cleaned = clean_rollup_formatting(polished)
                body = cleaned if cleaned else polished
            elif not polished:
                cleaned = clean_rollup_formatting(raw)
                body = cleaned if cleaned else raw
            if property_counts:
                props_section = _format_property_counts_section(property_counts)
                if props_section:
                    body = body.rstrip() + "\n\n" + props_section
            return body
    except Exception:
        pass
    return None


def polish_rollup_summary(text: str) -> Optional[str]:
    """
    Final LLM read-through: take the draft rollup and produce a clean version with a strong
    top-line paragraph and one strong bullet per competitor (changes since last refresh).
    Preserves a Running news list section if present (with [Read](url) links).
    Returns None if LLM unavailable or fails.
    """
    if not text or not text.strip():
        return text
    from .config import get_openai_client
    client = get_openai_client()
    if not client:
        return None
    has_news = "Running news" in text or "**Running news" in text
    news_instruction = (
        "\n3. **Running news list:** — If the input contains a Running news list section (with [Read more](url) or [Read](url) markdown links), copy it EXACTLY without modification. Keep each [Read more](url) on the same line as its summary—no line break before the URL."
        if has_news
        else ""
    )
    system = f"""You are an AI Chief of Staff for Kasa's exec team. You will receive a "Recent updates" rollup. Your job is to produce one clean, final version with stronger wording and clear "changes since last refresh" framing.

Output exactly two parts{(" (plus a third)" if has_news else "")}:

1. **Recent updates:** — A short top-line paragraph (2–3 sentences) that synthesizes what is happening across competitors, using specific market/city names where relevant, and the single most important takeaway or insight for Kasa. Be direct and executive-ready.

2. **Per-competitor bullets:** — One bullet per competitor, each on its own line. Format each line as: " - **Competitor Name:** [1–2 sentence summary of specific changes since last refresh: named hires or roles, specific partnerships, actual cities or markets for openings/expansion, key news or strategy shifts.]"
- Use the exact competitor names that appear in the input. Do not add or remove competitors; keep the same order.
- Write strong, concrete bullets: only specific facts (hires, partnerships, market entries, openings with city/market names, press, exits). Do NOT include "recommended action" or generic advice—focus on what actually happened. No fluff or generic strategy talk.{news_instruction}

FORMATTING: Put each part on its own line. Use " - **Name:**" for each bullet. Output only the recap, nothing else."""

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": text.strip()},
            ],
            max_tokens=1000,
            temperature=0.2,
        )
        choice = resp.choices[0] if resp.choices else None
        if choice and choice.message and choice.message.content:
            return choice.message.content.strip()
    except Exception:
        pass
    return None


def clean_rollup_formatting(text: str) -> Optional[str]:
    """
    Use the LLM to fix rollup formatting only: preserve all content and wording, add proper
    line breaks so "Recent updates" and "Per-competitor bullets" are separated and each
    competitor bullet is on its own line. Returns None if LLM unavailable or fails.
    """
    if not text or not text.strip():
        return text
    from .config import get_openai_client
    client = get_openai_client()
    if not client:
        return None
    system = """You are a formatting assistant. You will receive a competitive intelligence recap with two or three parts: (1) **Recent updates:** — a paragraph, (2) **Per-competitor bullets:** — a list of bullets like " - **CompetitorName:** ...", and optionally (3) **Running news list:** — bullets with [Read more](url) links.

Your task: output the EXACT same text with only formatting changes. Do not change a single word or add/remove content.

Formatting rules:
- Put "**Recent updates:**" (and its paragraph) first. End the paragraph with a single newline (no blank line after it).
- Then "**Per-competitor bullets:**" on its own line (or "2. **Per-competitor bullets:**" if numbered).
- Then each bullet on its own line: every " - **Name:** ..." must be on a separate line. Do not join multiple bullets onto one line.
- If present, keep "**Running news list:**" and each of its bullets on one line each, with [Read more](url) on the same line as the summary—no line break before the URL.

Keep all **bold** markers and all URLs. Output only the reformatted recap, nothing else."""

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": text.strip()},
            ],
            max_tokens=1000,
            temperature=0.0,
        )
        choice = resp.choices[0] if resp.choices else None
        if choice and choice.message and choice.message.content:
            return choice.message.content.strip()
    except Exception:
        pass
    return None


def _strip_subbullets(text: str) -> str:
    """Keep only top-level bullets (lines starting with '- ', '* ', or '• ' at column 0). Remove sub-bullets; preserve blank lines for spacing."""
    lines = []
    for line in text.splitlines():
        s = line.rstrip()
        if not s:
            lines.append("")  # preserve blank lines for section spacing
            continue
        # Sub-bullet: indented then bullet (e.g. "  - ", "    * ", "    • ")
        ls = s.lstrip()
        if len(ls) > 1 and s[0] in " \t" and (ls.startswith("- ") or ls.startswith("* ") or ls.startswith("• ")):
            continue
        lines.append(s)
    return "\n".join(lines)


def _normalize_rollup_line_breaks(text: str) -> str:
    """
    If the rollup is one long paragraph with " - **Name:**" bullets run together,
    split so each bullet is on its own line for display.
    """
    if not text or " - **" not in text:
        return text
    lines = text.splitlines()
    # Already has enough structure (multiple lines).
    if len(lines) >= 4:
        return text
    # One or few lines but contains bullet pattern: split on " - **" so each bullet gets a line.
    import re
    # Replace " - **" with newline + " - **" so bullets break onto separate lines (keep first occurrence as-is to preserve "Recent updates" paragraph).
    parts = re.split(r"(?= - \*\*)", text, flags=re.DOTALL)
    if len(parts) <= 1:
        return text
    # First part is lead paragraph (may end with " - **"); rest are " - **Name:** ..."
    result = parts[0].rstrip()
    for p in parts[1:]:
        p = p.lstrip()
        if not p:
            continue
        if not (p.startswith("- **") or p.startswith(" - **")):
            result += " " + p
            continue
        result += "\n" + p if result else p
    return result


def _rollup_merge_read_more_and_collapse_blanks(text: str) -> str:
    """
    If a line ends with [Read] or [Read more] and the next line is a URL, merge so the link
    is on one line for proper hyperlinking. Then collapse consecutive blank lines to one.
    """
    if not text or not text.strip():
        return text
    lines = text.splitlines()
    merged = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.rstrip()
        # Check if this line ends with [Read] or [Read more] (no URL yet)
        if re.search(r"\[Read(?: more)?\]\s*$", stripped) and i + 1 < len(lines):
            next_stripped = lines[i + 1].strip()
            if next_stripped.startswith("http://") or next_stripped.startswith("https://"):
                merged.append(stripped + "(" + next_stripped + ")")
                i += 2
                continue
        merged.append(line)
        i += 1
    # Collapse consecutive blank lines to one
    collapsed = []
    prev_blank = False
    for line in merged:
        is_blank = line.strip() == ""
        if is_blank:
            if prev_blank:
                continue
            prev_blank = True
        else:
            prev_blank = False
        collapsed.append(line)
    return "\n".join(collapsed)


def format_rollup_summary_for_display(text: Optional[str]) -> Optional[str]:
    """
    Escape roll-up summary for HTML, convert **markdown** to <strong>, convert [text](url)
    to hyperlinks <a href="url">text</a>, and preserve line breaks with <br>.
    Merges [Read more] with URL on next line and collapses excess blank lines.
    """
    if not text or not isinstance(text, str):
        return text
    text = _rollup_merge_read_more_and_collapse_blanks(text)
    text = _normalize_rollup_line_breaks(text)
    import html
    out = []
    for line in text.splitlines():
        s = line.rstrip()
        escaped = html.escape(s)
        # Replace markdown links [text](url) with <a href="url">text</a>.
        escaped = re.sub(
            r"\[([^\]]*)\]\((https?://[^\)]+)\)",
            r'<a href="\2" target="_blank" rel="noopener">\1</a>',
            escaped,
        )
        # Replace **...** with <strong>...</strong>
        escaped = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)
        out.append(escaped)
    return "<br>\n".join(out)


# Section headers to bold in the executive summary display (competitive brief structure).
# EXECUTIVE SUMMARY is not included—we strip that line so the page header is the only title.
_EXEC_SUMMARY_SECTION_HEADERS = frozenset({
    "Key takeaways",
})

# Sections to strip from display (no longer generated; hide if present in older summaries).
_STRIP_SECTIONS = frozenset({
    "IMPACT ON KASA / RECOMMENDED ACTION",
    "INDUSTRY CONTEXT",
})


def format_executive_summary_for_display(text: Optional[str]) -> Optional[str]:
    """
    Escape summary text, bold section headers.
    Strips a leading EXECUTIVE SUMMARY line, removes IMPACT ON KASA / RECOMMENDED ACTION
    and INDUSTRY CONTEXT sections, normalizes dashes to bullets (•), removes blank lines
    before section headers, and collapses multiple consecutive blank lines to one.
    """
    if not text or not isinstance(text, str):
        return text
    import html
    lines = text.splitlines()
    # Strip leading "EXECUTIVE SUMMARY" or "EXECUTIVE SUMMARY:" line so the page header is the only title.
    while lines and lines[0].strip().upper() in ("EXECUTIVE SUMMARY", "EXECUTIVE SUMMARY:"):
        lines.pop(0)
    # Remove IMPACT ON KASA / RECOMMENDED ACTION and INDUSTRY CONTEXT sections (and their content).
    strip_headers_upper = {h.upper() for h in _STRIP_SECTIONS}
    filtered = []
    in_strip_section = False
    for line in lines:
        stripped = line.strip()
        if stripped.upper() in strip_headers_upper:
            in_strip_section = True
            continue
        if in_strip_section and stripped and stripped in _EXEC_SUMMARY_SECTION_HEADERS:
            in_strip_section = False
        if in_strip_section:
            # Still in a strip section: skip bullet lines until next section or blank+section.
            if stripped and not (stripped.startswith("- ") or stripped.startswith("• ") or stripped.startswith("* ")):
                # Could be start of next section (e.g. "Key takeaways")
                if stripped in _EXEC_SUMMARY_SECTION_HEADERS:
                    in_strip_section = False
                else:
                    continue  # other non-bullet line inside strip section
            else:
                continue  # bullet under strip section
        filtered.append(line)
    lines = filtered
    # Collapse multiple consecutive blank lines to a single blank line (reduces spacing between bullets).
    collapsed = []
    prev_blank = False
    for line in lines:
        stripped = line.strip()
        is_blank = stripped == ""
        if is_blank:
            if prev_blank:
                continue  # skip extra blank lines
            prev_blank = True
        else:
            prev_blank = False
        collapsed.append(line)
    # Remove blank lines that sit between two bullet lines so bullets run - X / - X / - X with no gap.
    def _is_bullet_line(s: str) -> bool:
        t = s.strip()
        return len(t) > 1 and (t.startswith("- ") or t.startswith("• ") or t.startswith("* "))
    no_blank_between_bullets = []
    for i, line in enumerate(collapsed):
        stripped = line.strip()
        if stripped == "":
            prev_ok = i > 0 and _is_bullet_line(collapsed[i - 1])
            next_ok = i + 1 < len(collapsed) and _is_bullet_line(collapsed[i + 1])
            if prev_ok and next_ok:
                continue  # skip blank between two bullets
        no_blank_between_bullets.append(line)
    lines = no_blank_between_bullets
    # Remove blank line immediately before a section header to tighten spacing.
    section_headers_upper = {h.upper() for h in _EXEC_SUMMARY_SECTION_HEADERS}
    tightened = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == "":
            j = i + 1
            while j < len(lines) and lines[j].strip() == "":
                j += 1
            if j < len(lines):
                next_stripped = lines[j].strip().upper()
                if next_stripped in section_headers_upper:
                    continue  # drop this blank line before header
        tightened.append(line)
    lines = tightened
    out = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        # Normalize dash bullets to bullet character for display.
        if line.startswith("- ") and not line.startswith("• "):
            line = "• " + line[2:]
        escaped = html.escape(line)
        # One blank line before section headers for consistent spacing.
        is_section_header = stripped in _EXEC_SUMMARY_SECTION_HEADERS
        if is_section_header and out:
            out.append("")
        if is_section_header:
            out.append("<strong>" + html.escape(stripped) + "</strong>")
        else:
            out.append(escaped)
    return "\n".join(out)


def clean_senior_role_bullets_for_dossier(bullets: List[str], *, competitor_name: str = "") -> List[str]:
    """
    Use the LLM to clean senior-role bullet strings for display. Normalizes location formatting
    (e.g. "Austin, Texas, United States" → "Austin, TX"), removes redundant country suffixes,
    and keeps titles readable. Returns the same number of strings in the same order; on failure
    returns the original list unchanged.
    """
    if not bullets:
        return bullets
    from .config import get_openai_client
    client = get_openai_client()
    if not client:
        return bullets

    system = """You clean job-role bullet lines for a talent snapshot. Each line is either a job title alone or "Title (Location)".

Your job:
- Normalize locations: use "City, ST" for US (e.g. "Austin, Texas, United States" → "Austin, TX"; "New York, NY" keep).
- Drop trailing ", United States" or ", USA" or ", US".
- For "Remote", "Hybrid", or work-type-only text in parens, keep it as-is (e.g. "VP of Sales (Remote)").
- Keep the job title exactly as given; only clean the location part in parens.
- If a line has no parens, return it unchanged.
- Return the same number of lines in the same order. One line per bullet, no numbering or extra text."""

    user = f"Bullets to clean (one per line):\n" + "\n".join(bullets)

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            max_tokens=2048,
            temperature=0.1,
        )
        content = (resp.choices[0].message.content or "").strip()
        if not content:
            return bullets
        cleaned = [line.strip() for line in content.splitlines() if line.strip()]
        if len(cleaned) == len(bullets):
            return cleaned
        return bullets
    except Exception:
        return bullets


def clean_location_display_for_dossier(
    competitor_name: str,
    properties_by_location: List[Dict[str, Any]],
    asset_delta_by_city: List[Dict[str, Any]],
    *,
    other_sub_bullets_text: Optional[str] = None,
    debug_return_parsed: bool = False,
) -> Optional[Dict[str, Any]]:
    """
    Use the LLM to consolidate the property list by state. Input is ONLY the list of
    summarized bullets (location: count, keys). No per-property data. The LLM reviews
    whether rows are grouped by state; if not, it adds totals and maps regions to the
    closest state (or keeps a region as its own row if it does not map neatly to any state).
    Returns state-level properties_by_location and asset_delta_by_city.
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

    # Send ONLY the summarized bullets (location, count, keys). No per-property data.
    max_location_rows = 2000
    rows_sent = properties_by_location[:max_location_rows]
    raw_total = sum(r.get("count", 0) for r in rows_sent)
    raw_keys_total = sum(r.get("keys", 0) for r in rows_sent)
    counts_text = "; ".join(_loc_count_keys(r) for r in rows_sent)
    deltas_text = "; ".join(f"{r['location']}: +{r['added']}/−{r['removed']}" for r in asset_delta_by_city[:25])
    if not counts_text and not deltas_text:
        return None

    system = """You are consolidating property location data for a real estate/hospitality dashboard.

INPUT: You receive ONLY a list of summarized bullets: "Location: N" or "Location: N (K keys)". There is no per-property data—just these raw numbers by location and key counts. Location labels may be full state names, or US region/city/area names (often title-cased from URL slugs, e.g. "Emerald Coast 30a", "Smith Mountain Lake", "Lake Norman").

YOUR JOB — follow these two steps in order:

Step 1 — Consolidate and clean state-level rows:
- Merge duplicate state rows (same US state name) into a single row; sum "count" and "keys".
- Normalize state names to full US state names (e.g. CA → California, Florida → Florida).
- Treat "Washington D.C.", "Washington, D.C.", and "District of Columbia" as the same as "Washington DC"; merge into one row with location "Washington DC".
- Do not yet change any region/city/geographical labels.

Step 2 — Map regions/cities/geographical labels to states:
- For EVERY remaining row that is NOT already a US state, use US geographic knowledge to assign it to the best-fit US state and merge that row's count and keys into that state.
- Do NOT put any US city, region, or area name into "Other". When in doubt, assign to the most likely single state.
- Examples you must follow: Coastal Charleston → South Carolina; Emerald Coast, 30a, Emerald Coast 30a → Florida; Lake Norman → North Carolina; Newport Beach → California; Central Oregon, Bend, Sunriver → Oregon; Poconos → Pennsylvania; Smith Mountain Lake → Virginia; Gulf Shores → Alabama; Blue Ridge (or similar) → North Carolina or Georgia; Myrtle Beach, Hilton Head, Kiawah → South Carolina; Gatlinburg, Pigeon Forge, Smoky Mountains → Tennessee; Branson → Missouri; Ozarks → Missouri; Destin, Panama City Beach, 30a → Florida; Outer Banks → North Carolina; Cape Cod, Berkshires → Massachusetts; Hamptons, Hudson Valley, Catskills → New York.
- Only keep a row as its own label (do not merge) if it genuinely does not map to any single US state (e.g. international, or multi-state region with no clear primary). Do not use "Other" for those—keep the original label.
- Use "Other" ONLY for: Unspecified, career site, privacy, non-property URLs (e.g. /search), or genuinely non-US/unclear. Never put a US city, region, or area into "Other".

RULES:
- Use full US state names only. When merging, sum both "count" and "keys".
- Grand total of output "count" MUST equal the input total; sum "keys" correctly when merging.
- For asset_delta_by_city: one row per state; "added" and "removed" are the sums of deltas you merged into that state.
- Output order: list US states first (by count descending), then any unchanged non-state labels, then "Other" last if needed.

OUTPUT FORMAT:
- Return JSON only, no markdown: {"properties_by_location": [{"location": "...", "count": n, "keys": k}, ...], "asset_delta_by_city": [{"location": "...", "added": a, "removed": r}, ...]}.
- Include "keys" (number, 0 if not provided) on every properties_by_location row.
- Your output is the *merged* result: each US state must appear at most once, with that state's summed count and keys. Do not include separate rows for regions/cities you have merged into a state (e.g. do not output both "South Carolina" and "Coastal Charleston" if you merged Coastal Charleston into South Carolina—output only "South Carolina" with the combined total)."""

    num_bullets = len(rows_sent)
    user = f"Competitor: {competitor_name}\n\nTotal property count (your output counts MUST sum to this): {raw_total}\nTotal keys (sum when merging): {raw_keys_total}\n\nThere are {num_bullets} location bullets below. Step 1: consolidate and clean state-level rows (merge duplicates, normalize names). Step 2: for any region/city/geographical label, assign it to the best-fit US state and merge its counts into that state; only keep a row separate if it does not map to any single US state.\n\nCurrent counts by location (raw):\n{counts_text or 'none'}\n\nChanges by location (raw):\n{deltas_text or 'none'}"

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
                "location": _normalize_dc_label((r.get("location") or "").strip()),
                "count": _int(r.get("count"), 0),
                "keys": _int(r.get("keys"), 0),
            }
            for r in counts
            if isinstance(r, dict) and r.get("location") is not None
            and (r.get("location") or "").strip()
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
