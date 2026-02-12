import logging
import threading
from urllib.parse import urlencode
from typing import Optional

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.exc import IntegrityError
from starlette.status import HTTP_303_SEE_OTHER

from sqlalchemy import func

from ..db import get_session, get_last_refreshed
from ..models import Competitor, SourceEndpoint, RunLog, Snapshot

router = APIRouter()

# Main channels for status display
DISPLAY_CHANNELS = ("talent", "asset", "press")


def _sync_seed_file() -> None:
    """After any competitor/source change, write DB to seed_data.json so UI additions persist. Never fail the request."""
    try:
        from ..seed import export_seed_to_file
        export_seed_to_file()
    except Exception as e:
        logging.warning("Failed to sync seed_data.json after competitor change: %s", e, exc_info=True)


@router.get("/")
def root():
    return RedirectResponse(url="/competitors", status_code=HTTP_303_SEE_OTHER)


def _competitor_status(session, competitor_id: int) -> dict:
    """Return has_snapshots per channel and last_runs per channel for a competitor."""
    has_snapshots = {ch: False for ch in DISPLAY_CHANNELS}
    subq = (
        session.query(Snapshot.channel, func.count(Snapshot.id))
        .filter(Snapshot.competitor_id == competitor_id, Snapshot.channel.in_(DISPLAY_CHANNELS))
        .group_by(Snapshot.channel)
        .all()
    )
    for ch, cnt in subq:
        if cnt and cnt > 0:
            has_snapshots[ch] = True

    last_runs = {}
    for log in (
        session.query(RunLog)
        .filter(RunLog.competitor_id == competitor_id, RunLog.channel.in_(DISPLAY_CHANNELS))
        .order_by(RunLog.created_at.desc())
    ):
        if log.channel not in last_runs:
            last_runs[log.channel] = {
                "status": log.status,
                "created_at_str": log.created_at.strftime("%Y-%m-%d %H:%M"),
            }
    return {"has_snapshots": has_snapshots, "last_runs": last_runs}


@router.get("/competitors")
def competitors_list(request: Request):
    with get_session() as session:
        competitors = session.query(Competitor).order_by(Competitor.name.asc()).all()
        competitors_data = []
        for c in competitors:
            status = _competitor_status(session, c.id)
            competitors_data.append({
                "id": c.id,
                "name": c.name,
                "primary_domain": c.primary_domain,
                "has_snapshots": status["has_snapshots"],
                "last_runs": status["last_runs"],
            })
        last_refreshed = get_last_refreshed(session)
    nav_competitors = [{"id": c["id"], "name": c["name"]} for c in competitors_data]
    return request.app.state.templates.TemplateResponse(
        "competitors.html",
        {"request": request, "competitors": competitors_data, "nav_competitors": nav_competitors, "last_refreshed": last_refreshed},
    )


@router.get("/competitors/new")
def competitors_new(request: Request):
    error = request.query_params.get("error")
    error_message = None
    if error == "duplicate":
        error_message = "A competitor with that name already exists. Use a different name or edit the existing competitor."
    elif error == "server":
        error_message = "Something went wrong saving the competitor. Please try again."
    with get_session() as session:
        last_refreshed = get_last_refreshed(session)
    return request.app.state.templates.TemplateResponse(
        "competitor_new.html",
        {"request": request, "last_refreshed": last_refreshed, "error_message": error_message},
    )


@router.post("/competitors")
async def competitors_create(request: Request):
    try:
        form = await request.form()
        name = (form.get("name") or "").strip()
        if not name:
            return RedirectResponse(url="/competitors/new", status_code=HTTP_303_SEE_OTHER)
        primary_domain = (form.get("primary_domain") or "").strip() or None
        talent_urls = form.getlist("talent_urls")
        asset_urls = form.getlist("asset_urls")
        press_urls = form.getlist("press_urls")
        with get_session() as session:
            competitor = Competitor(name=name, primary_domain=primary_domain)
            session.add(competitor)
            session.flush()

            def _add_sources(urls, channel: str) -> None:
                for url in urls:
                    if not url:
                        continue
                    clean = url.strip()
                    if not clean:
                        continue
                    endpoint = SourceEndpoint(
                        competitor_id=competitor.id,
                        channel=channel,
                        url=clean,
                        confidence="high",
                        js_required=False,
                        use_sitemap_first=False,
                    )
                    session.add(endpoint)

            _add_sources(talent_urls, "talent")
            _add_sources(asset_urls, "asset")
            _add_sources(press_urls, "press")

            new_id = competitor.id
        _sync_seed_file()
        return RedirectResponse(url=f"/competitors/{new_id}/added", status_code=HTTP_303_SEE_OTHER)
    except IntegrityError:
        return RedirectResponse(
            url="/competitors/new?" + urlencode({"error": "duplicate"}),
            status_code=HTTP_303_SEE_OTHER,
        )
    except Exception as e:
        logging.exception("Failed to create competitor: %s", e)
        return RedirectResponse(
            url="/competitors/new?" + urlencode({"error": "server"}),
            status_code=HTTP_303_SEE_OTHER,
        )


def _parse_channels(value: Optional[str]) -> Optional[list[str]]:
    """Parse comma-separated channels; return None if empty/None (run all). Valid: talent, asset, press, homepage, public_records."""
    if not value or not str(value).strip():
        return None
    from ..runner import RUNNER_CHANNELS
    raw = [c.strip().lower() for c in str(value).split(",") if c.strip()]
    valid = [c for c in raw if c in RUNNER_CHANNELS]
    return valid if valid else None


