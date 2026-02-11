import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import or_

from ..db import get_session, get_last_refreshed
from ..models import Competitor, Event, Snapshot, Capability
from ..diff.asset_diff import diff_properties, delta_by_city, infer_location_for_property, is_location_treated_as_other, parse_keys_from_details
from ..executive_summary import (
    generate_executive_summary,
    clean_location_display_for_dossier,
    aggregate_state_and_state_city_rows,
)
from ..llm_structured import _assign_state_from_url
from ..rules.talent_rules import job_functional_area, FUNCTIONAL_AREA_DISPLAY_ORDER, PROPERTY_OPERATIONS_LABEL

router = APIRouter()

# In-memory caches for LLM results (keyed so repeat loads / same snapshot are fast).
# Max entries to avoid unbounded growth; LRU-style eviction by clearing when over limit.
_EXEC_SUMMARY_CACHE: dict[tuple, str] = {}
_EXEC_SUMMARY_CACHE_MAX = 50
_LOCATION_CLEAN_CACHE: dict[tuple, dict] = {}
_LOCATION_CLEAN_CACHE_MAX = 100
# Bump when prompt or aggregation logic changes so cached results are invalidated and new LLM runs.
_LOCATION_CLEAN_CACHE_VERSION = 2


def _exec_summary_cache_key(competitor_id: int, context: dict) -> tuple:
    """Stable key so we reuse summary when data hasn't changed."""
    base = context.get("comparison_baseline_date") or ""
    n_events = len(context.get("events_this_week") or [])
    n_news = len(context.get("top_news") or [])
    added = context.get("asset_added_since_baseline") or 0
    removed = context.get("asset_removed_since_baseline") or 0
    return (competitor_id, base, n_events, n_news, added, removed)


def _location_clean_cache_key(name: str, props: list, deltas: list, other_bullets: Optional[str] = None) -> tuple:
    props_sig = tuple((r.get("location"), r.get("count"), r.get("keys", 0)) for r in (props or [])[:100])
    deltas_sig = tuple((r.get("location"), r.get("added"), r.get("removed")) for r in (deltas or [])[:50])
    other_sig = (other_bullets or "")[:500]
    return (_LOCATION_CLEAN_CACHE_VERSION, name, props_sig, deltas_sig, other_sig)


def clear_dossier_caches_for_competitor(competitor_id: int) -> None:
    """Clear in-memory LLM caches so dossier and lazy endpoints show fresh data after a refresh."""
    global _EXEC_SUMMARY_CACHE, _LOCATION_CLEAN_CACHE
    _EXEC_SUMMARY_CACHE = {k: v for k, v in _EXEC_SUMMARY_CACHE.items() if k[0] != competitor_id}
    _LOCATION_CLEAN_CACHE.clear()  # keys are (competitor_name, ...); clear all to avoid stale location data

# Suggested next actions based on event types (rules-based).
RECOMMENDATIONS_MAP = {
    "asset.new_market": ("Review our presence and positioning in that market.", "Footprint expansion"),
    "asset.market_exit": ("Confirm exit and assess implications for our strategy.", "Potential retreat"),
    "asset.pipeline_signal": ("Track pipeline; consider competitive response when they launch.", "Pipeline signal"),
    "talent.senior_hire_or_role_posted": ("Monitor for org/strategy shifts; benchmark our own senior hiring.", "Senior hire"),
    "talent.new_capability": ("Assess our capability in that area; consider counter-investment.", "New capability"),
    "talent.hiring_surge": ("Interpret as strategic emphasis; review our roadmap in that function.", "Hiring surge"),
    "partner.major_partnership": ("Evaluate impact on distribution; consider similar or counter partnerships.", "Partnership"),
    "partner.partnership_surge": ("Watch for distribution strategy shift.", "Partnership surge"),
    "capital.fundraise_or_restructuring": ("Monitor for positioning and pricing changes post-capital.", "Capital event"),
    "narrative.priority_shift": ("Align messaging and positioning with their stated priorities.", "Narrative shift"),
    "narrative.homepage_updated": ("Review the updated page for messaging or product changes.", "Digital footprint"),
    "public_record.filing": ("Review filing for branding or entity strategy implications.", "Public record"),
}


def _utc_dt(dt: Optional[datetime]) -> Optional[datetime]:
    """Return datetime as UTC-aware; avoid naive/aware comparison errors."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_press_date(value) -> Optional[datetime]:
    """Best-effort parse for press item dates (RSS or ISO strings). Always returns UTC-aware or None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return _utc_dt(value)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc)
        except Exception:
            return None
    if isinstance(value, str):
        val = value.strip()
        if not val:
            return None
        try:
            return _utc_dt(datetime.fromisoformat(val.replace("Z", "+00:00")))
        except Exception:
            pass
        try:
            return _utc_dt(parsedate_to_datetime(val))
        except Exception:
            return None
    return None


