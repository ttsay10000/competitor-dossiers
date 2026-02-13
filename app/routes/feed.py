from typing import Optional, List, Union

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..config import settings
from ..db import get_session, get_last_refreshed
from ..models import Competitor, Event, RunLog
from ..digest import send_weekly_digest
from ..utils import to_eastern


class SendDigestBody(BaseModel):
    to: Union[str, List[str]] = Field(..., description="Recipient email(s): one string or list of strings")

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
        competitor_names = {c.id: c.name for c in competitors}
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
                "competitor_name": competitor_names.get(e.competitor_id, ""),
            }
            for e in events_rows
        ]
        competitors_data = [{"id": c.id, "name": c.name, "created_at": c.created_at} for c in competitors]
        nav_competitors = competitors_data
        last_refreshed = get_last_refreshed(session)

    # Event Feed filter uses same channel labels (category value -> display name)
    category_labels = {
        "talent": "Talent",
        "asset": "Asset",
        "partner": "Partner",
        "capital": "Capital",
        "narrative": "Narrative",
        "press": "Press",
        "public_record": "Public Record",
    }
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
            "category_labels": category_labels,
        },
    )


# Feed view: filter by channels (standardized labels). Event category -> channel key for grouping.
FEED_CHANNEL_LABELS = {
    "talent": "Talent",
    "asset": "Asset",
    "press": "Press",
    "public_record": "Public Record",
}
CATEGORY_TO_CHANNEL = {
    "talent": "talent",
    "asset": "asset",
    "press": "press",
    "partner": "press",
    "capital": "press",
    "narrative": "press",
    "public_record": "public_record",
}

# Topic filters for feed view: category -> list of {value, label, types} (types = event type or prefix to match)
FEED_TOPIC_OPTIONS = {
    "asset": [
        {"value": "new_market", "label": "New market", "types": ["asset.new_market"]},
        {"value": "market_exit", "label": "Market exit", "types": ["asset.market_exit"]},
        {"value": "pipeline", "label": "Pipeline / coming soon", "types": ["asset.pipeline_signal"]},
        {"value": "other", "label": "Other", "types": []},  # catchall when type not in above
    ],
    "talent": [
        {"value": "new_jobs", "label": "New jobs / hiring", "types": ["talent.hiring_surge", "talent.senior_hire_or_role_posted", "talent.new_capability"]},
        {"value": "removed_jobs", "label": "Removed jobs", "types": ["talent.jobs_removed"]},
        {"value": "other", "label": "Other", "types": []},
    ],
    "partner": [
        {"value": "partnerships", "label": "Partnerships", "types": ["partner.major_partnership"]},
        {"value": "partnership_surge", "label": "Partnership surge", "types": ["partner.partnership_surge"]},
        {"value": "other", "label": "Other", "types": []},
    ],
    "capital": [
        {"value": "fundraise", "label": "Fundraise / restructuring", "types": ["capital.fundraise_or_restructuring"]},
        {"value": "other", "label": "Other", "types": []},
    ],
    "narrative": [
        {"value": "priority_shift", "label": "Strategy / priority shift", "types": ["narrative.priority_shift"]},
        {"value": "homepage_updated", "label": "Homepage updated", "types": ["narrative.homepage_updated"]},
        {"value": "coming_soon", "label": "Coming soon", "types": ["narrative.coming_soon"]},
        {"value": "social_signal", "label": "Social signal", "types": ["narrative.social_signal"]},
        {"value": "other", "label": "Other", "types": []},
    ],
    "press": [
        {"value": "partnerships", "label": "Partnerships", "types": ["partner.major_partnership", "partner.partnership_surge"]},
        {"value": "market_launch", "label": "Market launch / expansion", "types": ["narrative.priority_shift"]},
        {"value": "funding", "label": "Funding / capital", "types": ["capital.fundraise_or_restructuring"]},
        {"value": "other", "label": "Interviews / press releases / other", "types": []},
    ],
    "public_record": [
        {"value": "filing", "label": "Filing", "types": ["public_record.filing"]},
        {"value": "other", "label": "Other", "types": []},
    ],
}


