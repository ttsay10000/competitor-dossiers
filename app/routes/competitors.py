import logging
import threading
from urllib.parse import urlencode
from typing import Optional

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.exc import IntegrityError
from starlette.status import HTTP_303_SEE_OTHER

from sqlalchemy import func

from datetime import datetime, timezone

from ..db import get_session, get_last_refreshed
from ..models import Competitor, CompetitorReviewProperty, SourceEndpoint, RunLog, Snapshot
from ..utils import to_eastern
from ..config import settings
from ..collectors.reviews import resolve_place_id_from_text
from ..validation import validate_url_format, suggest_urls_from_domain

router = APIRouter()

# Main channels for status display (order: T A P W S R)
DISPLAY_CHANNELS = ("talent", "asset", "press", "homepage", "social", "reviews")
# Single-letter labels for Data/Status column: W=website/digital footprint, S=social, R=reviews
CHANNEL_LETTERS = {"talent": "T", "asset": "A", "press": "P", "homepage": "W", "social": "S", "reviews": "R"}


def _sync_seed_file() -> bool:
    """After any competitor/source change, write DB to seed_data.json so UI additions persist. Never fail the request. Returns True if sync succeeded."""
    try:
        from ..seed import export_seed_to_file
        export_seed_to_file()
        return True
    except Exception as e:
        logging.warning("Failed to sync seed_data.json after competitor change: %s", e, exc_info=True)
        return False


@router.get("/")
def root():
    return RedirectResponse(url="/competitors", status_code=HTTP_303_SEE_OTHER)


def _competitor_status(session, competitor_id: int) -> dict:
    """Return has_snapshots per channel, last_runs per channel, and last_refresh for a competitor."""
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
                "created_at_str": to_eastern(log.created_at),
            }
    last_refresh_row = (
        session.query(RunLog)
        .filter(RunLog.competitor_id == competitor_id)
        .order_by(RunLog.created_at.desc())
        .first()
    )
    last_refresh_at = last_refresh_row.created_at if last_refresh_row else None
    return {"has_snapshots": has_snapshots, "last_runs": last_runs, "last_refresh_at": last_refresh_at}


@router.get("/competitors")
def competitors_list(request: Request):
    with get_session() as session:
        competitors = session.query(Competitor).order_by(Competitor.created_at.desc()).all()
        competitors_data = []
        for c in competitors:
            status = _competitor_status(session, c.id)
            last_refresh = status.get("last_refresh_at")
            competitors_data.append({
                "id": c.id,
                "name": c.name,
                "primary_domain": c.primary_domain,
                "created_at": c.created_at,
                "has_snapshots": status["has_snapshots"],
                "last_runs": status["last_runs"],
                "last_refresh": to_eastern(last_refresh) if last_refresh else None,
                "baseline_set": to_eastern(c.reporting_baseline_at) if getattr(c, "reporting_baseline_at", None) else None,
            })
        last_refreshed = get_last_refreshed(session)
    nav_competitors = [{"id": c["id"], "name": c["name"], "created_at": c.get("created_at")} for c in competitors_data]
    return request.app.state.templates.TemplateResponse(
        "competitors.html",
        {
            "request": request,
            "competitors": competitors_data,
            "nav_competitors": nav_competitors,
            "last_refreshed": last_refreshed,
            "display_channels": DISPLAY_CHANNELS,
            "channel_letters": CHANNEL_LETTERS,
        },
    )


@router.get("/competitors/suggest-urls")
def competitors_suggest_urls(domain: Optional[str] = None):
    """Return suggested talent/asset/press URLs for a primary domain (e.g. example.com)."""
    if not (domain or "").strip():
        return JSONResponse(content={"talent": [], "asset": [], "press": []})
    suggestions = suggest_urls_from_domain(domain.strip())
    return JSONResponse(content=suggestions)


@router.get("/competitors/rollup-summary")
def competitors_rollup_summary():
    """JSON: roll-up of executive summaries for the competitors page. Cached by summary inputs."""
    from ..routes.dossier import get_rollup_summary
    from ..executive_summary import format_rollup_summary_for_display
    with get_session() as session:
        rollup_text, empty_reason = get_rollup_summary(session)
    if empty_reason == "no_summaries":
        return JSONResponse(content={"empty": True, "message": "Run the summary/digest to generate executive summaries; the roll-up will appear here."})
    if empty_reason == "no_api_key":
        return JSONResponse(content={"empty": True, "message": "Set OPENAI_API_KEY to generate executive summaries and the roll-up."})
    if empty_reason == "rollup_failed":
        return JSONResponse(content={"empty": True, "message": "Unable to generate roll-up; try again later."})
    if rollup_text:
        return JSONResponse(content={
            "rollup": rollup_text,
            "rollup_display": format_rollup_summary_for_display(rollup_text),
        })
    return JSONResponse(content={"empty": True, "message": "No roll-up available."})