def _event_dict(e) -> dict:
    """Serializable event for templates (avoids DetachedInstanceError)."""
    detected = e.detected_at.strftime("%Y-%m-%d") if getattr(e, "detected_at", None) else None
    occurred = e.occurred_at.strftime("%Y-%m-%d") if getattr(e, "occurred_at", None) else None
    return {
        "title": e.title,
        "summary": e.summary,
        "severity": e.severity,
        "category": e.category,
        "type": e.type,
        "detected_at_str": detected,
        "occurred_at_str": occurred,
        "why_it_matters": e.why_it_matters,
        "evidence_json": e.evidence_json,
    }


def build_recommendations(events: list) -> list[dict]:
    """Build suggested next actions from events (deduplicated by type)."""
    seen_types = set()
    out = []
    for event in events:
        if not event.type or event.type in seen_types:
            continue
        pair = RECOMMENDATIONS_MAP.get(event.type)
        if pair:
            action, reason = pair
            seen_types.add(event.type)
            out.append({"action": action, "reason": reason, "event_type": event.type})
    if not out:
        out.append({"action": "No specific actions this period; maintain routine monitoring.", "reason": "No high-signal events", "event_type": None})
    return out


def format_dossier_preview_text(context: dict) -> str:
    """Format dossier context as plain text matching what the site displays (for terminal debug)."""
    if context.get("error"):
        return f"[Error] {context['error']}"
    name = (context.get("competitor") or {}).get("name") or "Unknown"
    lines = [
        "",
        "=" * 60,
        f"  SITE PREVIEW: {name}",
        "=" * 60,
        "",
        "--- Executive summary ---",
        "(On site: AI summary or 'Loading…' / 'Set OPENAI_API_KEY…')",
        "",
        "--- Top news (up to 5) ---",
    ]
    top_news = context.get("top_news") or []
    if top_news:
        for item in top_news:
            date_str = item.get("date") or ""
            title = item.get("title") or "Article"
            url = item.get("url") or item.get("link") or ""
            topic = (item.get("topic") or "").replace("_", " ")
            outlet = item.get("outlet") or ""
            raw_summary = item.get("summary") or ""
            summary = raw_summary[:120] + ("…" if len(raw_summary) > 120 else "") if raw_summary else ""
            parts = [f"  {date_str} — {title}", f"    URL: {url}"]
            if topic:
                parts.append(f"    Topic: {topic}")
            if outlet:
                parts.append(f"    Outlet: {outlet}")
            if summary:
                parts.append(f"    Summary: {summary}")
            lines.append("\n".join(parts))
            lines.append("")
    else:
        lines.append("  No press items in snapshot, or none passed relevance filter.")
        lines.append("")
    lines.append("--- Properties by location ---")
    total = context.get("total_properties") or 0
    if total:
        lines.append(f"  Total: {total} propert{'ies' if total != 1 else 'y'}")
    props = context.get("properties_by_location") or []
    if props:
        for row in props:
            loc = row.get("location") or ""
            count = row.get("count") or 0
            keys = row.get("keys") or 0
            keys_str = f" ({keys} keys)" if keys else ""
            lines.append(f"  · {loc}: {count} propert{'ies' if count != 1 else 'y'}{keys_str}")
        other_display = context.get("other_properties_display") or []
        if other_display and any((r.get("location") or "").strip() == "Other" for r in props):
            for op in other_display:
                lines.append(f"      - {op.get('name') or 'Unnamed'} — {op.get('raw_location') or op.get('market') or op.get('url') or '—'}")
    else:
        lines.append("  No property data yet. Run the asset collector to populate.")
    lines.append("")
    lines.append("--- Talent snapshot ---")
    talent_jobs = context.get("talent_jobs") or []
    jobs_bf = context.get("jobs_by_function") or []
    jobs_prop = context.get("jobs_by_function_property") or []
    if talent_jobs:
        lines.append(f"  Total roles: {len(talent_jobs)}")
        lines.append("  Business & strategy:")
        if jobs_bf:
            for row in jobs_bf:
                s = f" ({row.get('senior')} senior)" if row.get("senior") else ""
                lines.append(f"    · {row.get('function')}: {row.get('total')} role{'s' if (row.get('total') or 0) != 1 else ''}{s}")
        else:
            lines.append("    No business/strategy roles in this snapshot.")
        lines.append("  Property operations:")
        if jobs_prop:
            for row in jobs_prop:
                s = f" ({row.get('senior')} senior)" if row.get("senior") else ""
                lines.append(f"    · {row.get('function')}: {row.get('total')} role{'s' if (row.get('total') or 0) != 1 else ''}{s}")
        else:
            lines.append("    No property operations roles in this snapshot.")
    else:
        lines.append("  No talent data yet. Run the talent collector to populate.")
    lines.append("")
    lines.append("--- Press (90d, business-focused) ---")
    press_90d = context.get("press_90d") or []
    if press_90d:
        lines.append(f"  Total items: {len(press_90d)}")
        for item in press_90d[:5]:
            date_str = item.get("date") or "Date unknown"
            title = item.get("display_title") or item.get("title") or "Article"
            lines.append(f"  · {date_str} — {title}")
        if len(press_90d) > 5:
            lines.append(f"  … and {len(press_90d) - 5} more")
    else:
        lines.append("  No business-relevant press.")
    lines.append("")
    lines.append("--- Recent events (90 days) ---")
    events = context.get("events") or []
    if events:
        lines.append(f"  Total events: {len(events)}")
        for e in events[:5]:
            title = e.get("title") or "Event"
            when = e.get("occurred_at_str") or e.get("detected_at_str") or ""
            cat = e.get("category") or ""
            typ = e.get("type") or ""
            lines.append(f"  · {when} — {title} [{cat} / {typ}]")
        if len(events) > 5:
            lines.append(f"  … and {len(events) - 5} more")
    else:
        lines.append("  No recent events.")
    lines.append("")
    return "\n".join(lines)