def _events_for_dashboard(session):
    """Load recent events for dashboard feed view: one-liner + date added + channel/type for filtering."""
    rows = (
        session.query(Event)
        .order_by(Event.detected_at.desc())
        .limit(500)
        .all()
    )
    competitor_ids = {e.competitor_id for e in rows}
    competitor_names = {}
    if competitor_ids:
        for c in session.query(Competitor).filter(Competitor.id.in_(competitor_ids)).all():
            competitor_names[c.id] = c.name
    channel_keys = set()
    events = []
    for e in rows:
        ch = CATEGORY_TO_CHANNEL.get(e.category, e.category)
        channel_keys.add(ch)
        events.append({
            "category": e.category,
            "channel_key": ch,
            "type": e.type,
            "severity": e.severity,
            "title": e.title,
            "summary": e.summary,
            "detected_at_str": e.detected_at.strftime("%Y-%m-%d"),
            "detected_at_short": e.detected_at.strftime("%b %d"),
            "competitor_name": competitor_names.get(e.competitor_id, ""),
        })
    # Order: Talent, Asset, Press, Public Record
    order = ("talent", "asset", "press", "public_record")
    feed_channels = sorted(channel_keys, key=lambda c: (order.index(c) if c in order else 99, c))
    return events, feed_channels


def _feed_topics_grouped(feed_channels):
    """Build topics for the Topics dropdown, grouped by channel: list of {channel_key, channel_label, topics: [{value, label, types}]}."""
    grouped = []
    for ch in feed_channels:
        label = FEED_CHANNEL_LABELS.get(ch, ch.replace("_", " ").title())
        topics = []
        for cat, opts in FEED_TOPIC_OPTIONS.items():
            if CATEGORY_TO_CHANNEL.get(cat) != ch:
                continue
            for opt in opts:
                topics.append({
                    "value": opt["value"],
                    "label": opt["label"],
                    "types": opt.get("types") or [],
                })
        if topics:
            grouped.append({"channel_key": ch, "channel_label": label, "topics": topics})
    return grouped


@router.get("/digest")
def digest(request: Request):
    with get_session() as session:
        recent_logs = session.query(RunLog).order_by(RunLog.created_at.desc()).limit(100).all()
        last_runs = {}
        for log in recent_logs:
            if log.channel not in last_runs:
                last_runs[log.channel] = _run_log_summary(log)
        all_competitors = session.query(Competitor).order_by(Competitor.created_at.desc()).all()
        nav_competitors = [{"id": c.id, "name": c.name, "created_at": c.created_at} for c in all_competitors]
        last_refreshed = get_last_refreshed(session)
        feed_events, feed_channels = _events_for_dashboard(session)
        feed_topics_grouped = _feed_topics_grouped(feed_channels)
    return request.app.state.templates.TemplateResponse(
        "digest.html",
        {
            "request": request,
            "last_runs": last_runs,
            "last_refreshed": last_refreshed,
            "nav_competitors": nav_competitors,
            "send_enabled": settings.digest_send_enabled,
            "feed_events": feed_events,
            "feed_channels": feed_channels,
            "feed_channel_labels": FEED_CHANNEL_LABELS,
            "feed_topics_grouped": feed_topics_grouped,
        },
    )


@router.post("/digest/send")
def digest_send(body: SendDigestBody):
    """Send the weekly digest to the given email address(es). Requires SMTP_* and MAIL_FROM to be set."""
    if not settings.digest_send_enabled:
        return JSONResponse(
            status_code=503,
            content={"ok": False, "error": "Email send is not configured. Set SMTP_HOST, SMTP_USER, SMTP_PASSWORD, and MAIL_FROM."},
        )
    to_raw = body.to if isinstance(body.to, list) else [body.to]
    to_emails = [e.strip() for e in to_raw if (e or "").strip()]
    success, message = send_weekly_digest(to_emails)
    if success:
        return JSONResponse(content={"ok": True, "message": message})
    return JSONResponse(status_code=400, content={"ok": False, "error": message})
