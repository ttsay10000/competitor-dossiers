import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode
from typing import Optional

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.exc import IntegrityError
from starlette.status import HTTP_303_SEE_OTHER

from sqlalchemy import func

from datetime import datetime, timedelta, timezone

from sqlalchemy import or_

from ..db import get_session, get_last_refreshed
from ..models import Competitor, CompetitorReviewProperty, SourceEndpoint, RunLog, Snapshot, Event, Capability
from ..utils import to_eastern
from ..config import settings
from ..collectors.reviews import resolve_place_id_from_text
from ..validation import validate_url_format, suggest_urls_from_domain, normalize_domain
from ..diff.asset_diff import infer_location_for_property
from ..llm_structured import _assign_state_from_url, research_operating_model_llm
from ..executive_summary import US_STATES_LIST

router = APIRouter()

# Run start timestamps that the user requested to cancel. Workers check this and skip remaining jobs.
CANCELLED_RUN_STARTS: set[int] = set()

# Main channels for status display (order: T A P W S R)
DISPLAY_CHANNELS = ("talent", "asset", "press", "homepage", "public_records", "social", "reviews")

# Order to run channels when user selects multiple (quickest to longest). Unlisted channels run last.
CHANNEL_RUN_ORDER = ("talent", "press", "homepage", "social", "asset", "reviews")


def _sort_channels_by_run_order(channels: list[str]) -> list[str]:
    """Return channels sorted by CHANNEL_RUN_ORDER (quickest first). Channels not in order run last."""
    order = {ch: i for i, ch in enumerate(CHANNEL_RUN_ORDER)}
    return sorted(channels, key=lambda c: order.get(c, len(CHANNEL_RUN_ORDER)))
# Single-letter labels for Data/Status column: W=website/digital footprint, S=social, R=reviews
CHANNEL_LETTERS = {"talent": "T", "asset": "A", "press": "P", "homepage": "W", "public_records": "Pr", "reviews": "R", "social": "S"}
CHANNEL_NAMES = {"talent": "Talent", "asset": "Asset", "press": "Press", "homepage": "Website", "public_records": "Public Records", "social": "Social", "reviews": "Reviews"}


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


def _competitor_topline_summary(session, c: Competitor) -> dict:
    """Return topline metrics for list view: properties, states count, jobs, latest news, website changes, descriptions."""
    US_STATES_SET = frozenset(US_STATES_LIST)
    total_properties = 0
    total_markets_states = 0
    latest_asset = (
        session.query(Snapshot)
        .filter(Snapshot.competitor_id == c.id, Snapshot.channel == "asset")
        .order_by(Snapshot.captured_at.desc())
        .first()
    )
    if latest_asset:
        raw = (latest_asset.structured_json or {}).get("properties", [])
        props = [p for p in raw if isinstance(p, dict)]
        props = [_assign_state_from_url(p) for p in props]
        total_properties = len(props)
        locations = [infer_location_for_property(p) for p in props]
        state_parts = set()
        for loc in locations:
            part = (loc.split(" - ")[0] if " - " in (loc or "") else (loc or "")).strip()
            if part and part in US_STATES_SET:
                state_parts.add(part)
        total_markets_states = len(state_parts)

    total_jobs = 0
    latest_talent = (
        session.query(Snapshot)
        .filter(Snapshot.competitor_id == c.id, Snapshot.channel == "talent")
        .order_by(Snapshot.captured_at.desc())
        .first()
    )
    if latest_talent:
        jobs = (latest_talent.structured_json or {}).get("jobs", [])
        total_jobs = len([j for j in jobs if isinstance(j, dict)])

    latest_news_count = 0
    latest_news_headline = None
    latest_news_items: list[dict] = []
    latest_press = (
        session.query(Snapshot)
        .filter(Snapshot.competitor_id == c.id, Snapshot.channel == "press")
        .order_by(Snapshot.captured_at.desc())
        .first()
    )
    if latest_press:
        struct = latest_press.structured_json or {}
        # Recent news = top-news bullets from dossier (no hyperlinks). Use stored top_news when available.
        stored_top_news = struct.get("top_news")
        if isinstance(stored_top_news, list) and stored_top_news:
            for b in stored_top_news[:5]:
                if isinstance(b, dict):
                    bullet = (b.get("bullet") or b.get("title") or "").strip() or None
                    if bullet:
                        latest_news_items.append({"title": bullet, "url": None})
            latest_news_count = len(latest_news_items)
            if latest_news_items:
                latest_news_headline = latest_news_items[0].get("title")
        else:
            # Fallback: flatten articles (old snapshots without top_news).
            press_groups = struct.get("press_groups") or []
            if press_groups:
                items = []
                for g in press_groups:
                    for art in (g.get("articles") or []) if isinstance(g, dict) else []:
                        if isinstance(art, dict):
                            items.append(art)
            else:
                items = struct.get("canonical_items") or struct.get("items", [])
            items = [i for i in items if isinstance(i, dict)]
            latest_news_count = len(items)
            for item in items[:5]:
                title = (item.get("title") or item.get("display_title") or "").strip() or None
                if not title:
                    continue
                url = (item.get("url") or item.get("link") or item.get("source_url") or "").strip() or None
                latest_news_items.append({"title": title, "url": url})
            if items:
                first = items[0]
                latest_news_headline = (first.get("title") or first.get("display_title") or "").strip() or None

    cutoff = datetime.now(timezone.utc) - timedelta(days=90)
    q = session.query(Event).filter(
        Event.competitor_id == c.id,
        Event.type == "narrative.homepage_updated",
        or_(Event.detected_at >= cutoff, (Event.occurred_at.isnot(None)) & (Event.occurred_at >= cutoff)),
    )
    if getattr(c, "reporting_baseline_at", None):
        baseline = c.reporting_baseline_at
        if baseline.tzinfo is None:
            baseline = baseline.replace(tzinfo=timezone.utc)
        q = q.filter(or_(Event.detected_at >= baseline, (Event.occurred_at.isnot(None)) & (Event.occurred_at >= baseline)))
    recent_website_changes_count = q.count()

    return {
        "total_properties": total_properties,
        "total_markets_states": total_markets_states,
        "total_jobs": total_jobs,
        "latest_news_count": latest_news_count,
        "latest_news_headline": latest_news_headline,
        "latest_news_items": latest_news_items,
        "recent_website_changes_count": recent_website_changes_count,
        "short_description": getattr(c, "short_description", None) or None,
        "operating_model_description": getattr(c, "operating_model_description", None) or None,
    }


