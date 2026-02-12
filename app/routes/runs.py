from typing import Any, Optional

from fastapi import APIRouter, Request

from ..db import get_session, get_last_refreshed
from ..models import Competitor, RunLog

router = APIRouter()


def _extra_str(extra: Any) -> str:
    """Safely format RunLog.extra_json for display; handles None, non-dict, or dict."""
    if extra is None:
        return ""
    if not isinstance(extra, dict):
        return str(extra)[:200] if extra else ""
    return " ".join(f"{k}: {v}" for k, v in extra.items())


@router.get("/runs")
def runs(
    request: Request,
    competitor_id: Optional[int] = None,
    channel: Optional[str] = None,
    status: Optional[str] = None,
    started: Optional[int] = None,
):
    with get_session() as session:
        competitors = session.query(Competitor).order_by(Competitor.created_at.desc()).all()
        competitor_names = {c.id: c.name for c in competitors}
        competitor_options = [{"id": c.id, "name": c.name, "created_at": c.created_at} for c in competitors]
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
                "extra_str": _extra_str(log.extra_json),
            }
            for log in logs_rows
        ]
        last_refreshed = get_last_refreshed(session)
        nav_competitors = competitor_options
        run_started_name = None
        if started and competitor_id and competitor_id in competitor_names:
            run_started_name = competitor_names[competitor_id]

    return request.app.state.templates.TemplateResponse(
        "runs.html",
        {
            "request": request,
            "logs": logs,
            "competitor_options": competitor_options,
            "competitor_names": competitor_names,
            "nav_competitors": nav_competitors,
            "selected_competitor": competitor_id,
            "selected_channel": channel,
            "selected_status": status,
            "last_refreshed": last_refreshed,
            "run_started_name": run_started_name,
        },
    )