def build_dossier_context(session, competitor_id: int, *, skip_property_llm: bool = False) -> dict:
    """Build dossier context. When skip_property_llm=True, use URL-derived locations only (no LLM) for fast load."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=90)
    competitor = session.get(Competitor, competitor_id)
    if competitor is None:
        return {"error": "Competitor not found."}

    latest_asset = (
        session.query(Snapshot)
        .filter(Snapshot.competitor_id == competitor_id, Snapshot.channel == "asset")
        .order_by(Snapshot.captured_at.desc())
        .first()
    )
    # Prefer most recent talent snapshot that has jobs (Lark/AvantStay often get 0 jobs on cron without Playwright).
    talent_candidates = (
        session.query(Snapshot)
        .filter(Snapshot.competitor_id == competitor_id, Snapshot.channel == "talent")
        .order_by(Snapshot.captured_at.desc())
        .limit(20)
        .all()
    )
    latest_talent = None
    for s in talent_candidates:
        jobs_in = (s.structured_json or {}).get("jobs", [])
        if jobs_in and any(isinstance(j, dict) for j in jobs_in):
            latest_talent = s
            break
    if not latest_talent and talent_candidates:
        latest_talent = talent_candidates[0]  # show latest even if empty
    latest_press = (
        session.query(Snapshot)
        .filter(Snapshot.competitor_id == competitor_id, Snapshot.channel == "press")
        .order_by(Snapshot.captured_at.desc())
        .first()
    )

    # Include events that are "recent" by either detection or occurrence (90 days).
    # This ensures events that occurred within 90 days are shown even if they were detected earlier.
    events = (
        session.query(Event)
        .filter(
            Event.competitor_id == competitor_id,
            or_(
                Event.detected_at >= cutoff,
                (Event.occurred_at.isnot(None)) & (Event.occurred_at >= cutoff),
            ),
        )
        .order_by(Event.detected_at.desc())
        .all()
    )

    # Reporting baseline = manual seed date when set, otherwise oldest snapshot.
    # Only events and news *after* this count as "new" in summaries.
    reporting_baseline_date = None
    if getattr(competitor, "reporting_baseline_at", None):
        reporting_baseline_date = competitor.reporting_baseline_at.strftime("%Y-%m-%d")
    else:
        for ch in ("asset", "talent", "press"):
            oldest = (
                session.query(Snapshot)
                .filter(Snapshot.competitor_id == competitor_id, Snapshot.channel == ch)
                .order_by(Snapshot.captured_at.asc())
                .first()
            )
            if oldest:
                d = oldest.captured_at.strftime("%Y-%m-%d")
                if reporting_baseline_date is None or d < reporting_baseline_date:
                    reporting_baseline_date = d
    if reporting_baseline_date:
        baseline_cutoff = datetime.strptime(reporting_baseline_date, "%Y-%m-%d")
        baseline_date = baseline_cutoff.date()
        events = [
            e
            for e in events
            if e.detected_at.date() >= baseline_date
            or (e.occurred_at is not None and _utc_dt(e.occurred_at) and _utc_dt(e.occurred_at).date() >= baseline_date)
        ]
    # Order events by date published (occurred_at) when available, else detected_at, newest first.
    # Use timestamp to avoid naive/aware comparison errors.
    def _event_sort_key(e):
        dt = e.occurred_at or e.detected_at
        return (_utc_dt(dt) or datetime.min.replace(tzinfo=timezone.utc)).timestamp()
    events = sorted(events, key=_event_sort_key, reverse=True)

    capabilities = (
        session.query(Capability)
        .filter(Capability.competitor_id == competitor_id)
        .order_by(Capability.first_seen_at.asc())
        .all()
    )

    raw_asset_props = (latest_asset.structured_json or {}).get("properties", []) if latest_asset else []
    asset_props = [p for p in raw_asset_props if isinstance(p, dict)]
    # Use URL-derived state only (e.g. Avantstay path -> state). No per-property LLM; the list
    # (location - count) is sent once to the LLM in clean_location_display_for_dossier to bucket by state.
    if latest_asset and asset_props:
        asset_props = [_assign_state_from_url(p) for p in asset_props]
    # Inferred locations (URL-derived state or path slug) for summary and counts
    _locations = [infer_location_for_property(p) for p in asset_props]
    markets = sorted({loc for loc in _locations if loc != "Unspecified"})

    raw_talent_jobs = (latest_talent.structured_json or {}).get("jobs", []) if latest_talent else []
    talent_jobs = [j for j in raw_talent_jobs if isinstance(j, dict)]

    # Summarize jobs by functional area (LLM-set or rule-based fallback); split into business vs property operations.
    # When stored value is missing or "Other", try rule-based so keyword-matched roles (e.g. Property operations) are used.
    jobs_by_function = []
    jobs_by_function_property = []
    by_func: dict[str, list[dict]] = {}
    for job in talent_jobs:
        stored = job.get("functional_area")
        if stored and stored != "Other":
            func = stored
        else:
            func = job_functional_area(job)
        by_func.setdefault(func, []).append(job)
    order = {name: i for i, name in enumerate(FUNCTIONAL_AREA_DISPLAY_ORDER)}
    for func in sorted(by_func.keys(), key=lambda f: (order.get(f, 99), f)):
        jobs_list = by_func[func]
        senior_count = sum(1 for j in jobs_list if j.get("is_senior"))
        row = {"function": func, "total": len(jobs_list), "senior": senior_count}
        if func == PROPERTY_OPERATIONS_LABEL:
            jobs_by_function_property.append(row)
        else:
            jobs_by_function.append(row)

    raw_press_items = (latest_press.structured_json or {}).get("items", []) if latest_press else []
    press_items = [i for i in raw_press_items if isinstance(i, dict)]
    raw_canonical_press = (latest_press.structured_json or {}).get("canonical_items", []) if latest_press else []
    canonical_press = [i for i in raw_canonical_press if isinstance(i, dict)]
    raw_press_groups = (latest_press.structured_json or {}).get("press_groups", []) if latest_press else []
    press_groups_snapshot = [g for g in raw_press_groups if isinstance(g, dict) and g.get("articles")]

    takeaways = []
    if any(event.type == "asset.new_market" for event in events):
        takeaways.append("Recent footprint expansion activity detected.")
    if any(event.type == "talent.hiring_surge" for event in events):
        takeaways.append("Hiring surge suggests strategic buildout.")
    if any(event.category == "partner" for event in events):
        takeaways.append("Partnership activity indicates distribution focus.")
    if not takeaways:
        takeaways.append("No major strategic shifts detected in the last 120 days.")

    recommendations = build_recommendations(events)

    week_cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    events_this_week = [e for e in events if _utc_dt(e.detected_at) and _utc_dt(e.detected_at) >= week_cutoff]
    events_this_week_dicts = [_event_dict(e) for e in events_this_week]

    def _press_display_title(raw_title: str) -> str:
        """Clean slug-derived titles: strip trailing numeric IDs (e.g. PR Newswire) and title-case if all lowercase."""
        if not raw_title or not isinstance(raw_title, str):
            return raw_title or ""
        t = raw_title.strip()
        t = re.sub(r"\s+\d{7,}$", "", t)  # strip trailing space + long numeric ID (e.g. 302576370)
        if not t:
            return raw_title.strip()
        if t.islower() and len(t) > 3:
            return t.title()
        return t

    # Build press_groups for template: use snapshot groups if present, else one group per canonical item (backward compat).
    if press_groups_snapshot:
        press_groups = []
        for g in press_groups_snapshot:
            articles = []
            for art in g.get("articles") or []:
                a = dict(art)
                a["display_title"] = _press_display_title(a.get("title") or "")
                articles.append(a)
            press_groups.append({
                "group_title": g.get("group_title") or "News",
                "one_line_summary": g.get("one_line_summary") or "",
                "articles": articles,
            })
    else:
        press_groups = [
            {
                "group_title": _press_display_title(item.get("title") or ""),
                "one_line_summary": (item.get("summary") or "").strip(),
                "articles": [{
                    **dict(item),
                    "display_title": _press_display_title(item.get("title") or ""),
                    "url": item.get("url") or item.get("link"),
                    "outlet": item.get("outlet"),
                }],
            }
            for item in canonical_press
            if (item.get("topic") or "").strip().lower() not in {"irrelevant", "promo_or_brand_marketing"}
        ]

    # Flat list for sorting and Top news: from groups (all articles) or from canonical_press (old snapshot).
    if press_groups_snapshot:
        flat_for_sort = []
        for g in press_groups_snapshot:
            for art in g.get("articles") or []:
                flat_for_sort.append(dict(art))
    else:
        flat_for_sort = [
            item for item in canonical_press
            if (item.get("topic") or "").strip().lower() not in {"irrelevant", "promo_or_brand_marketing"}
        ]
    press_90d = []
    for item in flat_for_sort:
        dt = _parse_press_date(item.get("date"))
        display_date = dt.strftime("%Y-%m-%d") if dt else None
        out = dict(item)
        if display_date:
            out["date"] = display_date
        elif item.get("date") and isinstance(item.get("date"), str) and (item.get("date") or "").strip():
            out["date"] = (item.get("date", "") or "").strip()[:20]
        out["display_title"] = _press_display_title(out.get("title") or "")
        out["_sort_dt"] = dt
        press_90d.append(out)
    press_90d.sort(key=lambda x: (x["_sort_dt"] is None, -(x["_sort_dt"].timestamp() if x["_sort_dt"] else 0)))
    for p in press_90d:
        p.pop("_sort_dt", None)

    # Top news: up to 5 most newsworthy articles. Use topic when present; else treat as other_business (new group-based shape).
    STRONG_BUSINESS_TOPICS = {
        "fundraising", "restructuring_or_layoffs", "executive_interview",
        "new_partnership", "new_hotel_opening", "other_business",
    }
    TOPIC_NEWSWORTHINESS = {
        "fundraising": 5,
        "new_partnership": 5,
        "restructuring_or_layoffs": 5,
        "executive_interview": 5,
        "new_hotel_opening": 5,
        "other_business": 2,
    }
    pool = [
        item for item in press_90d
        if (item.get("topic") or "").strip().lower() in STRONG_BUSINESS_TOPICS or not item.get("topic")
    ]
    # When reporting baseline is set (e.g. after "Reset baseline"), only news on or after that date counts.
    if reporting_baseline_date:
        pool = [
            item for item in pool
            if _parse_press_date(item.get("date")) and _parse_press_date(item.get("date")).date() >= baseline_cutoff.date()
        ]
    week_cutoff_pub = datetime.now(timezone.utc) - timedelta(days=7)

    def _newsworthiness_score(item: dict) -> tuple:
        topic = (item.get("topic") or "").strip().lower()
        topic_score = TOPIC_NEWSWORTHINESS.get(topic, 2)  # default 2 (other_business) when no topic (group-based shape)
        dt = _parse_press_date(item.get("date"))
        recency_score = 5 if (dt and dt >= week_cutoff_pub) else 0  # last 7 days boost
        score = topic_score + recency_score  # no source prioritization; all sources equal
        # Use timestamp for sort to avoid mixing naive/aware datetimes (TypeError on some pages)
        ts = dt.timestamp() if dt else 0.0
        return (score, ts)

    pool_scored = []
    for item in pool:
        score, ts = _newsworthiness_score(item)
        pool_scored.append((score, ts, item))
    # Highest newsworthiness first; within same score, newest date first
    pool_scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    top_news = [item for _, _, item in pool_scored[:5]]

    # Properties by location (state/city) for high-level week-over-week tracking.
    # Aggregate count and total keys per location (keys parsed from property details).
    location_counts = {}
    location_keys = {}
    for p in asset_props:
        loc = infer_location_for_property(p)
        location_counts[loc] = location_counts.get(loc, 0) + 1
        location_keys[loc] = location_keys.get(loc, 0) + parse_keys_from_details(p.get("details"))
    properties_by_location = [
        {"location": loc, "count": n, "keys": location_keys.get(loc, 0)}
        for loc, n in sorted(location_counts.items(), key=lambda x: (-x[1], x[0]))
    ]
    # Per-location summary only (one bullet per location: "Location - N properties (M keys)"); no sublists.
    by_loc_list: dict[str, list[dict]] = {}
    for p in asset_props:
        loc = infer_location_for_property(p)
        by_loc_list.setdefault(loc, []).append({
            "name": (p.get("name") or "").strip() or "Unnamed",
            "details": (p.get("details") or "").strip() or None,
        })
    def _location_sort_key(item):
        loc, plist = item
        is_trailing = 1 if (loc or "").strip() in ("Other", "Unspecified") else 0
        return (is_trailing, -len(plist), (loc or "").lower())

    properties_by_location_with_list = [
        {
            "location": loc,
            "count": len(plist),
            "keys": location_keys.get(loc, 0),
        }
        for loc, plist in sorted(by_loc_list.items(), key=_location_sort_key)
    ]
    total_properties = len(asset_props)

    # Asset comparison baseline: when reporting_baseline_at is set, use the latest snapshot
    # at or before that time so property deltas = "since reset". Otherwise oldest snapshot (seed).
    asset_baseline_date = None
    asset_added_since_baseline = 0
    asset_removed_since_baseline = 0
    asset_delta_by_city = []
    if latest_asset:
        baseline_asset = None
        if getattr(competitor, "reporting_baseline_at", None):
            baseline_asset = (
                session.query(Snapshot)
                .filter(
                    Snapshot.competitor_id == competitor_id,
                    Snapshot.channel == "asset",
                    Snapshot.captured_at <= competitor.reporting_baseline_at,
                )
                .order_by(Snapshot.captured_at.desc())
                .first()
            )
        if baseline_asset is None:
            baseline_asset = (
                session.query(Snapshot)
                .filter(Snapshot.competitor_id == competitor_id, Snapshot.channel == "asset")
                .order_by(Snapshot.captured_at.asc())
                .first()
            )
        if baseline_asset:
            raw_baseline = (baseline_asset.structured_json or {}).get("properties", [])
            baseline_props = [p for p in raw_baseline if isinstance(p, dict)]
            diff = diff_properties(baseline_props, asset_props)
            asset_added_since_baseline = len(diff["added"])
            asset_removed_since_baseline = len(diff["removed"])
            asset_baseline_date = baseline_asset.captured_at.strftime("%Y-%m-%d")
            asset_delta_by_city = delta_by_city(diff["added"], diff["removed"])

    # Expand "Other" (non-state) into subbullets for display and for LLM (URL path hints: temecula, central-oregon, etc.).
    other_properties_display = []
    other_props = [p for p in asset_props if is_location_treated_as_other(infer_location_for_property(p))]
    if other_props:
        other_properties_display = [
            {
                "name": (p.get("name") or "").strip() or "Unnamed",
                "url": (p.get("url") or "").strip() or "",
                "market": (p.get("market") or "").strip() or "",
                "raw_location": infer_location_for_property(p),
            }
            for p in other_props
        ]

    # One LLM step: send only the list (location - count) and Other sub-bullets; LLM buckets by state. No per-property data.
    # Pre-aggregate "State - City" rows (e.g. Vermont - Burlington, Vermont - Stowe) into one row per state with summed count/keys
    # so the LLM receives state-level rows and we get e.g. "Vermont – 2 properties (62 keys)".
    properties_by_location_for_llm = aggregate_state_and_state_city_rows(properties_by_location)
    other_sub_bullets_text = None
    if other_properties_display:
        other_sub_bullets_text = "\n".join(
            f"{d.get('url', '')} — {d.get('raw_location', 'Other')}" for d in other_properties_display
        )
    cleaned = None
    if not skip_property_llm:
        loc_key = _location_clean_cache_key(
            competitor.name, properties_by_location_for_llm, asset_delta_by_city, other_sub_bullets_text
        )
        if loc_key in _LOCATION_CLEAN_CACHE:
            cleaned = _LOCATION_CLEAN_CACHE[loc_key]
        else:
            cleaned = clean_location_display_for_dossier(
                competitor.name,
                properties_by_location_for_llm,
                asset_delta_by_city,
                other_sub_bullets_text=other_sub_bullets_text,
            )
            if cleaned and len(_LOCATION_CLEAN_CACHE) >= _LOCATION_CLEAN_CACHE_MAX:
                _LOCATION_CLEAN_CACHE.clear()
            if cleaned:
                _LOCATION_CLEAN_CACHE[loc_key] = cleaned
    location_totals_match = True
    if cleaned:
        properties_by_location = cleaned.get("properties_by_location") or properties_by_location_for_llm
        asset_delta_by_city = cleaned.get("asset_delta_by_city") or asset_delta_by_city
        location_totals_match = cleaned.get("location_totals_match", True)
    else:
        # No LLM or rejected: still use state-aggregated rows so display is one row per state (e.g. Vermont – 2 properties (62 keys))
        properties_by_location = properties_by_location_for_llm
    # Use same list for single-bullet display (one line per location: "Location – N properties (M keys)")
    properties_by_location_with_list = [
        {"location": r["location"], "count": r["count"], "keys": r.get("keys", 0)}
        for r in properties_by_location
    ]

    context = {
        "competitor": {"id": competitor.id, "name": competitor.name},
        "markets": markets,
        "capabilities": [{"capability": c.capability} for c in capabilities],
        "events": [_event_dict(e) for e in events],
        "talent_jobs": talent_jobs,
        "jobs_by_function": jobs_by_function,
        "jobs_by_function_property": jobs_by_function_property,
        "press_items": press_items,
        "press_90d": press_90d,
        "press_groups": press_groups,
        "takeaways": takeaways,
        "recommendations": recommendations,
        "events_this_week": events_this_week_dicts,
        "top_news": top_news,
        "properties_by_location": properties_by_location,
        "properties_by_location_with_list": properties_by_location_with_list,
        "total_properties": total_properties,
        "location_totals_match": location_totals_match,
        "asset_baseline_date": asset_baseline_date,
        "asset_added_since_baseline": asset_added_since_baseline,
        "asset_removed_since_baseline": asset_removed_since_baseline,
        "asset_delta_by_city": asset_delta_by_city,
        "other_properties_display": other_properties_display,
        "comparison_baseline_date": reporting_baseline_date,
    }
    # Executive summary is loaded lazily via JS (see GET /dossier/{id}/executive-summary) so page renders fast.
    context["executive_summary"] = None
    # Set by route when skip_property_llm and API key is set, so frontend can lazy-load refined properties
    context["properties_refinement_available"] = False
    return context


def build_summary_context(session, competitor_id: int, days: int = 7) -> dict:
    """Weekly executive summary: high-signal events from last N days, grouped by category."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    competitor = session.get(Competitor, competitor_id)
    if competitor is None:
        return {"error": "Competitor not found."}

    events = (
        session.query(Event)
        .filter(
            Event.competitor_id == competitor_id,
            or_(
                Event.detected_at >= cutoff,
                (Event.occurred_at.isnot(None)) & (Event.occurred_at >= cutoff),
            ),
        )
        .order_by(Event.detected_at.desc())
        .all()
    )
    # Prefer high/med for executive summary; include low only if needed.
    events = [e for e in events if e.severity in ("high", "med")]

    by_category = {}
    for e in events:
        by_category.setdefault(e.category, []).append(_event_dict(e))

    recommendations = build_recommendations(events)
    new_this_week = [e for e in events if e.category == "asset" or (e.type or "").startswith("asset.")]

    return {
        "competitor": {"id": competitor.id, "name": competitor.name},
        "events": [_event_dict(e) for e in events],
        "events_by_category": by_category,
        "recommendations": recommendations,
        "new_this_week": [_event_dict(e) for e in new_this_week],
        "summary_days": days,
    }