@router.get("/competitors")
def competitors_list(request: Request):
    with get_session() as session:
        competitors = session.query(Competitor).order_by(Competitor.created_at.desc()).all()
        competitors_data = []
        for c in competitors:
            status = _competitor_status(session, c.id)
            last_refresh = status.get("last_refresh_at")
            topline = _competitor_topline_summary(session, c)
            competitors_data.append({
                "id": c.id,
                "name": c.name,
                "primary_domain": c.primary_domain,
                "created_at": c.created_at,
                "has_snapshots": status["has_snapshots"],
                "last_runs": status["last_runs"],
                "last_refresh": to_eastern(last_refresh) if last_refresh else None,
                "baseline_set": to_eastern(c.reporting_baseline_at) if getattr(c, "reporting_baseline_at", None) else None,
                "total_properties": topline["total_properties"],
                "total_markets_states": topline["total_markets_states"],
                "total_jobs": topline["total_jobs"],
                "latest_news_count": topline["latest_news_count"],
                "latest_news_headline": topline["latest_news_headline"],
                "latest_news_items": topline.get("latest_news_items") or [],
                "recent_website_changes_count": topline["recent_website_changes_count"],
                "short_description": topline["short_description"],
                "operating_model_description": topline["operating_model_description"],
            })
        last_refreshed = get_last_refreshed(session)
    nav_competitors = [{"id": c["id"], "name": c["name"], "created_at": c.get("created_at")} for c in competitors_data]
    run_blocked = request.query_params.get("run_blocked") == "1"
    run_blocked_running = request.query_params.get("running") or ""
    remove_error = request.query_params.get("remove_error") == "1"
    remove_competitor_name = request.query_params.get("competitor_name") or ""
    removed = request.query_params.get("removed") == "1"
    return request.app.state.templates.TemplateResponse(
        "competitors.html",
        {
            "request": request,
            "competitors": competitors_data,
            "nav_competitors": nav_competitors,
            "last_refreshed": last_refreshed,
            "display_channels": DISPLAY_CHANNELS,
            "channel_letters": CHANNEL_LETTERS,
            "channel_names": CHANNEL_NAMES,
            "run_blocked": run_blocked,
            "run_blocked_running": run_blocked_running,
            "remove_error": remove_error,
            "remove_competitor_name": remove_competitor_name,
            "removed": removed,
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


def _validate_source_urls(talent_urls: list, asset_urls: list) -> list[dict]:
    """Validate all non-empty URLs; return list of errors {channel, index, value, message}."""
    errors = []
    for channel, urls in [("talent", talent_urls), ("asset", asset_urls)]:
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
        raw_domain = (form.get("primary_domain") or "").strip() or None
        primary_domain = normalize_domain(raw_domain) if raw_domain else None
        talent_urls = form.getlist("talent_urls")
        asset_urls = form.getlist("asset_urls")
        twitter_url = (form.get("twitter_url") or "").strip() or None
        linkedin_url = (form.get("linkedin_url") or "").strip() or None
        press_keywords_raw = (form.get("press_keywords") or "").strip() or None

        url_errors = list(_validate_source_urls(talent_urls, asset_urls))
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
                "twitter_url": twitter_url or "",
                "linkedin_url": linkedin_url or "",
                "press_keywords": press_keywords_raw or "",
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
            press_keywords = _parse_google_news_phrases(press_keywords_raw)
            if press_keywords:
                press_url = f"https://{primary_domain}" if primary_domain else "https://example.com"
                session.add(
                    SourceEndpoint(
                        competitor_id=competitor.id,
                        channel="press",
                        url=press_url,
                        confidence="high",
                        js_required=False,
                        use_sitemap_first=False,
                        extra_options={
                            "google_news_search_phrases": press_keywords,
                        },
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


def _running_for_competitor_channels(session, competitor_id: int, channels: Optional[list[str]]) -> list[tuple[str, str]]:
    """Return list of (competitor_name, channel) that are already running for this competitor (and given channels or any)."""
    running = (
        session.query(RunLog.channel, Competitor.name)
        .join(Competitor, RunLog.competitor_id == Competitor.id)
        .filter(RunLog.competitor_id == competitor_id, RunLog.status == "running")
        .all()
    )
    if channels is not None:
        ch_set = set(channels)
        running = [(name, ch) for ch, name in running if ch in ch_set]
    else:
        running = [(name, ch) for ch, name in running]
    return running


def _running_for_any_selected(session, competitor_ids: list[int], channels: list[str]) -> list[tuple[str, str]]:
    """Return list of (competitor_name, channel) that are already running for any of the given (competitor_id, channel) pairs."""
    if not competitor_ids or not channels:
        return []
    running = (
        session.query(RunLog.competitor_id, RunLog.channel, Competitor.name)
        .join(Competitor, RunLog.competitor_id == Competitor.id)
        .filter(
            RunLog.competitor_id.in_(competitor_ids),
            RunLog.channel.in_(channels),
            RunLog.status == "running",
        )
        .all()
    )
    return [(name, ch) for _cid, ch, name in running]


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
    if channels:
        channels = _sort_channels_by_run_order(channels)

    with get_session() as session:
        competitor = session.get(Competitor, competitor_id)
        if competitor is None:
            return RedirectResponse(url="/competitors", status_code=HTTP_303_SEE_OTHER)
        name = competitor.name
        # Block if a refresh is already running for this competitor (same channels we're about to start)
        channels_to_run = list(channels) if channels else None  # None = all channels
        already = _running_for_competitor_channels(session, competitor_id, channels_to_run)
        if already:
            # Redirect to dossier so user sees message; they can cancel from Run status or wait
            redirect_url = f"/dossier/{competitor_id}?run_blocked=1&running=" + ",".join(f"{n}:{ch}" for n, ch in already[:5])
            return RedirectResponse(url=redirect_url, status_code=HTTP_303_SEE_OTHER)

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
        cancel_check = lambda: run_start_ts in CANCELLED_RUN_STARTS
        if channels:
            for ch in channels:
                if run_start_ts in CANCELLED_RUN_STARTS:
                    return
                run(channel=ch, competitor_name=name, is_cancelled=cancel_check)
        else:
            run(competitor_name=name, is_cancelled=cancel_check)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    # Send user to dossier so they see run status with timers
    redirect_url = f"/dossier/{competitor_id}?started=1&run_start_ts={run_start_ts}"
    if channels:
        redirect_url += "&channels=" + ",".join(channels)
    return RedirectResponse(url=redirect_url, status_code=HTTP_303_SEE_OTHER)


@router.post("/competitors/run-selected-channels")
async def competitors_run_selected_channels(request: Request):
    """Start selected channels for all competitors in the background; redirect to /competitors with run log params."""
    form = await request.form()
    ch_list = form.getlist("channels") if form else []
    channels = _parse_channels(",".join(ch_list)) if ch_list else _parse_channels(form.get("channels") if form else None)
    if not channels:
        return RedirectResponse(url="/competitors", status_code=HTTP_303_SEE_OTHER)
    channels = _sort_channels_by_run_order(channels)

    with get_session() as session:
        competitors = session.query(Competitor).order_by(Competitor.name.asc()).all()
        competitors = [c for c in competitors if getattr(c, "is_active", True)]
        competitor_ids = [c.id for c in competitors]
        already = _running_for_any_selected(session, competitor_ids, channels)
        if already:
            redirect_url = "/competitors?run_blocked=1&running=" + ",".join(f"{n}:{ch}" for n, ch in already[:10])
            return RedirectResponse(url=redirect_url, status_code=HTTP_303_SEE_OTHER)

    run_start_ts = int(datetime.now(timezone.utc).timestamp())
    competitor_names = []
    with get_session() as session:
        competitors = session.query(Competitor).order_by(Competitor.name.asc()).all()
        competitors = [c for c in competitors if getattr(c, "is_active", True)]
        competitor_names = [c.name for c in competitors]
        for competitor in competitors:
            for ch in channels:
                session.add(
                    RunLog(
                        competitor_id=competitor.id,
                        channel=ch,
                        status="running",
                        message=None,
                        extra_json={"started_at": run_start_ts},
                    )
                )

    # channels already sorted by CHANNEL_RUN_ORDER (talent → press → website → social → asset → reviews)
    channels_list = list(channels)
    jobs = [(run_start_ts, name, ch) for ch in channels_list for name in competitor_names]

    def _run_one(args):
        run_ts, name, ch = args
        if run_ts in CANCELLED_RUN_STARTS:
            return
        from ..runner import run
        run(channel=ch, competitor_name=name, is_cancelled=lambda: run_ts in CANCELLED_RUN_STARTS)

    def _run_all():
        max_workers = min(3, len(jobs)) or 1
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            list(executor.map(_run_one, jobs))

    thread = threading.Thread(target=_run_all, daemon=True)
    thread.start()

    redirect_url = f"/competitors?started=1&run_start_ts={run_start_ts}&channels=" + ",".join(channels)
    return RedirectResponse(url=redirect_url, status_code=HTTP_303_SEE_OTHER)


@router.post("/competitors/cancel-run")
async def competitors_cancel_run(request: Request):
    """Mark a run as cancelled: set CANCELLED_RUN_STARTS so workers skip remaining jobs, and set RunLog status to cancelled."""
    run_start_ts = request.query_params.get("run_start_ts")
    if not run_start_ts:
        try:
            body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
            run_start_ts = body.get("run_start_ts")
        except Exception:
            pass
    if not run_start_ts:
        form = await request.form()
        run_start_ts = form.get("run_start_ts") if form else None
    if not run_start_ts:
        return JSONResponse(content={"error": "run_start_ts required"}, status_code=400)
    run_start_ts = int(run_start_ts)

    CANCELLED_RUN_STARTS.add(run_start_ts)

    with get_session() as session:
        running = session.query(RunLog).filter(RunLog.status == "running").all()
        for log in running:
            if isinstance(log.extra_json, dict) and log.extra_json.get("started_at") == run_start_ts:
                log.status = "cancelled"
        session.commit()

    return JSONResponse(content={"ok": True})


@router.get("/competitors/run-status")
def competitors_run_status(request: Request):
    """Return running and completed run logs for all competitors (global refresh run log)."""
    run_start_ts = request.query_params.get("run_start_ts")
    channels_param = request.query_params.get("channels")
    if not run_start_ts or not channels_param:
        return JSONResponse(content={"error": "run_start_ts and channels required"}, status_code=400)
    run_start_ts = int(run_start_ts)
    requested_channels = [c.strip().lower() for c in channels_param.split(",") if c.strip()]

    with get_session() as session:
        running = (
            session.query(RunLog, Competitor.name)
            .join(Competitor, RunLog.competitor_id == Competitor.id)
            .filter(
                RunLog.status == "running",
                RunLog.channel.in_(requested_channels),
            )
            .order_by(Competitor.name.asc(), RunLog.channel.asc())
            .all()
        )
        completed_logs = (
            session.query(RunLog)
            .filter(
                RunLog.status.in_(["success", "error", "skipped", "cancelled"]),
                RunLog.channel.in_(requested_channels),
            )
            .order_by(RunLog.created_at.desc())
            .all()
        )
        # Only completions from this run (created_at >= run_start_ts)
        run_start_dt = datetime.fromtimestamp(run_start_ts, tz=timezone.utc)
        completed_logs = [log for log in completed_logs if log.created_at and log.created_at >= run_start_dt]
        # Latest per (competitor_id, channel)
        completed_by_key = {}
        for log in completed_logs:
            key = (log.competitor_id, log.channel)
            if key not in completed_by_key:
                completed_by_key[key] = log
        competitor_ids = set(log.competitor_id for log in completed_by_key.values()) | set(r.competitor_id for r, _ in running)
        competitors_by_id = {}
        if competitor_ids:
            for c in session.query(Competitor).filter(Competitor.id.in_(competitor_ids)).all():
                competitors_by_id[c.id] = c.name

        return JSONResponse(
            content={
                "running": [
                    {
                        "competitor_id": r.competitor_id,
                        "competitor_name": name,
                        "channel": r.channel,
                        "created_at": r.created_at.isoformat() if r.created_at else None,
                    }
                    for r, name in running
                ],
                "completed": [
                    {
                        "competitor_id": log.competitor_id,
                        "competitor_name": competitors_by_id.get(log.competitor_id, ""),
                        "channel": log.channel,
                        "status": log.status,
                        "message": log.message or "",
                        "created_at": log.created_at.isoformat() if log.created_at else None,
                    }
                    for log in completed_by_key.values()
                ],
            }
        )


@router.get("/competitors/{competitor_id}/added")
def competitor_added(request: Request, competitor_id: int):
    """Landing page after adding a new competitor: summary of uploaded data and next-step instructions."""
    seed_sync_failed = request.query_params.get("seed_sync") == "failed"
    export_seed = request.query_params.get("export_seed")  # "1" or "failed" after Add to seed
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
            "export_seed": export_seed,
        },
    )


@router.get("/competitors/{competitor_id}")
def competitors_edit(request: Request, competitor_id: int):
    review_error = request.query_params.get("review_error")
    source_error = request.query_params.get("source_error")
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
            "short_description": getattr(competitor, "short_description", None) or "",
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
                "extra_options": ep.extra_options or {},
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
            "source_error": source_error,
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
    short_description: Optional[str] = Form(None),
):
    with get_session() as session:
        competitor = session.get(Competitor, competitor_id)
        if competitor is None:
            return RedirectResponse(url="/competitors", status_code=HTTP_303_SEE_OTHER)
        competitor.name = name.strip()
        raw_domain = (primary_domain or "").strip() or None
        competitor.primary_domain = normalize_domain(raw_domain) if raw_domain else None
        competitor.short_description = (short_description or "").strip() or None
    _sync_seed_file()
    return RedirectResponse(url=f"/competitors/{competitor_id}", status_code=HTTP_303_SEE_OTHER)


def _parse_google_news_phrases(raw: Optional[str]) -> Optional[list[str]]:
    """Parse comma- or newline-separated phrases into a list (strip empty)."""
    if not raw or not (raw := raw.strip()):
        return None
    phrases = [p.strip() for p in raw.replace("\n", ",").split(",") if p.strip()]
    return phrases if phrases else None


def _press_placeholder_url(primary_domain: Optional[str]) -> str:
    """URL placeholder for press channel (Google News doesn't need a real URL)."""
    if primary_domain and primary_domain.strip():
        return f"https://{primary_domain.strip().split('/')[0].split(':')[0]}"
    return "https://example.com"


@router.post("/competitors/{competitor_id}/sources")
def competitor_add_source(
    competitor_id: int,
    channel: str = Form(...),
    url: Optional[str] = Form(None),
    confidence: str = Form("high"),
    js_required: Optional[str] = Form(None),
    use_sitemap_first: Optional[str] = Form(None),
    google_news_search_phrases: Optional[str] = Form(None),
):
    with get_session() as session:
        competitor = session.get(Competitor, competitor_id)
        if competitor is None:
            return RedirectResponse(url="/competitors", status_code=HTTP_303_SEE_OTHER)
        ch = channel.strip().lower()
        url_val = (url or "").strip()
        if ch == "press" and not url_val:
            url_val = _press_placeholder_url(competitor.primary_domain)
        elif not url_val:
            return RedirectResponse(
                url=f"/competitors/{competitor_id}?source_error=url_required",
                status_code=HTTP_303_SEE_OTHER,
            )
        extra_options = None
        if ch == "press":
            phrases = _parse_google_news_phrases(google_news_search_phrases)
            if phrases:
                extra_options = {"google_news_search_phrases": phrases}
        endpoint = SourceEndpoint(
            competitor_id=competitor_id,
            channel=ch,
            url=url_val,
            confidence=confidence.strip() or "high",
            js_required=bool(js_required),
            use_sitemap_first=bool(use_sitemap_first),
            extra_options=extra_options,
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
    url: Optional[str] = Form(None),
    confidence: str = Form("high"),
    js_required: Optional[str] = Form(None),
    use_sitemap_first: Optional[str] = Form(None),
    google_news_search_phrases: Optional[str] = Form(None),
):
    with get_session() as session:
        endpoint = session.get(SourceEndpoint, source_id)
        if endpoint is None:
            return RedirectResponse(url=f"/competitors/{competitor_id}", status_code=HTTP_303_SEE_OTHER)
        competitor = session.get(Competitor, competitor_id)
        ch = channel.strip().lower()
        url_val = (url or "").strip()
        if ch == "press" and not url_val:
            url_val = _press_placeholder_url(competitor.primary_domain if competitor else None)
        elif not url_val:
            return RedirectResponse(
                url=f"/competitors/{competitor_id}?source_error=url_required",
                status_code=HTTP_303_SEE_OTHER,
            )
        endpoint.channel = ch
        endpoint.url = url_val
        endpoint.confidence = confidence.strip() or "high"
        endpoint.js_required = bool(js_required)
        endpoint.use_sitemap_first = bool(use_sitemap_first)
        if ch == "press":
            extra = dict(endpoint.extra_options or {})
            phrases = _parse_google_news_phrases(google_news_search_phrases)
            if phrases:
                extra["google_news_search_phrases"] = phrases
            else:
                extra.pop("google_news_search_phrases", None)
            endpoint.extra_options = extra if extra else None
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


def _required_remove_phrase(name: str) -> str:
    """Exact phrase user must type to confirm competitor removal."""
    return f"remove {name} from site"


@router.post("/competitors/{competitor_id}/remove")
async def competitor_remove(request: Request, competitor_id: int, confirmation: str = Form("")):
    """Remove a competitor after confirmation. Expects confirmation exactly: 'remove {name} from site'."""
    with get_session() as session:
        competitor = session.get(Competitor, competitor_id)
        if competitor is None:
            return RedirectResponse(url="/competitors", status_code=HTTP_303_SEE_OTHER)
        name = competitor.name
        required = _required_remove_phrase(name)
        if (confirmation or "").strip() != required:
            return RedirectResponse(
                url="/competitors?" + urlencode({"remove_error": "1", "competitor_name": name}),
                status_code=HTTP_303_SEE_OTHER,
            )
        # Delete dependent rows (no cascade from Competitor to these)
        session.query(RunLog).filter(RunLog.competitor_id == competitor_id).delete()
        session.query(Snapshot).filter(Snapshot.competitor_id == competitor_id).delete()
        session.query(Event).filter(Event.competitor_id == competitor_id).delete()
        session.query(Capability).filter(Capability.competitor_id == competitor_id).delete()
        session.delete(competitor)
    _sync_seed_file()
    return RedirectResponse(
        url="/competitors?" + urlencode({"removed": "1"}),
        status_code=HTTP_303_SEE_OTHER,
    )


@router.post("/competitors/{competitor_id}/research-operating-model")
def competitor_research_operating_model(competitor_id: int):
    """Run LLM research on operating model (revenue share / owned / master lease) and save to competitor."""
    with get_session() as session:
        competitor = session.get(Competitor, competitor_id)
        if competitor is None:
            return RedirectResponse(url="/competitors", status_code=HTTP_303_SEE_OTHER)
        text = research_operating_model_llm(competitor.name, competitor.primary_domain or "")
        if text:
            competitor.operating_model_description = text
            session.commit()
        # Redirect back; optional query param could signal success/failure
        q = urlencode({"operating_model_updated": "1"}) if text else ""
    return RedirectResponse(url="/competitors" + ("?" + q if q else ""), status_code=HTTP_303_SEE_OTHER)
