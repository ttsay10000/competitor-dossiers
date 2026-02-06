from typing import Optional

from fastapi import APIRouter, Request

from ..db import get_session, get_last_refreshed
from ..models import Competitor, RunLog

router = APIRouter()


@router.get("/runs")
def runs(request: Request, competitor_id: Optional[int] = None, channel: Optional[str] = None, status: Optional[str] = None):
    with get_session() as session:
        competitors = session.query(Competitor).order_by(Competitor.name.asc()).all()
        competitor_names = {competitor.id: competitor.name for competitor in competitors}
        query = session.query(RunLog).order_by(RunLog.created_at.desc())
        if competitor_id:
            query = query.filter(RunLog.competitor_id == competitor_id)
        if channel:
            query = query.filter(RunLog.channel == channel)
        if status:
            query = query.filter(RunLog.status == status)
        logs_rows = query.limit(200).all()
        logs = [
            {
                "created_at_str": log.created_at.strftime("%Y-%m-%d %H:%M"),
                "competitor_id": log.competitor_id,
                "channel": log.channel,
                "status": log.status,
                "message": log.message,
            }
            for log in logs_rows
        ]
        last_refreshed = get_last_refreshed(session)
        nav_competitors = [{"id": c.id, "name": c.name} for c in competitors]

    return request.app.state.templates.TemplateResponse(
        "runs.html",
        {
            "request": request,
            "logs": logs,
            "competitors": competitors,
            "competitor_names": competitor_names,
            "nav_competitors": nav_competitors,
            "selected_competitor": competitor_id,
            "selected_channel": channel,
            "selected_status": status,
            "last_refreshed": last_refreshed,
        },
    )
