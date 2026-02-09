from datetime import datetime, timedelta
import json
from typing import Any, Optional

from .collectors.talent import collect_talent_snapshot, build_structured_json as build_talent_structured
from .collectors.asset import collect_asset_snapshot, build_structured_json as build_asset_structured
from .collectors.press import collect_press_snapshot, build_structured_json as build_press_structured
from .collectors.homepage import collect_homepage_snapshot, build_structured_json as build_homepage_structured
from .collectors.public_records import collect_public_records_snapshot, build_structured_json as build_public_records_structured
from .db import get_session
from .diff.talent_diff import diff_jobs, count_recent_by_capability
from .diff.asset_diff import diff_properties, extract_markets
from .diff.press_diff import diff_items
from .models import Competitor, SourceEndpoint, Snapshot, Event, Capability, RunLog
from .rules.talent_rules import (
    assign_job_flags,
    build_capability_event,
    build_hiring_surge_event,
    build_senior_event,
    build_strategic_role_event,
    recent_threshold_crossed,
)
from .rules.asset_rules import (
    build_new_market_event,
    build_market_exit_event,
    build_pipeline_event,
)
from .rules.press_rules import classify_press, build_press_event, is_executive_relevant
from .rules.homepage_rules import build_homepage_updated_event
from .rules.public_records_rules import build_filing_event


def load_latest_snapshot(session, competitor_id: int, channel: str) -> Optional[Snapshot]:
    return (
        session.query(Snapshot)
        .filter(Snapshot.competitor_id == competitor_id, Snapshot.channel == channel)
        .order_by(Snapshot.captured_at.desc())
        .first()
    )


def capability_seen(session, competitor_id: int, capability: str) -> bool:
    return (
        session.query(Capability)
        .filter(Capability.competitor_id == competitor_id, Capability.capability == capability)
        .first()
        is not None
    )


def store_capability(session, competitor_id: int, capability: str) -> None:
    session.add(Capability(competitor_id=competitor_id, capability=capability))


def persist_snapshot(session, competitor_id: int, channel: str, raw_content: str, raw_hash: str, structured_json: dict) -> Snapshot:
    snapshot = Snapshot(
        competitor_id=competitor_id,
        channel=channel,
        raw_content=raw_content,
        raw_hash=raw_hash,
        structured_json=structured_json,
    )
    session.add(snapshot)
    return snapshot