@router.get("/dossier/{competitor_id}/summary")
def summary(request: Request, competitor_id: int, days: int = 7):
    """Per-competitor weekly executive summary."""
    with get_session() as session:
        context = build_summary_context(session, competitor_id, days=days)
        context["last_refreshed"] = get_last_refreshed(session)
        all_competitors = session.query(Competitor).order_by(Competitor.name.asc()).all()
        context["nav_competitors"] = [{"id": c.id, "name": c.name} for c in all_competitors]
    if "error" in context:
        return request.app.state.templates.TemplateResponse(
            "summary.html",
            {"request": request, "error": context["error"], "last_refreshed": context.get("last_refreshed"), "nav_competitors": context.get("nav_competitors", [])},
        )
    return request.app.state.templates.TemplateResponse(
        "summary.html",
        {"request": request, **context},
    )


@router.get("/dossier/{competitor_id}/properties-by-location")
def dossier_properties_by_location(competitor_id: int):
    """Lazy-loaded AI-refined properties by location (JSON). Used when initial page load skipped LLM for speed."""
    with get_session() as session:
        context = build_dossier_context(session, competitor_id, skip_property_llm=False)
    if "error" in context:
        return {"error": context["error"]}
    return {
        "total_properties": context.get("total_properties", 0),
        "properties_by_location": context.get("properties_by_location") or [],
        "other_properties_display": context.get("other_properties_display") or [],
        "location_totals_match": context.get("location_totals_match", True),
    }


