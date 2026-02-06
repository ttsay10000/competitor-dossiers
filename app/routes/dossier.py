from datetime import datetime, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import Response

from ..db import get_session, get_last_refreshed
from ..models import Competitor, Event, Snapshot, Capability

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

    asset_props = (latest_asset.structured_json or {}).get("properties", []) if latest_asset else []
    markets = sorted({prop.get("market") for prop in asset_props if prop.get("market")})

    talent_jobs = (latest_talent.structured_json or {}).get("jobs", []) if latest_talent else []

    press_items = (latest_press.structured_json or {}).get("items", []) if latest_press else []

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

    # New this week (asset-related events in last 7 days) for operational signals.
    week_cutoff = datetime.utcnow() - timedelta(days=7)
    events_this_week = [e for e in events if e.detected_at >= week_cutoff]
    new_this_week = [e for e in events_this_week if e.category == "asset" or (e.type or "").startswith("asset.")]

    return {
        "competitor": {"id": competitor.id, "name": competitor.name},
        "markets": markets,
        "capabilities": [{"capability": c.capability} for c in capabilities],
        "events": [_event_dict(e) for e in events],
        "talent_jobs": talent_jobs,
        "press_items": press_items,
        "takeaways": takeaways,
        "recommendations": recommendations,
        "new_this_week": [_event_dict(e) for e in new_this_week],
        "events_this_week": [_event_dict(e) for e in events_this_week],
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
    if "error" in context:
        return request.app.state.templates.TemplateResponse(
            "summary.html",
            {"request": request, "error": context["error"], "last_refreshed": context.get("last_refreshed")},
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
    if "error" in context:
        return request.app.state.templates.TemplateResponse(
            "dossier.html",
            {"request": request, "error": context["error"], "last_refreshed": context.get("last_refreshed")},
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
