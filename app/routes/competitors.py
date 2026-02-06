from typing import Optional

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from starlette.status import HTTP_303_SEE_OTHER

from ..db import get_session, get_last_refreshed
from ..models import Competitor, SourceEndpoint, RunLog

router = APIRouter()


@router.get("/")
def root():
    return RedirectResponse(url="/feed", status_code=HTTP_303_SEE_OTHER)


@router.get("/competitors")
def competitors_list(request: Request):
    with get_session() as session:
        competitors = session.query(Competitor).order_by(Competitor.name.asc()).all()
        last_refreshed = get_last_refreshed(session)
    return request.app.state.templates.TemplateResponse(
        "competitors.html",
        {"request": request, "competitors": competitors, "last_refreshed": last_refreshed},
    )


@router.post("/competitors")
def competitors_create(
    name: str = Form(...),
    primary_domain: Optional[str] = Form(None),
):
    with get_session() as session:
        competitor = Competitor(name=name.strip(), primary_domain=(primary_domain or "").strip() or None)
        session.add(competitor)
    return RedirectResponse(url="/competitors", status_code=HTTP_303_SEE_OTHER)


@router.get("/competitors/{competitor_id}")
def competitors_edit(request: Request, competitor_id: int):
    with get_session() as session:
        competitor = session.get(Competitor, competitor_id)
        if competitor is None:
            return RedirectResponse(url="/competitors", status_code=HTTP_303_SEE_OTHER)
        endpoints = list(competitor.source_endpoints)
        recent_logs = (
            session.query(RunLog)
            .filter(RunLog.competitor_id == competitor_id)
            .order_by(RunLog.created_at.desc())
            .limit(100)
            .all()
        )
        last_runs: dict[str, RunLog] = {}
        for log in recent_logs:
            if log.channel not in last_runs:
                last_runs[log.channel] = log
        last_refreshed = get_last_refreshed(session)
    return request.app.state.templates.TemplateResponse(
        "competitor_edit.html",
        {"request": request, "competitor": competitor, "endpoints": endpoints, "last_runs": last_runs, "last_refreshed": last_refreshed},
    )


@router.post("/competitors/{competitor_id}")
def competitors_update(
    competitor_id: int,
    name: str = Form(...),
    primary_domain: Optional[str] = Form(None),
):
    with get_session() as session:
        competitor = session.get(Competitor, competitor_id)
        if competitor is None:
            return RedirectResponse(url="/competitors", status_code=HTTP_303_SEE_OTHER)
        competitor.name = name.strip()
        competitor.primary_domain = (primary_domain or "").strip() or None
    return RedirectResponse(url=f"/competitors/{competitor_id}", status_code=HTTP_303_SEE_OTHER)


@router.post("/competitors/{competitor_id}/sources")
def competitor_add_source(
    competitor_id: int,
    channel: str = Form(...),
    url: str = Form(...),
    confidence: str = Form("high"),
    js_required: Optional[str] = Form(None),
    use_sitemap_first: Optional[str] = Form(None),
):
    with get_session() as session:
        competitor = session.get(Competitor, competitor_id)
        if competitor is None:
            return RedirectResponse(url="/competitors", status_code=HTTP_303_SEE_OTHER)
        endpoint = SourceEndpoint(
            competitor_id=competitor_id,
            channel=channel.strip(),
            url=url.strip(),
            confidence=confidence.strip() or "high",
            js_required=bool(js_required),
            use_sitemap_first=bool(use_sitemap_first),
        )
        session.add(endpoint)
    return RedirectResponse(url=f"/competitors/{competitor_id}", status_code=HTTP_303_SEE_OTHER)


@router.post("/competitors/{competitor_id}/sources/{source_id}/delete")
def competitor_delete_source(competitor_id: int, source_id: int):
    with get_session() as session:
        endpoint = session.get(SourceEndpoint, source_id)
        if endpoint is not None:
            session.delete(endpoint)
    return RedirectResponse(url=f"/competitors/{competitor_id}", status_code=HTTP_303_SEE_OTHER)


@router.post("/competitors/{competitor_id}/sources/{source_id}")
def competitor_update_source(
    competitor_id: int,
    source_id: int,
    channel: str = Form(...),
    url: str = Form(...),
    confidence: str = Form("high"),
    js_required: Optional[str] = Form(None),
    use_sitemap_first: Optional[str] = Form(None),
):
    with get_session() as session:
        endpoint = session.get(SourceEndpoint, source_id)
        if endpoint is None:
            return RedirectResponse(url=f"/competitors/{competitor_id}", status_code=HTTP_303_SEE_OTHER)
        endpoint.channel = channel.strip()
        endpoint.url = url.strip()
        endpoint.confidence = confidence.strip() or "high"
        endpoint.js_required = bool(js_required)
        endpoint.use_sitemap_first = bool(use_sitemap_first)
    return RedirectResponse(url=f"/competitors/{competitor_id}", status_code=HTTP_303_SEE_OTHER)