@router.get("/dossier/{competitor_id}/executive-summary")
def dossier_executive_summary(competitor_id: int):
    """Lazy-loaded executive summary (JSON). One LLM call only; uses URL-derived locations and state-level topline."""
    from ..config import settings
    if not settings.openai_api_key:
        return {"summary": None, "error": "OPENAI_API_KEY not set"}
    with get_session() as session:
        # Skip property/location LLM so we do exactly one call (summary). State-level aggregation in executive_summary.
        context = build_dossier_context(session, competitor_id, skip_property_llm=True)
    if "error" in context:
        return {"summary": None, "error": context["error"]}
    cache_key = _exec_summary_cache_key(competitor_id, context)
    if cache_key in _EXEC_SUMMARY_CACHE:
        return {"summary": _EXEC_SUMMARY_CACHE[cache_key]}
    summary = generate_executive_summary(context)
    if summary and len(_EXEC_SUMMARY_CACHE) >= _EXEC_SUMMARY_CACHE_MAX:
        _EXEC_SUMMARY_CACHE.clear()
    if summary:
        _EXEC_SUMMARY_CACHE[cache_key] = summary
    return {"summary": summary}


@router.post("/dossier/set-baseline-all-and-refresh", status_code=303)
def set_baseline_all_and_refresh(request: Request):
    """
    Set every competitor's comparison baseline to now, then run all collectors. Use to
    establish a baseline so the next view (or next refresh) shows only changes after
    this point. The run's new snapshots/events will appear as changes since baseline.
    """
    from ..runner import run as run_all_channels, advance_baseline_after_full_refresh

    advance_baseline_after_full_refresh()
    run_all_channels()
    return RedirectResponse(url="/competitors?refreshed=1", status_code=303)


