from typing import Optional

from fastapi import APIRouter, Request

from ..db import get_session
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
        logs = query.limit(200).all()

    return request.app.state.templates.TemplateResponse(
        "runs.html",
        {
            "request": request,
            "logs": logs,
            "competitors": competitors,
            "competitor_names": competitor_names,
            "selected_competitor": competitor_id,
            "selected_channel": channel,
            "selected_status": status,
        },
    )
