from typing import Any, Optional

from fastapi import APIRouter, Request

from ..db import get_session, get_last_refreshed
from ..models import Competitor, RunLog
from ..utils import to_eastern

router = APIRouter()

# Human-readable explanations for technical message codes (helps debug website issues)
MESSAGE_DISPLAY_MAP = {
    "snapshot_unchanged": "No changes since last run",
    "primary_returned_zero_jobs_trying_secondary": "Primary careers URL returned 0 jobs, trying next URL",
    "primary_returned_zero_properties_trying_secondary": "Primary listings URL returned 0 properties, trying next URL",
    "empty_snapshot_kept_previous": "Collector returned empty; keeping previous snapshot",
    "talent_js_fallback_zero_jobs": "JS careers page returned 0 jobs (may need Playwright; check hint in details)",
    "talent_seed_baseline": "First snapshot saved as baseline",
    "asset_seed_baseline": "First snapshot saved as baseline",
    "press_seed_baseline": "First snapshot saved as baseline",
    "homepage_seed_baseline": "First snapshot saved as baseline",
    "social_seed_baseline": "First snapshot saved as baseline",
    "no_press_items": "No press items found from any source",
    "all_items_outside_window": "All items older than 90 days; none kept",
    "no_posts": "No posts from RSS/social feeds",
}


def _extra_str(extra: Any) -> str:
    """Safely format RunLog.extra_json for display; handles None, non-dict, or dict."""
    if extra is None:
        return ""
    if not isinstance(extra, dict):
        return str(extra)[:200] if extra else ""
    return " ".join(f"{k}: {v}" for k, v in extra.items())


def _display_message(log: RunLog) -> str:
    """Build a user-friendly message for success, error, or skip; helps understand website issues."""
    msg = log.message or ""
    extra = log.extra_json if isinstance(log.extra_json, dict) else {}
    display = MESSAGE_DISPLAY_MAP.get(msg, msg) if msg else ""
    if log.status == "error" and msg:
        return msg  # Error message is already descriptive (exception text)
    if log.status == "skipped" and display:
        return display
    if log.status == "success":
        if msg and msg in MESSAGE_DISPLAY_MAP:
            return MESSAGE_DISPLAY_MAP[msg]
        # Build from extra when no message
        parts = []
        if extra.get("jobs") is not None:
            parts.append(f"Collected {extra['jobs']} jobs")
        elif extra.get("added_jobs") is not None:
            parts.append(f"+{extra['added_jobs']} jobs")
        elif extra.get("properties") is not None:
            parts.append(f"Collected {extra['properties']} properties")
        elif extra.get("added_properties") is not None:
            parts.append(f"+{extra['added_properties']} properties")
        elif extra.get("raw_items") is not None:
            parts.append(f"{extra.get('filtered_items', extra['raw_items'])} articles")
        elif extra.get("added_items") is not None:
            parts.append(f"+{extra['added_items']} articles")
        elif extra.get("posts") is not None:
            parts.append(f"{extra['posts']} posts")
        elif extra.get("total") is not None and extra.get("properties") is not None:
            parts.append(f"{extra['properties']}/{extra['total']} review properties")
        elif extra.get("added_items") is not None:
            parts.append(f"+{extra['added_items']} items")
        if parts:
            return " ".join(parts)
    return display or ""


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
                "created_at_str": to_eastern(log.created_at),
                "created_at_ts": int(log.created_at.timestamp()) if log.created_at else None,
                "competitor_id": log.competitor_id,
                "channel": log.channel,
                "status": log.status,
                "message": log.message,
                "display_message": _display_message(log),
                "extra_str": _extra_str(log.extra_json),
                "extra": log.extra_json,
            }
            for log in logs_rows
        ]
        last_refreshed = get_last_refreshed(session)
        nav_competitors = competitor_options
        run_started_name = None
        if started and competitor_id and competitor_id in competitor_names:
            run_started_name = competitor_names[competitor_id]

        # Detect running batches so the page can offer Cancel run (by run_start_ts)
        running_logs = (
            session.query(RunLog)
            .filter(RunLog.status == "running")
            .limit(500)
            .all()
        )
        running_run_start_ts = None
        running_batches = []
        for log in running_logs:
            if isinstance(log.extra_json, dict) and "started_at" in log.extra_json:
                ts = log.extra_json["started_at"]
                if ts not in {b["run_start_ts"] for b in running_batches}:
                    running_batches.append({"run_start_ts": ts})
        if running_batches:
            running_run_start_ts = running_batches[0]["run_start_ts"]

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
            "running_run_start_ts": running_run_start_ts,
            "running_batches": running_batches,
        },
    )