@router.get("/dossier/{competitor_id}")
def dossier(request: Request, competitor_id: int):
    try:
        from ..config import settings
        with get_session() as session:
            # Fast load: skip property/location LLM; refined data lazy-loaded via JS
            context = build_dossier_context(session, competitor_id, skip_property_llm=True)
            context["last_refreshed"] = get_last_refreshed(session)
            all_competitors = session.query(Competitor).order_by(Competitor.name.asc()).all()
            context["nav_competitors"] = [{"id": c.id, "name": c.name} for c in all_competitors]
            context["executive_summary_lazy"] = bool(settings.openai_api_key)
            context["properties_refinement_available"] = bool(settings.openai_api_key)
    except Exception as exc:
        import traceback
        traceback.print_exc()
        return request.app.state.templates.TemplateResponse(
            "dossier.html",
            {
                "request": request,
                "error": f"Dossier failed to load: {exc!s}. Check server logs for details.",
                "last_refreshed": None,
                "nav_competitors": [],
            },
            headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
        )
    _NO_STORE_HEADERS = {"Cache-Control": "no-store, no-cache, must-revalidate"}
    if "error" in context:
        return request.app.state.templates.TemplateResponse(
            "dossier.html",
            {"request": request, "error": context["error"], "last_refreshed": context.get("last_refreshed"), "nav_competitors": context.get("nav_competitors", [])},
            headers=_NO_STORE_HEADERS,
        )
    return request.app.state.templates.TemplateResponse(
        "dossier.html",
        {"request": request, **context},
        headers=_NO_STORE_HEADERS,
    )


