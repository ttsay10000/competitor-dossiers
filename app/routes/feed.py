from typing import Optional

from fastapi import APIRouter, Request

from ..db import get_session, get_last_refreshed
from ..models import Competitor, Event, RunLog
from ..digest import build_weekly_digest
from ..utils import to_eastern

router = APIRouter()


def _run_log_summary(log) -> dict:
    """Serializable RunLog summary so templates don't touch detached ORM."""
    return {
        "status": log.status,
        "created_at_str": to_eastern(log.created_at),
    }


@router.get("/feed")
def feed(request: Request, competitor_id: Optional[int] = None, severity: Optional[str] = None, category: Optional[str] = None, show_low: int = 0):
    with get_session() as session:
        competitors = session.query(Competitor).order_by(Competitor.created_at.desc()).all()
        recent_logs = session.query(RunLog).order_by(RunLog.created_at.desc()).limit(100).all()
        last_runs: dict = {}
        for log in recent_logs:
            if log.channel not in last_runs:
                last_runs[log.channel] = _run_log_summary(log)
        query = session.query(Event).order_by(Event.detected_at.desc())

        if competitor_id:
            query = query.filter(Event.competitor_id == competitor_id)
        if severity:
            query = query.filter(Event.severity == severity)
        elif not show_low:
            query = query.filter(Event.severity != "low")
        if category:
            query = query.filter(Event.category == category)

        events_rows = query.limit(200).all()
        events = [
            {
                "title": e.title,
                "summary": e.summary,
                "severity": e.severity,
                "category": e.category,
                "type": e.type,
                "detected_at_str": e.detected_at.strftime("%Y-%m-%d"),
                "why_it_matters": e.why_it_matters,
                "evidence_json": e.evidence_json,
            }
            for e in events_rows
        ]
        competitors_data = [{"id": c.id, "name": c.name, "created_at": c.created_at} for c in competitors]
        nav_competitors = competitors_data
        last_refreshed = get_last_refreshed(session)

    return request.app.state.templates.TemplateResponse(
        "feed.html",
        {
            "request": request,
            "events": events,
            "competitors": competitors_data,
            "nav_competitors": nav_competitors,
            "selected_competitor": competitor_id,
            "selected_severity": severity,
            "selected_category": category,
            "show_low": bool(show_low),
            "last_runs": last_runs,
            "last_refreshed": last_refreshed,
        },
    )


@router.get("/digest")
def digest(request: Request):
    digest_text = build_weekly_digest()
    with get_session() as session:
        recent_logs = session.query(RunLog).order_by(RunLog.created_at.desc()).limit(100).all()
        last_runs = {}
        for log in recent_logs:
            if log.channel not in last_runs:
                last_runs[log.channel] = _run_log_summary(log)
        all_competitors = session.query(Competitor).order_by(Competitor.created_at.desc()).all()
        nav_competitors = [{"id": c.id, "name": c.name, "created_at": c.created_at} for c in all_competitors]
        last_refreshed = get_last_refreshed(session)
    return request.app.state.templates.TemplateResponse(
        "digest.html",
        {"request": request, "digest_text": digest_text, "last_runs": last_runs, "last_refreshed": last_refreshed, "nav_competitors": nav_competitors},
    )
