from typing import Optional

from fastapi import APIRouter, Request

from ..db import get_session, get_last_refreshed
from ..models import Competitor, Event, RunLog
from ..digest import build_weekly_digest

router = APIRouter()


@router.get("/feed")
def feed(request: Request, competitor_id: Optional[int] = None, severity: Optional[str] = None, category: Optional[str] = None, show_low: int = 0):
    with get_session() as session:
        competitors = session.query(Competitor).order_by(Competitor.name.asc()).all()
        recent_logs = session.query(RunLog).order_by(RunLog.created_at.desc()).limit(100).all()
        last_runs: dict[str, RunLog] = {}
        for log in recent_logs:
            if log.channel not in last_runs:
                last_runs[log.channel] = log
        query = session.query(Event).order_by(Event.detected_at.desc())

        if competitor_id:
            query = query.filter(Event.competitor_id == competitor_id)
        if severity:
            query = query.filter(Event.severity == severity)
        elif not show_low:
            query = query.filter(Event.severity != "low")
        if category:
            query = query.filter(Event.category == category)

        events = query.limit(200).all()
        last_refreshed = get_last_refreshed(session)

    return request.app.state.templates.TemplateResponse(
        "feed.html",
        {
            "request": request,
            "events": events,
            "competitors": competitors,
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
        last_runs: dict[str, RunLog] = {}
        for log in recent_logs:
            if log.channel not in last_runs:
                last_runs[log.channel] = log
        last_refreshed = get_last_refreshed(session)
    return request.app.state.templates.TemplateResponse(
        "digest.html",
        {"request": request, "digest_text": digest_text, "last_runs": last_runs, "last_refreshed": last_refreshed},
    )