@router.post("/dossier/{competitor_id}/refresh", status_code=303)
def dossier_refresh(request: Request, competitor_id: int):
    """
    Run all collectors (asset, talent, press, etc.) for all competitors—same as the cron job.
    After the run, advances every competitor's comparison baseline to now so the executive
    summary and dossier compare to the last refresh (e.g. changes in the last 7 days), not
    the original baseline.
    If form field "force" is set (e.g. "Force full refresh" checked), the latest press
    snapshot for this competitor is deleted before running so the press run is not skipped
    due to hash (ensures Google News and other sources are re-fetched and new code paths run).
    """
    from ..runner import run as run_all_channels, advance_baseline_after_full_refresh, load_latest_snapshot

    with get_session() as session:
        competitor = session.get(Competitor, competitor_id)
        if competitor is None:
            return RedirectResponse(url="/", status_code=303)
        # Force full refresh: clear latest press snapshot so run does not skip on hash.
        if request.form.get("force"):
            latest_press = load_latest_snapshot(session, competitor_id, "press")
            if latest_press:
                session.delete(latest_press)
    run_all_channels()
    advance_baseline_after_full_refresh()
    clear_dossier_caches_for_competitor(competitor_id)
    # Cache-busting query param so the browser doesn't serve a cached dossier page
    ts = int(datetime.now(timezone.utc).timestamp())
    return RedirectResponse(url=f"/dossier/{competitor_id}?r={ts}&refreshed=1", status_code=303)