def _validate_source_urls(talent_urls: list, asset_urls: list, press_urls: list) -> list[dict]:
    """Validate all non-empty URLs; return list of errors {channel, index, value, message}."""
    errors = []
    for channel, urls in [("talent", talent_urls), ("asset", asset_urls), ("press", press_urls)]:
        for i, raw in enumerate(urls):
            url = (raw or "").strip()
            if not url:
                continue
            ok, msg = validate_url_format(url)
            if not ok:
                errors.append({"channel": channel, "index": i, "value": url, "message": msg or "Invalid URL"})
    return errors


def _validate_optional_url(url: Optional[str]) -> Optional[str]:
    """If url is non-empty, validate and return error message or None."""
    u = (url or "").strip()
    if not u:
        return None
    ok, msg = validate_url_format(u)
    return None if ok else (msg or "Invalid URL")


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
        {
            "request": request,
            "last_refreshed": last_refreshed,
            "error_message": error_message,
            "form_data": None,
            "url_errors": [],
        },
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
        twitter_url = (form.get("twitter_url") or "").strip() or None
        linkedin_url = (form.get("linkedin_url") or "").strip() or None

        url_errors = list(_validate_source_urls(talent_urls, asset_urls, press_urls))
        err = _validate_optional_url(twitter_url)
        if err:
            url_errors.append({"channel": "twitter", "index": 0, "value": twitter_url or "", "message": err})
        err = _validate_optional_url(linkedin_url)
        if err:
            url_errors.append({"channel": "linkedin", "index": 0, "value": linkedin_url or "", "message": err})
        if url_errors:
            with get_session() as session:
                last_refreshed = get_last_refreshed(session)
            form_data = {
                "name": name,
                "primary_domain": primary_domain or "",
                "talent_urls": talent_urls,
                "asset_urls": asset_urls,
                "press_urls": press_urls,
                "twitter_url": twitter_url or "",
                "linkedin_url": linkedin_url or "",
            }
            return request.app.state.templates.TemplateResponse(
                "competitor_new.html",
                {
                    "request": request,
                    "last_refreshed": last_refreshed,
                    "error_message": "Some URLs are invalid. Please fix them below.",
                    "form_data": form_data,
                    "url_errors": url_errors,
                },
                status_code=422,
            )

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
            if twitter_url:
                session.add(
                    SourceEndpoint(
                        competitor_id=competitor.id,
                        channel="social",
                        url=twitter_url,
                        confidence="high",
                        js_required=False,
                        use_sitemap_first=False,
                        extra_options={"platform": "twitter"},
                    )
                )
            if linkedin_url:
                session.add(
                    SourceEndpoint(
                        competitor_id=competitor.id,
                        channel="social",
                        url=linkedin_url,
                        confidence="high",
                        js_required=False,
                        use_sitemap_first=False,
                        extra_options={"platform": "linkedin"},
                    )
                )

            new_id = competitor.id
        seed_synced = _sync_seed_file()
        url = f"/competitors/{new_id}/added"
        if not seed_synced:
            url += "?seed_sync=failed"
        return RedirectResponse(url=url, status_code=HTTP_303_SEE_OTHER)
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

    run_start_ts = int(datetime.now(timezone.utc).timestamp())
    with get_session() as session:
        competitor = session.get(Competitor, competitor_id)
        if competitor is None:
            return RedirectResponse(url="/competitors", status_code=HTTP_303_SEE_OTHER)
        name = competitor.name
        # Log "running" so dossier run-status can show timers; runner will add success/error when done
        if channels:
            for ch in channels:
                session.add(
                    RunLog(
                        competitor_id=competitor_id,
                        channel=ch,
                        status="running",
                        message=None,
                        extra_json={"started_at": run_start_ts},
                    )
                )

    def _run():
        from ..runner import run
        if channels:
            for ch in channels:
                run(channel=ch, competitor_name=name)
        else:
            run(competitor_name=name)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    # Send user to dossier so they see run status with timers
    redirect_url = f"/dossier/{competitor_id}?started=1&run_start_ts={run_start_ts}"
    if channels:
        redirect_url += "&channels=" + ",".join(channels)
    return RedirectResponse(url=redirect_url, status_code=HTTP_303_SEE_OTHER)


@router.get("/competitors/{competitor_id}/added")
def competitor_added(request: Request, competitor_id: int):
    """Landing page after adding a new competitor: summary of uploaded data and next-step instructions."""
    seed_sync_failed = request.query_params.get("seed_sync") == "failed"
    with get_session() as session:
        competitor = session.get(Competitor, competitor_id)
        if competitor is None:
            return RedirectResponse(url="/competitors", status_code=HTTP_303_SEE_OTHER)
        endpoints = list(competitor.source_endpoints)
        talent_urls = [e.url for e in endpoints if e.channel == "talent"]
        asset_urls = [e.url for e in endpoints if e.channel == "asset"]
        press_urls = [e.url for e in endpoints if e.channel == "press"]
        last_refreshed = get_last_refreshed(session)
        all_competitors = session.query(Competitor).order_by(Competitor.created_at.desc()).all()
        nav_competitors = [{"id": c.id, "name": c.name, "created_at": c.created_at} for c in all_competitors]
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
            "seed_sync_failed": seed_sync_failed,
        },
    )


