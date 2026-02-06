from datetime import datetime, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import Response

from ..db import get_session
from ..models import Competitor, Event, Snapshot, Capability

router = APIRouter()


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

    return {
        "competitor": competitor,
        "markets": markets,
        "capabilities": capabilities,
        "events": events,
        "talent_jobs": talent_jobs,
        "press_items": press_items,
        "takeaways": takeaways,
    }


@router.get("/dossier/{competitor_id}")
def dossier(request: Request, competitor_id: int):
    with get_session() as session:
        context = build_dossier_context(session, competitor_id)
    if "error" in context:
        return request.app.state.templates.TemplateResponse(
            "dossier.html",
            {"request": request, "error": context["error"]},
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