@router.post("/dossier/{competitor_id}/seed", status_code=303)
def dossier_seed(request: Request, competitor_id: int):
    """
    Set every competitor's comparison baseline to now, then run a full refresh (all
    channels). Use to reset the timeline: the baseline is "before this run", so the
    page after redirect shows the new data as changes since that baseline.
    """
    from ..runner import run as run_all_channels, advance_baseline_after_full_refresh

    with get_session() as session:
        competitor = session.get(Competitor, competitor_id)
        if competitor is None:
            last_refreshed = get_last_refreshed(session)
            all_competitors = session.query(Competitor).order_by(Competitor.name.asc()).all()
            nav_competitors = [{"id": c.id, "name": c.name} for c in all_competitors]
            return request.app.state.templates.TemplateResponse(
                "dossier.html",
                {"request": request, "error": "Competitor not found.", "last_refreshed": last_refreshed, "nav_competitors": nav_competitors},
            )

    # Set baseline first so the run's new snapshots/events count as "since baseline"
    advance_baseline_after_full_refresh()
    run_all_channels()
    clear_dossier_caches_for_competitor(competitor_id)
    ts = int(datetime.now(timezone.utc).timestamp())
    return RedirectResponse(url=f"/dossier/{competitor_id}?r={ts}&refreshed=1", status_code=303)

