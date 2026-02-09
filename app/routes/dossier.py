from datetime import datetime, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import Response

from ..db import get_session, get_last_refreshed
from ..models import Competitor, Event, Snapshot, Capability
from ..diff.asset_diff import diff_properties, delta_by_city
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


def _event_dict(e) -> dict:
    """Serializable event for templates (avoids DetachedInstanceError)."""
    return {
        "title": e.title,
        "summary": e.summary,
        "severity": e.severity,
        "category": e.category,
        "type": e.type,
        "detected_at_str": e.detected_at.strftime("%Y-%m-%d"),
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
    cutoff = datetime.utcnow() - timedelta(days=90)
    competitor = session.get(Competitor, competitor_id)
    if competitor is None:
        return {"error": "Competitor not found."}

    latest_asset = (
        session.query(Snapshot)
        .filter(Snapshot.competitor_id == competitor_id, Snapshot.channel == "asset")
        .order_by(Snapshot.captured_at.desc())
        .first()
    )
    latest_talent = (
        session.query(Snapshot)
        .filter(Snapshot.competitor_id == competitor_id, Snapshot.channel == "talent")
        .order_by(Snapshot.captured_at.desc())
        .first()
    )
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

    capabilities = (
        session.query(Capability)
        .filter(Capability.competitor_id == competitor_id)
        .order_by(Capability.first_seen_at.asc())
        .all()
    )

    raw_asset_props = (latest_asset.structured_json or {}).get("properties", []) if latest_asset else []
    asset_props = [p for p in raw_asset_props if isinstance(p, dict)]
    markets = sorted({prop.get("market") for prop in asset_props if prop.get("market")})

    raw_talent_jobs = (latest_talent.structured_json or {}).get("jobs", []) if latest_talent else []
    talent_jobs = [j for j in raw_talent_jobs if isinstance(j, dict)]

    # Summarize jobs by functional area; split into business vs property operations.
    jobs_by_function = []
    jobs_by_function_property = []
    by_func: dict[str, list[dict]] = {}
    for job in talent_jobs:
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

    takeaways = []
    if any(event.type == "asset.new_market" for event in events):
        takeaways.append("Recent footprint expansion activity detected.")
    if any(event.type == "talent.hiring_surge" for event in events):
        takeaways.append("Hiring surge suggests strategic buildout.")
    if any(event.category == "partner" for event in events):
        takeaways.append("Partnership activity indicates distribution focus.")
    if not takeaways:
        takeaways.append("No major strategic shifts detected in the last 90 days.")

    recommendations = build_recommendations(events)

    week_cutoff = datetime.utcnow() - timedelta(days=7)
    events_this_week = [e for e in events if e.detected_at >= week_cutoff]
    events_this_week_dicts = [_event_dict(e) for e in events_this_week]

    # Major events (past 7 days) in four buckets for the top of the dossier.
    major_events_buckets = {
        "Talent Radar": [],
        "Asset Watch": [],
        "Digital Footprint": [],
        "Public Record": [],
    }
    for e in events_this_week_dicts:
        t = e.get("type") or ""
        cat = e.get("category") or ""
        if t.startswith("talent."):
            major_events_buckets["Talent Radar"].append(e)
        elif t.startswith("asset."):
            major_events_buckets["Asset Watch"].append(e)
        elif t in ("narrative.homepage_updated", "asset.pipeline_signal"):
            major_events_buckets["Digital Footprint"].append(e)
        elif cat == "public_record" or t == "public_record.filing" or cat in ("partner", "capital") or t == "narrative.priority_shift":
            major_events_buckets["Public Record"].append(e)

    # Top 5 news: exclude blog-like; prefer major business topics (fundraising, partnerships, markets).
    blog_like = ("blog", "post", "update:", "weekly", "monthly")
    relevance_hints = ("fundraise", "funding", "partnership", "acquisition", "expansion", "launch", "series", "market", "executive", "ceo", "strategic")
    candidates = []
    for item in press_items:
        title = (item.get("title") or "").lower()
        link = (item.get("link") or item.get("url") or "").lower()
        if any(x in title or x in link for x in blog_like):
            continue
        score = sum(1 for h in relevance_hints if h in title)
        candidates.append((score, item))
    candidates.sort(key=lambda x: -x[0])
    top_news = [item for _, item in candidates[:5]]

    # Summary of business points (bullets for at-a-glance).
    summary_business_points = []
    if markets:
        summary_business_points.append("Markets: " + ", ".join(markets))
    if capabilities:
        summary_business_points.append("Capabilities: " + ", ".join(c.capability for c in capabilities))
    if any(e.get("type") == "asset.new_market" for e in events_this_week_dicts):
        summary_business_points.append("New market(s) added this week.")
    if any(e.get("type", "").startswith("talent.") for e in events_this_week_dicts):
        summary_business_points.append("Talent activity this week.")
    if not summary_business_points:
        summary_business_points.append("No major business updates in the last 90 days.")

    # Properties by location (city/market and count) from asset snapshot.
    location_counts = {}
    for prop in asset_props:
        loc = (prop.get("market") or prop.get("location") or "Unspecified").strip() or "Unspecified"
        location_counts[loc] = location_counts.get(loc, 0) + 1
    properties_by_location = [{"location": loc, "count": n} for loc, n in sorted(location_counts.items(), key=lambda x: (-x[1], x[0]))]

    # Baseline = oldest asset snapshot (first run). Week-over-week: compare latest to baseline.
    asset_baseline_date = None
    asset_added_since_baseline = 0
    asset_removed_since_baseline = 0
    asset_delta_by_city = []
    if latest_asset:
        baseline_asset = (
            session.query(Snapshot)
            .filter(Snapshot.competitor_id == competitor_id, Snapshot.channel == "asset")
            .order_by(Snapshot.captured_at.asc())
            .first()
        )
        if baseline_asset and baseline_asset.id != latest_asset.id:
            raw_baseline = (baseline_asset.structured_json or {}).get("properties", [])
            baseline_props = [p for p in raw_baseline if isinstance(p, dict)]
            diff = diff_properties(baseline_props, asset_props)
            asset_added_since_baseline = len(diff["added"])
            asset_removed_since_baseline = len(diff["removed"])
            asset_baseline_date = baseline_asset.captured_at.strftime("%Y-%m-%d")
            asset_delta_by_city = delta_by_city(diff["added"], diff["removed"])

    return {
        "competitor": {"id": competitor.id, "name": competitor.name},
        "markets": markets,
        "capabilities": [{"capability": c.capability} for c in capabilities],
        "events": [_event_dict(e) for e in events],
        "talent_jobs": talent_jobs,
        "jobs_by_function": jobs_by_function,
        "jobs_by_function_property": jobs_by_function_property,
        "press_items": press_items,
        "takeaways": takeaways,
        "recommendations": recommendations,
        "major_events_buckets": major_events_buckets,
        "events_this_week": events_this_week_dicts,
        "top_news": top_news,
        "summary_business_points": summary_business_points,
        "properties_by_location": properties_by_location,
        "asset_baseline_date": asset_baseline_date,
        "asset_added_since_baseline": asset_added_since_baseline,
        "asset_removed_since_baseline": asset_removed_since_baseline,
        "asset_delta_by_city": asset_delta_by_city,
    }


def build_summary_context(session, competitor_id: int, days: int = 7) -> dict:
    """Weekly executive summary: high-signal events from last N days, grouped by category."""
    cutoff = datetime.utcnow() - timedelta(days=days)
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
    with get_session() as session:
        context = build_dossier_context(session, competitor_id)
        context["last_refreshed"] = get_last_refreshed(session)
        all_competitors = session.query(Competitor).order_by(Competitor.name.asc()).all()
        context["nav_competitors"] = [{"id": c.id, "name": c.name} for c in all_competitors]
    if "error" in context:
        return request.app.state.templates.TemplateResponse(
            "dossier.html",
            {"request": request, "error": context["error"], "last_refreshed": context.get("last_refreshed"), "nav_competitors": context.get("nav_competitors", [])},
        )
    return request.app.state.templates.TemplateResponse(
        "dossier.html",
        {"request": request, **context},
    )


@router.get("/dossier/{competitor_id}/pdf")
def dossier_pdf(request: Request, competitor_id: int):
    from weasyprint import HTML

    with get_session() as session:
        context = build_dossier_context(session, competitor_id)
        context["last_refreshed"] = get_last_refreshed(session)
    if "error" in context:
        return request.app.state.templates.TemplateResponse(
            "dossier.html",
            {"request": request, "error": context["error"]},
        )

    html_content = request.app.state.templates.TemplateResponse(
        "dossier.html",
        {"request": request, **context},
    ).body.decode("utf-8")
    pdf = HTML(string=html_content, base_url=str(request.base_url)).write_pdf()
    filename = f"dossier_{competitor_id}.pdf"
    return Response(pdf, media_type="application/pdf", headers={"Content-Disposition": f"attachment; filename={filename}"})
