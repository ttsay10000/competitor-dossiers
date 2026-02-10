from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from ..db import get_session, get_last_refreshed
from ..models import Competitor, Event, Snapshot, Capability
from ..diff.asset_diff import diff_properties, delta_by_city, infer_location_for_property, is_location_treated_as_other, parse_keys_from_details
from ..executive_summary import generate_executive_summary, clean_location_display_for_dossier
from ..llm_structured import assign_states_to_properties_for_dossier
from ..rules.talent_rules import job_functional_area, FUNCTIONAL_AREA_DISPLAY_ORDER, PROPERTY_OPERATIONS_LABEL

router = APIRouter()

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


def build_dossier_context(session, competitor_id: int) -> dict:
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

    events = (
        session.query(Event)
        .filter(Event.competitor_id == competitor_id, Event.detected_at >= cutoff)
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
        events = [e for e in events if e.detected_at.date() >= baseline_cutoff.date()]
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
    # Run all properties through LLM to assign state per property, then bucket by state for location bullets.
    enriched_props = assign_states_to_properties_for_dossier(competitor.name, asset_props)
    if enriched_props is not None:
        asset_props = enriched_props
    # Inferred locations (LLM-set state or URL-derived) for summary and counts
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

    # Canonical press list for last 120 days, with display-ready date strings, sorted by date published (newest first).
    # Exclude irrelevant and promo so we only show business-relevant news.
    EXCLUDED_TOPICS = {"irrelevant", "promo_or_brand_marketing"}
    press_90d = []
    now = datetime.now(timezone.utc)
    press_cutoff = now - timedelta(days=120)
    for item in canonical_press:
        topic = (item.get("topic") or "").strip().lower()
        if topic in EXCLUDED_TOPICS:
            continue
        dt = _parse_press_date(item.get("date"))
        if dt and dt < press_cutoff:
            continue
        display_date = dt.strftime("%Y-%m-%d") if dt else None
        out = dict(item)
        if display_date:
            out["date"] = display_date
        out["_sort_dt"] = dt  # for sorting; removed before template
        press_90d.append(out)
    press_90d.sort(key=lambda x: (x["_sort_dt"] is None, -(x["_sort_dt"].timestamp() if x["_sort_dt"] else 0)))
    for p in press_90d:
        p.pop("_sort_dt", None)

    # Top news: up to 5 most newsworthy articles. Press releases (e.g. PR Newswire) and strategic topics rank highest; recency breaks ties.
    STRONG_BUSINESS_TOPICS = {
        "fundraising", "restructuring_or_layoffs", "executive_interview",
        "new_partnership", "new_hotel_opening", "other_business",
    }
    # Tiers 5/4/3 treated equally (strategic topics); other_business slightly lower. Unlikely to have many in a week.
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
        if (item.get("topic") or "").lower() in STRONG_BUSINESS_TOPICS
    ]
    # When reporting baseline is set (e.g. after "Reset baseline"), only news on or after that date counts.
    if reporting_baseline_date:
        pool = [
            item for item in pool
            if _parse_press_date(item.get("date")) and _parse_press_date(item.get("date")).date() >= baseline_cutoff.date()
        ]
    week_cutoff_pub = now - timedelta(days=7)

    def _newsworthiness_score(item: dict) -> tuple:
        topic = (item.get("topic") or "").lower()
        topic_score = TOPIC_NEWSWORTHINESS.get(topic, 0)
        url = (item.get("url") or item.get("link") or "").lower()
        outlet = (item.get("outlet") or "").lower()
        is_press_release = "prnewswire" in url or "prnewswire" in outlet
        source_score = 10 if is_press_release else 0  # press releases = highest priority
        dt = _parse_press_date(item.get("date"))
        recency_score = 5 if (dt and dt >= week_cutoff_pub) else 0  # last 7 days boost
        score = source_score + topic_score + recency_score
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

    # Optional: LLM-cleaned location display (State - City, group noise as Other); preserves keys.
    cleaned = clean_location_display_for_dossier(
        competitor.name, properties_by_location, asset_delta_by_city
    )
    if cleaned:
        properties_by_location = cleaned.get("properties_by_location") or properties_by_location
        asset_delta_by_city = cleaned.get("asset_delta_by_city") or asset_delta_by_city
        # Use same cleaned list for single-bullet display (one line per location: "Location - N properties (M keys)")
        properties_by_location_with_list = [
            {"location": r["location"], "count": r["count"], "keys": r.get("keys", 0)}
            for r in properties_by_location
        ]

    # Expand "Other" (non-state) into subbullets with location for quick check (no separate LLM).
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
        "takeaways": takeaways,
        "recommendations": recommendations,
        "events_this_week": events_this_week_dicts,
        "top_news": top_news,
        "properties_by_location": properties_by_location,
        "properties_by_location_with_list": properties_by_location_with_list,
        "total_properties": total_properties,
        "asset_baseline_date": asset_baseline_date,
        "asset_added_since_baseline": asset_added_since_baseline,
        "asset_removed_since_baseline": asset_removed_since_baseline,
        "asset_delta_by_city": asset_delta_by_city,
        "other_properties_display": other_properties_display,
        "comparison_baseline_date": reporting_baseline_date,
    }
    context["executive_summary"] = generate_executive_summary(context)
    return context