@router.get("/competitors/{competitor_id}")
def competitors_edit(request: Request, competitor_id: int):
    review_error = request.query_params.get("review_error")
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
                    "created_at_str": to_eastern(log.created_at),
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
        review_properties = [
            {"id": rp.id, "place_id": rp.place_id, "display_name": rp.display_name or rp.place_id}
            for rp in (competitor.review_properties if hasattr(competitor, "review_properties") else [])
        ]
        latest_reviews = (
            session.query(Snapshot)
            .filter(Snapshot.competitor_id == competitor_id, Snapshot.channel == "reviews")
            .order_by(Snapshot.captured_at.desc())
            .first()
        )
        reviews_snapshot = (latest_reviews.structured_json or {}) if latest_reviews else {}
        all_competitors = session.query(Competitor).order_by(Competitor.created_at.desc()).all()
        nav_competitors = [{"id": c.id, "name": c.name, "created_at": c.created_at} for c in all_competitors]
    return request.app.state.templates.TemplateResponse(
        "competitor_edit.html",
        {
            "request": request,
            "competitor": competitor_data,
            "endpoints": endpoints_data,
            "review_properties": review_properties,
            "reviews_snapshot": reviews_snapshot,
            "review_error": review_error,
            "last_runs": last_runs,
            "last_refreshed": last_refreshed,
            "nav_competitors": nav_competitors,
        },
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


def _safe_redirect_after_review_action(competitor_id: int, next_url: Optional[str], review_error: Optional[str] = None) -> str:
    """Return redirect URL: to dossier if next_url is a valid dossier path for this competitor, else edit page."""
    base = f"/competitors/{competitor_id}"
    if review_error:
        base += f"?review_error={review_error}"
    if not next_url or not (next_url.strip().startswith("/dossier/") and str(competitor_id) in next_url):
        return base
    path = next_url.strip().split("?")[0]
    if path != f"/dossier/{competitor_id}":
        return base
    out = f"/dossier/{competitor_id}"
    if review_error:
        out += f"?review_error={review_error}"
    return out


@router.post("/competitors/{competitor_id}/review-properties")
async def competitor_add_review_property(request: Request, competitor_id: int):
    """Add a review property by Place ID or by search query (name + address). Redirect to next_url if provided (e.g. /dossier/{id})."""
    form = await request.form()
    place_id_or_query = (form.get("place_id_or_query") or "").strip()
    next_url = (form.get("next_url") or "").strip()
    if not place_id_or_query:
        url = _safe_redirect_after_review_action(competitor_id, next_url, "empty")
        return RedirectResponse(url=url, status_code=HTTP_303_SEE_OTHER)
    with get_session() as session:
        competitor = session.get(Competitor, competitor_id)
        if competitor is None:
            return RedirectResponse(url="/competitors", status_code=HTTP_303_SEE_OTHER)
        api_key = getattr(settings, "google_places_api_key", None) or ""
        resolved = resolve_place_id_from_text(place_id_or_query, api_key)
        if not resolved:
            url = _safe_redirect_after_review_action(competitor_id, next_url, "not_found")
            return RedirectResponse(url=url, status_code=HTTP_303_SEE_OTHER)
        place_id, display_name = resolved
        existing = (
            session.query(CompetitorReviewProperty)
            .filter(
                CompetitorReviewProperty.competitor_id == competitor_id,
                CompetitorReviewProperty.place_id == place_id,
            )
            .first()
        )
        if existing:
            url = _safe_redirect_after_review_action(competitor_id, next_url, None)
            return RedirectResponse(url=url, status_code=HTTP_303_SEE_OTHER)
        session.add(
            CompetitorReviewProperty(
                competitor_id=competitor_id,
                place_id=place_id,
                display_name=display_name or None,
            )
        )
    url = _safe_redirect_after_review_action(competitor_id, next_url, None)
    return RedirectResponse(url=url, status_code=HTTP_303_SEE_OTHER)


@router.post("/competitors/{competitor_id}/review-properties/{property_id}/delete")
def competitor_remove_review_property(competitor_id: int, property_id: int):
    with get_session() as session:
        prop = session.get(CompetitorReviewProperty, property_id)
        if prop is not None and prop.competitor_id == competitor_id:
            session.delete(prop)
    return RedirectResponse(url=f"/competitors/{competitor_id}", status_code=HTTP_303_SEE_OTHER)