def _normalize_occurred_at(value: Any) -> Optional[datetime]:
    """DB expects datetime; rules may pass ms (int), iso str, or datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        # Lever etc. use milliseconds since epoch
        return datetime.utcfromtimestamp(value / 1000.0)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None
    return None


def create_event(session, competitor_id: int, event: dict) -> None:
    session.add(
        Event(
            competitor_id=competitor_id,
            category=event["category"],
            type=event["type"],
            severity=event["severity"],
            title=event["title"],
            summary=event["summary"],
            why_it_matters=event.get("why_it_matters"),
            evidence_json=event.get("evidence"),
            occurred_at=_normalize_occurred_at(event.get("occurred_at")),
        )
    )


def log_event(message: str, **fields: dict) -> None:
    payload = {"message": message, "ts": datetime.utcnow().isoformat(), **fields}
    print(json.dumps(payload))


def log_run(session, competitor_id: Optional[int], channel: str, status: str, message: Optional[str] = None, extra: Optional[dict] = None) -> None:
    session.add(
        RunLog(
            competitor_id=competitor_id,
            channel=channel,
            status=status,
            message=message,
            extra_json=extra,
        )
    )


DEDUPE_WINDOWS_DAYS = {
    "talent.senior_hire_or_role_posted": 30,
    "talent.new_capability": 90,
    "talent.hiring_surge": 30,
    "asset.new_market": 60,
    "asset.market_exit": 90,
    "asset.pipeline_signal": 30,
    "partner.major_partnership": 60,
    "partner.partnership_surge": 60,
    "capital.fundraise_or_restructuring": 90,
    "narrative.priority_shift": 90,
    "narrative.homepage_updated": 14,
    "public_record.filing": 60,
}


def event_recently_created(
    session,
    competitor_id: int,
    event_type: str,
    title: str,
    window_days: int = 30,
) -> bool:
    cutoff = datetime.utcnow() - timedelta(days=window_days)
    return (
        session.query(Event)
        .filter(
            Event.competitor_id == competitor_id,
            Event.type == event_type,
            Event.title == title,
            Event.detected_at >= cutoff,
        )
        .first()
        is not None
    )


def dedupe_window_for(event_type: str) -> int:
    return DEDUPE_WINDOWS_DAYS.get(event_type, 30)


def should_skip_due_to_hash(
    session,
    competitor_id: int,
    channel: str,
    raw_hash: Optional[str],
) -> bool:
    if not raw_hash:
        return False
    latest = load_latest_snapshot(session, competitor_id, channel)
    if not latest:
        return False
    return latest.raw_hash == raw_hash


def compute_capability_counts(jobs: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for job in jobs:
        capability = job.get("capability_bucket")
        if not capability:
            continue
        counts[capability] = counts.get(capability, 0) + 1
    return counts


def run_talent() -> None:
    with get_session() as session:
        competitors = session.query(Competitor).order_by(Competitor.name.asc()).all()
        for competitor in competitors:
            endpoints = [
                endpoint
                for endpoint in competitor.source_endpoints
                if endpoint.channel == "talent"
            ]
            for endpoint in endpoints:
                print(
                    f"[{datetime.utcnow().isoformat()}] talent run: "
                    f"{competitor.name} {endpoint.url} ({endpoint.confidence})"
                )
                try:
                    snapshot = collect_talent_snapshot(endpoint.url)
                except Exception as exc:
                    log_run(
                        session,
                        competitor.id,
                        "talent",
                        "error",
                        message=str(exc),
                        extra={"url": endpoint.url},
                    )
                    continue
                if should_skip_due_to_hash(session, competitor.id, "talent", snapshot.get("raw_hash")):
                    log_event(
                        "snapshot_unchanged",
                        competitor=competitor.name,
                        channel="talent",
                        url=endpoint.url,
                    )
                    log_run(
                        session,
                        competitor.id,
                        "talent",
                        "skipped",
                        message="snapshot_unchanged",
                        extra={"url": endpoint.url},
                    )
                    continue
                structured = build_talent_structured(snapshot)

                latest = load_latest_snapshot(session, competitor.id, "talent")
                previous_jobs = (latest.structured_json or {}).get("jobs", []) if latest else []
                current_jobs = structured.get("jobs", [])

                current_jobs = [assign_job_flags(job) for job in current_jobs]
                previous_jobs = [assign_job_flags(job) for job in previous_jobs]
                structured["jobs"] = current_jobs

                persist_snapshot(
                    session,
                    competitor.id,
                    "talent",
                    snapshot.get("raw_content") or "",
                    snapshot.get("raw_hash") or "",
                    structured,
                )

                diff = diff_jobs(previous_jobs, current_jobs)
                added_jobs = diff["added"]

                previous_counts = compute_capability_counts(previous_jobs)
                current_counts = compute_capability_counts(current_jobs)

                # New capability detection
                for capability in current_counts.keys():
                    if not capability_seen(session, competitor.id, capability):
                        store_capability(session, competitor.id, capability)
                        event = build_capability_event(capability)
                        if not event_recently_created(
                            session,
                            competitor.id,
                            event["type"],
                            event["title"],
                            window_days=dedupe_window_for(event["type"]),
                        ):
                            create_event(session, competitor.id, event)

                # Senior / strategic roles
                for job in added_jobs:
                    if job.get("is_senior"):
                        event = build_senior_event(job)
                        if not event_recently_created(
                            session,
                            competitor.id,
                            event["type"],
                            event["title"],
                            window_days=dedupe_window_for(event["type"]),
                        ):
                            create_event(session, competitor.id, event)
                    elif job.get("is_strategic"):
                        event = build_strategic_role_event(job)
                        if not event_recently_created(
                            session,
                            competitor.id,
                            event["type"],
                            event["title"],
                            window_days=dedupe_window_for(event["type"]),
                        ):
                            create_event(session, competitor.id, event)

                # Hiring surge
                for capability in current_counts.keys():
                    previous_count = count_recent_by_capability(previous_jobs, capability, days=30)
                    current_count = count_recent_by_capability(current_jobs, capability, days=30)
                    if recent_threshold_crossed(previous_count, current_count, threshold=5):
                        event = build_hiring_surge_event(capability, current_count)
                        if not event_recently_created(
                            session,
                            competitor.id,
                            event["type"],
                            event["title"],
                            window_days=dedupe_window_for(event["type"]),
                        ):
                            create_event(session, competitor.id, event)

                log_run(
                    session,
                    competitor.id,
                    "talent",
                    "success",
                    extra={"added_jobs": len(added_jobs)},
                )


def run_asset() -> None:
    with get_session() as session:
        competitors = session.query(Competitor).order_by(Competitor.name.asc()).all()
        for competitor in competitors:
            endpoints = [
                endpoint
                for endpoint in competitor.source_endpoints
                if endpoint.channel == "asset"
            ]
            for endpoint in endpoints:
                print(
                    f"[{datetime.utcnow().isoformat()}] asset run: "
                    f"{competitor.name} {endpoint.url} ({endpoint.confidence})"
                )
                try:
                    snapshot = collect_asset_snapshot(
                        endpoint.url,
                        js_required=endpoint.js_required,
                        use_sitemap_first=endpoint.use_sitemap_first,
                        extra_options=endpoint.extra_options,
                    )
                except Exception as exc:
                    log_run(
                        session,
                        competitor.id,
                        "asset",
                        "error",
                        message=str(exc),
                        extra={"url": endpoint.url},
                    )
                    continue
                if should_skip_due_to_hash(session, competitor.id, "asset", snapshot.get("raw_hash")):
                    log_event(
                        "snapshot_unchanged",
                        competitor=competitor.name,
                        channel="asset",
                        url=endpoint.url,
                    )
                    log_run(
                        session,
                        competitor.id,
                        "asset",
                        "skipped",
                        message="snapshot_unchanged",
                        extra={"url": endpoint.url},
                    )
                    continue
                structured = build_asset_structured(snapshot)

                latest = load_latest_snapshot(session, competitor.id, "asset")
                previous_props = (latest.structured_json or {}).get("properties", []) if latest else []
                current_props = structured.get("properties", [])

                persist_snapshot(
                    session,
                    competitor.id,
                    "asset",
                    snapshot.get("raw_content") or "",
                    snapshot.get("raw_hash") or "",
                    structured,
                )

                diff = diff_properties(previous_props, current_props)
                added_props = diff["added"]

                previous_markets = extract_markets(previous_props)
                current_markets = extract_markets(current_props)

                # New market detection
                for market in current_markets:
                    if market not in previous_markets:
                        event = build_new_market_event(market)
                        if not event_recently_created(
                            session,
                            competitor.id,
                            event["type"],
                            event["title"],
                            window_days=dedupe_window_for(event["type"]),
                        ):
                            create_event(session, competitor.id, event)

                # Pipeline signals
                for prop in added_props:
                    status = (prop.get("status") or "").lower()
                    name = (prop.get("name") or "").lower()
                    if "coming soon" in status or "coming soon" in name:
                        event = build_pipeline_event(prop)
                        if not event_recently_created(
                            session,
                            competitor.id,
                            event["type"],
                            event["title"],
                            window_days=dedupe_window_for(event["type"]),
                        ):
                            create_event(session, competitor.id, event)

                # Market exit (requires two consecutive runs showing removal)
                if latest:
                    removed_markets = previous_markets - current_markets
                    if removed_markets:
                        older = (
                            session.query(Snapshot)
                            .filter(Snapshot.competitor_id == competitor.id, Snapshot.channel == "asset")
                            .order_by(Snapshot.captured_at.desc())
                            .offset(1)
                            .first()
                        )
                        if older:
                            older_markets = extract_markets((older.structured_json or {}).get("properties", []))
                            confirmed_exits = [m for m in removed_markets if m not in older_markets]
                            for market in confirmed_exits:
                                event = build_market_exit_event(market)
                                if not event_recently_created(
                                    session,
                                    competitor.id,
                                    event["type"],
                                    event["title"],
                                    window_days=dedupe_window_for(event["type"]),
                                ):
                                    create_event(session, competitor.id, event)

                log_run(
                    session,
                    competitor.id,
                    "asset",
                    "success",
                    extra={"added_properties": len(added_props)},
                )


def run_press() -> None:
    with get_session() as session:
        competitors = session.query(Competitor).order_by(Competitor.name.asc()).all()
        for competitor in competitors:
            endpoints = [
                endpoint
                for endpoint in competitor.source_endpoints
                if endpoint.channel == "press"
            ]
            for endpoint in endpoints:
                print(
                    f"[{datetime.utcnow().isoformat()}] press run: "
                    f"{competitor.name} {endpoint.url} ({endpoint.confidence})"
                )
                try:
                    snapshot = collect_press_snapshot(endpoint.url)
                except Exception as exc:
                    log_run(
                        session,
                        competitor.id,
                        "press",
                        "error",
                        message=str(exc),
                        extra={"url": endpoint.url},
                    )
                    continue
                if should_skip_due_to_hash(session, competitor.id, "press", snapshot.get("raw_hash")):
                    log_event(
                        "snapshot_unchanged",
                        competitor=competitor.name,
                        channel="press",
                        url=endpoint.url,
                    )
                    log_run(
                        session,
                        competitor.id,
                        "press",
                        "skipped",
                        message="snapshot_unchanged",
                        extra={"url": endpoint.url},
                    )
                    continue
                structured = build_press_structured(snapshot)

                latest = load_latest_snapshot(session, competitor.id, "press")
                previous_items = (latest.structured_json or {}).get("items", []) if latest else []
                current_items = structured.get("items", [])

                persist_snapshot(
                    session,
                    competitor.id,
                    "press",
                    snapshot.get("raw_content") or "",
                    snapshot.get("raw_hash") or "",
                    structured,
                )

                diff = diff_items(previous_items, current_items)
                added_items = diff["added"]

                for item in added_items:
                    category = classify_press(item)
                    if not category:
                        continue
                    if not is_executive_relevant(item):
                        continue
                    event = build_press_event(category, item)
                    if not event_recently_created(
                        session,
                        competitor.id,
                        event["type"],
                        event["title"],
                        window_days=dedupe_window_for(event["type"]),
                    ):
                        create_event(session, competitor.id, event)

                log_run(
                    session,
                    competitor.id,
                    "press",
                    "success",
                    extra={"added_items": len(added_items)},
                )


def run_homepage() -> None:
    with get_session() as session:
        competitors = session.query(Competitor).order_by(Competitor.name.asc()).all()
        for competitor in competitors:
            endpoints = [
                ep
                for ep in competitor.source_endpoints
                if ep.channel == "homepage"
            ]
            for endpoint in endpoints:
                print(
                    f"[{datetime.utcnow().isoformat()}] homepage run: "
                    f"{competitor.name} {endpoint.url}"
                )
                try:
                    snapshot = collect_homepage_snapshot(
                        endpoint.url,
                        js_required=getattr(endpoint, "js_required", False),
                    )
                except Exception as exc:
                    log_run(
                        session,
                        competitor.id,
                        "homepage",
                        "error",
                        message=str(exc),
                        extra={"url": endpoint.url},
                    )
                    continue
                if should_skip_due_to_hash(session, competitor.id, "homepage", snapshot.get("raw_hash")):
                    log_run(
                        session,
                        competitor.id,
                        "homepage",
                        "skipped",
                        message="snapshot_unchanged",
                        extra={"url": endpoint.url},
                    )
                    continue
                structured = build_homepage_structured(snapshot)
                persist_snapshot(
                    session,
                    competitor.id,
                    "homepage",
                    snapshot.get("raw_content") or "",
                    snapshot.get("raw_hash") or "",
                    structured,
                )
                event = build_homepage_updated_event(snapshot.get("source_url") or endpoint.url)
                if not event_recently_created(
                    session,
                    competitor.id,
                    event["type"],
                    event["title"],
                    window_days=dedupe_window_for(event["type"]),
                ):
                    create_event(session, competitor.id, event)
                log_run(
                    session,
                    competitor.id,
                    "homepage",
                    "success",
                    extra={"source_url": snapshot.get("source_url")},
                )


def run_public_records() -> None:
    with get_session() as session:
        competitors = session.query(Competitor).order_by(Competitor.name.asc()).all()
        for competitor in competitors:
            endpoints = [
                ep
                for ep in competitor.source_endpoints
                if ep.channel == "public_records"
            ]
            for endpoint in endpoints:
                print(
                    f"[{datetime.utcnow().isoformat()}] public_records run: "
                    f"{competitor.name} {endpoint.url}"
                )
                try:
                    snapshot = collect_public_records_snapshot(endpoint.url)
                except Exception as exc:
                    log_run(
                        session,
                        competitor.id,
                        "public_records",
                        "error",
                        message=str(exc),
                        extra={"url": endpoint.url},
                    )
                    continue
                if should_skip_due_to_hash(session, competitor.id, "public_records", snapshot.get("raw_hash")):
                    log_run(
                        session,
                        competitor.id,
                        "public_records",
                        "skipped",
                        message="snapshot_unchanged",
                        extra={"url": endpoint.url},
                    )
                    continue
                structured = build_public_records_structured(snapshot)
                latest = load_latest_snapshot(session, competitor.id, "public_records")
                previous_items = (latest.structured_json or {}).get("items", []) if latest else []
                current_items = structured.get("items", [])

                persist_snapshot(
                    session,
                    competitor.id,
                    "public_records",
                    snapshot.get("raw_content") or "",
                    snapshot.get("raw_hash") or "",
                    structured,
                )

                diff = diff_items(previous_items, current_items)
                added_items = diff["added"]

                for item in added_items:
                    event = build_filing_event(item)
                    if not event_recently_created(
                        session,
                        competitor.id,
                        event["type"],
                        event["title"],
                        window_days=dedupe_window_for(event["type"]),
                    ):
                        create_event(session, competitor.id, event)

                log_run(
                    session,
                    competitor.id,
                    "public_records",
                    "success",
                    extra={"added_items": len(added_items)},
                )


def run(channel: Optional[str] = None) -> None:
    if channel in (None, "talent"):
        run_talent()
    if channel in (None, "asset"):
        run_asset()
    if channel in (None, "press"):
        run_press()
    if channel in (None, "homepage"):
        run_homepage()
    if channel in (None, "public_records"):
        run_public_records()
    if channel is not None and channel not in ("talent", "asset", "press", "homepage", "public_records"):
        print(f"[{datetime.utcnow().isoformat()}] unknown channel: {channel}")


if __name__ == "__main__":
    run()