def build_summary_context(session, competitor_id: int, days: int = 7) -> dict:
    """Weekly executive summary: high-signal events from last N days, grouped by category."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    competitor = session.get(Competitor, competitor_id)
    if competitor is None:
        return {"error": "Competitor not found."}

    events = (
        session.query(Event)
        .filter(Event.competitor_id == competitor_id, Event.detected_at >= cutoff)
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


@router.get("/dossier/{competitor_id}")
def dossier(request: Request, competitor_id: int):
    try:
        with get_session() as session:
            context = build_dossier_context(session, competitor_id)
            context["last_refreshed"] = get_last_refreshed(session)
            all_competitors = session.query(Competitor).order_by(Competitor.name.asc()).all()
            context["nav_competitors"] = [{"id": c.id, "name": c.name} for c in all_competitors]
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
        )
    if "error" in context:
        return request.app.state.templates.TemplateResponse(
            "dossier.html",
            {"request": request, "error": context["error"], "last_refreshed": context.get("last_refreshed"), "nav_competitors": context.get("nav_competitors", [])},
        )
    return request.app.state.templates.TemplateResponse(
        "dossier.html",
        {"request": request, **context},
    )


@router.post("/dossier/{competitor_id}/refresh", status_code=303)
def dossier_refresh(request: Request, competitor_id: int):
    """
    Run all collectors (asset, talent, press, etc.) for all competitors—same as the cron job.
    Does not change the reporting baseline. Use this to pull latest data and generate events
    compared to the existing baseline, then view the updated dossier.
    """
    from ..runner import run as run_all_channels

    with get_session() as session:
        competitor = session.get(Competitor, competitor_id)
        if competitor is None:
            return RedirectResponse(url="/", status_code=303)
    run_all_channels()
    return RedirectResponse(url=f"/dossier/{competitor_id}", status_code=303)


@router.post("/dossier/{competitor_id}/seed", status_code=303)
def dossier_seed(request: Request, competitor_id: int):
    """
    Manual seed/baseline reset for a single competitor.

    Does three things:
    1) Refreshes data using the normal runner (all channels, all competitors).
    2) Sets a reporting baseline timestamp for this competitor so future weekly
       summaries treat only post-baseline items as "new".
    3) Effectively resets the visible timeline in the dossier because events
       and top news are filtered to post-baseline dates.
    """
    from ..runner import run as run_all_channels

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
        # Set baseline to "now" so subsequent views only surface post-baseline changes.
        competitor.reporting_baseline_at = datetime.now(timezone.utc)
        session.add(competitor)
        session.commit()

    # Run a fresh pull for all channels (includes this competitor).
    # This keeps logic consistent with the scheduled cron job.
    run_all_channels()

    return RedirectResponse(url=f"/dossier/{competitor_id}", status_code=303)