@router.post("/competitors/{competitor_id}/run-now")
async def competitor_run_now(request: Request, competitor_id: int):
    """Start data collection for this competitor in the background. Optional form/query: channels=talent,asset,press or channels[]."""
    channels = None
    form = await request.form()
    if form:
        # Form can send multiple channels (checkboxes) or single comma-separated
        ch_list = form.getlist("channels")
        if ch_list:
            channels = _parse_channels(",".join(ch_list))
        else:
            channels = _parse_channels(form.get("channels"))
    if channels is None:
        channels = _parse_channels(request.query_params.get("channels"))

    with get_session() as session:
        competitor = session.get(Competitor, competitor_id)
        if competitor is None:
            return RedirectResponse(url="/competitors", status_code=HTTP_303_SEE_OTHER)
        name = competitor.name

    def _run():
        from ..runner import run
        if channels:
            for ch in channels:
                run(channel=ch, competitor_name=name)
        else:
            run(competitor_name=name)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    redirect_url = f"/runs?competitor_id={competitor_id}&started=1"
    if channels:
        redirect_url += "&channels=" + ",".join(channels)
    return RedirectResponse(url=redirect_url, status_code=HTTP_303_SEE_OTHER)


@router.get("/competitors/{competitor_id}/added")
def competitor_added(request: Request, competitor_id: int):
    """Landing page after adding a new competitor: summary of uploaded data and next-step instructions."""
    with get_session() as session:
        competitor = session.get(Competitor, competitor_id)
        if competitor is None:
            return RedirectResponse(url="/competitors", status_code=HTTP_303_SEE_OTHER)
        endpoints = list(competitor.source_endpoints)
        talent_urls = [e.url for e in endpoints if e.channel == "talent"]
        asset_urls = [e.url for e in endpoints if e.channel == "asset"]
        press_urls = [e.url for e in endpoints if e.channel == "press"]
        last_refreshed = get_last_refreshed(session)
        all_competitors = session.query(Competitor).order_by(Competitor.name.asc()).all()
        nav_competitors = [{"id": c.id, "name": c.name} for c in all_competitors]
    competitor_data = {"id": competitor.id, "name": competitor.name, "primary_domain": competitor.primary_domain}
    return request.app.state.templates.TemplateResponse(
        "competitor_added.html",
        {
            "request": request,
            "competitor": competitor_data,
            "talent_urls": talent_urls,
            "asset_urls": asset_urls,
            "press_urls": press_urls,
            "last_refreshed": last_refreshed,
            "nav_competitors": nav_competitors,
        },
    )


@router.get("/competitors/{competitor_id}")
def competitors_edit(request: Request, competitor_id: int):
    with get_session() as session:
        competitor = session.get(Competitor, competitor_id)
        if competitor is None:
            return RedirectResponse(url="/competitors", status_code=HTTP_303_SEE_OTHER)
        endpoints = sorted(competitor.source_endpoints, key=lambda e: e.id)
        # First endpoint (by id) per channel is primary for talent/asset; used for fallback in runner
        primary_ids = set()
        for ch in ("talent", "asset"):
            first = next((e.id for e in endpoints if e.channel == ch), None)
            if first is not None:
                primary_ids.add(first)
        recent_logs = (
            session.query(RunLog)
            .filter(RunLog.competitor_id == competitor_id)
            .order_by(RunLog.created_at.desc())
            .limit(100)
            .all()
        )
        last_runs = {}
        for log in recent_logs:
            if log.channel not in last_runs:
                last_runs[log.channel] = {
                    "status": log.status,
                    "created_at_str": log.created_at.strftime("%Y-%m-%d %H:%M"),
                }
        last_refreshed = get_last_refreshed(session)
        competitor_data = {
            "id": competitor.id,
            "name": competitor.name,
            "primary_domain": competitor.primary_domain,
            "is_active": getattr(competitor, "is_active", True),
        }
        endpoints_data = [
            {
                "id": ep.id,
                "channel": ep.channel,
                "url": ep.url,
                "confidence": ep.confidence,
                "js_required": ep.js_required,
                "use_sitemap_first": ep.use_sitemap_first,
                "is_primary": ep.channel in ("talent", "asset") and ep.id in primary_ids,
            }
            for ep in endpoints
        ]
        all_competitors = session.query(Competitor).order_by(Competitor.name.asc()).all()
        nav_competitors = [{"id": c.id, "name": c.name} for c in all_competitors]
    return request.app.state.templates.TemplateResponse(
        "competitor_edit.html",
        {"request": request, "competitor": competitor_data, "endpoints": endpoints_data, "last_runs": last_runs, "last_refreshed": last_refreshed, "nav_competitors": nav_competitors},
    )


@router.post("/competitors/{competitor_id}")
def competitors_update(
    competitor_id: int,
    name: str = Form(...),
    primary_domain: Optional[str] = Form(None),
    is_active: Optional[str] = Form(None),
):
    with get_session() as session:
        competitor = session.get(Competitor, competitor_id)
        if competitor is None:
            return RedirectResponse(url="/competitors", status_code=HTTP_303_SEE_OTHER)
        competitor.name = name.strip()
        competitor.primary_domain = (primary_domain or "").strip() or None
        competitor.is_active = bool(is_active)
    _sync_seed_file()
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
    _sync_seed_file()
    return RedirectResponse(url=f"/competitors/{competitor_id}", status_code=HTTP_303_SEE_OTHER)


@router.post("/competitors/{competitor_id}/sources/{source_id}/delete")
def competitor_delete_source(competitor_id: int, source_id: int):
    with get_session() as session:
        endpoint = session.get(SourceEndpoint, source_id)
        if endpoint is not None:
            session.delete(endpoint)
    _sync_seed_file()
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
    _sync_seed_file()
    return RedirectResponse(url=f"/competitors/{competitor_id}", status_code=HTTP_303_SEE_OTHER)
