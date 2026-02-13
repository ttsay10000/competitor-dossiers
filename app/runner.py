from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import Any, Callable, Optional
from urllib.parse import urljoin, urlparse

from .collectors.talent import collect_talent_snapshot, build_structured_json as build_talent_structured
from .collectors.asset import collect_asset_snapshot, build_structured_json as build_asset_structured
from .collectors.press import build_structured_json as build_press_structured
from .collectors.global_press import collect_google_news_items, collect_prnewswire_items
from .collectors.homepage import (
    build_composite_hash,
    build_structured_json as build_homepage_structured,
    collect_homepage_snapshot,
)
from .collectors.public_records import collect_public_records_snapshot, build_structured_json as build_public_records_structured
from .collectors.reviews import collect_property_review, build_structured_json as build_reviews_structured
from .collectors.social import collect_social_feed, build_structured_json as build_social_structured
from .config import settings
from .db import get_session
from .diff.talent_diff import diff_jobs, count_recent_by_capability
from .diff.asset_diff import diff_properties, extract_markets
from .llm_structured import (
    enrich_properties_with_llm,
    enrich_properties_url_and_rules_only,
    enrich_jobs_with_llm,
    enrich_press_items_with_llm,
    enrich_social_posts_with_llm,
    interpret_website_change,
    summarize_top_news_llm,
)
from .utils import parse_url_context
from .diff.press_diff import diff_items
from .diff.social_diff import diff_social_posts
from sqlalchemy.orm import selectinload

from .models import Competitor, CompetitorReviewProperty, SourceEndpoint, Snapshot, Event, Capability, RunLog
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
from .rules.homepage_rules import (
    build_coming_soon_event,
    build_homepage_updated_event,
    detect_coming_soon_phrases,
)
from .rules.public_records_rules import build_filing_event
from .rules.social_rules import build_social_signal_event

# All channels that support per-competitor seed baseline (SEED_MODE): first snapshot
# per competitor/channel persists as baseline; no diff or events until the next run.
RUNNER_CHANNELS = ("talent", "asset", "press", "homepage", "public_records", "reviews", "social")

# Competitors that skip LLM for asset enrichment and use only URL/HTML + rule-based state (e.g. Vacasa).
# All other competitors use enrich_properties_with_llm. Add names here normalized to lowercase.
ASSET_ENRICH_URL_AND_RULES_ONLY = frozenset({"vacasa"})

# Common paths to probe for digital footprint when product_paths is not set per endpoint.
# Crawled for every competitor (homepage or primary_domain) to detect changes / coming-soon signals.
COMMON_HOMEPAGE_PATHS = [
    "/locations",
    "/coming-soon",
    "/product",
    "/features",
    "/about",
    "/blog",
]


def load_latest_snapshot(session, competitor_id: int, channel: str) -> Optional[Snapshot]:
    return (
        session.query(Snapshot)
        .filter(Snapshot.competitor_id == competitor_id, Snapshot.channel == channel)
        .order_by(Snapshot.captured_at.desc())
        .first()
    )


def clear_latest_snapshots(session, channel: Optional[str] = None) -> int:
    """
    Delete the latest snapshot per competitor for the given channel (or all channels if channel is None).
    Use before a run so snapshot_unchanged is not triggered and enrichment re-runs.
    Returns number of snapshots deleted.
    """
    competitors = session.query(Competitor).all()
    deleted = 0
    channels = [channel] if channel else list(RUNNER_CHANNELS)
    for c in competitors:
        for ch in channels:
            latest = load_latest_snapshot(session, c.id, ch)
            if latest:
                session.delete(latest)
                deleted += 1
    return deleted


def clear_all_snapshots(session, channel: Optional[str] = None) -> int:
    """
    Delete every snapshot for the given channel (or all channels if channel is None).
    Use for "force reset and establish baselines" so the next run has no previous
    snapshot to compare to—no hash-based skip and no re-use of older snapshots.
    Returns number of snapshots deleted.
    """
    q = session.query(Snapshot)
    if channel is not None:
        q = q.filter(Snapshot.channel == channel)
    count = q.count()
    q.delete(synchronize_session=False)
    return count


def clear_all_events(session) -> int:
    """
    Delete all events from the events table. Used on force refresh so the new
    baseline run does not carry over previous detected events.
    Returns number of events deleted.
    """
    count = session.query(Event).count()
    session.query(Event).delete(synchronize_session=False)
    return count


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
        return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc)
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
            summary=event.get("summary", ""),
            why_it_matters=event.get("why_it_matters"),
            evidence_json=event.get("evidence"),
            occurred_at=_normalize_occurred_at(event.get("occurred_at")),
        )
    )


def log_event(message: str, **fields: dict) -> None:
    payload = {"message": message, "ts": datetime.now(timezone.utc).isoformat(), **fields}
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
    "narrative.coming_soon": 30,
    "narrative.social_signal": 30,
    "public_record.filing": 60,
}


def _parse_press_date(value: Any) -> Optional[datetime]:
    """Best-effort parse for press item dates (RSS or ISO strings). Always returns UTC-aware or None."""
    if value is None:
        return None
    dt: Optional[datetime] = None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        try:
            dt = datetime.fromtimestamp(value / 1000.0, tz=timezone.utc)
        except Exception:
            return None
    elif isinstance(value, str):
        val = value.strip()
        if not val:
            return None
        try:
            dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
        except Exception:
            return None
    if dt is None:
        return None
    # Normalize to UTC-aware so comparisons with cutoff (aware) never raise TypeError
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt


def _build_press_raw_hash(items: list[dict]) -> str:
    """
    Compute a stable hash for a set of press items so we can skip unchanged snapshots.
    Uses provider + url/link + title + date; order-independent.
    """
    if not items:
        return ""
    keys: list[str] = []
    for it in items:
        provider = (it.get("provider") or "").strip()
        url = (it.get("url") or it.get("link") or "").strip()
        title = (it.get("title") or "").strip()
        date = (it.get("date") or "").strip() if isinstance(it.get("date"), str) else str(it.get("date") or "")
        keys.append(f"{provider}|{url}|{title}|{date}")
    keys.sort()
    payload = "\n".join(keys)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def event_recently_created(
    session,
    competitor_id: int,
    event_type: str,
    title: str,
    window_days: int = 30,
) -> bool:
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
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


def event_already_created_since_baseline(
    session,
    competitor: Competitor,
    event_type: str,
    title: str,
    *,
    fallback_window_days: int = 14,
) -> bool:
    """
    For website-change events: have we already created this (type + title) since the last
    refresh? Uses competitor.reporting_baseline_at so dedupe is per refresh cycle, not calendar.
    If no baseline is set, falls back to time-based (fallback_window_days).
    """
    baseline = getattr(competitor, "reporting_baseline_at", None)
    if baseline is not None:
        cutoff = baseline
    else:
        cutoff = datetime.now(timezone.utc) - timedelta(days=fallback_window_days)
    return (
        session.query(Event)
        .filter(
            Event.competitor_id == competitor.id,
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


def _endpoints_ordered(endpoints: list[SourceEndpoint]) -> list[SourceEndpoint]:
    """Return endpoints sorted by id (primary first). Used for talent/asset with fallback to secondary."""
    return sorted(endpoints, key=lambda e: e.id)


def _is_cancelled(is_cancelled: Optional[Callable[[], bool]]) -> bool:
    """Return True if an optional cancel callable is set and returns True (run was cancelled)."""
    return bool(is_cancelled and is_cancelled())


def _filter_competitors_by_name(competitors: list, competitor_name: Optional[str]) -> list:
    """Filter competitors by name: exact match (case-insensitive) or first-word match so 'Lark' matches 'Lark Hotels'."""
    if not competitor_name or not competitors:
        return list(competitors)
    want = competitor_name.strip().lower()
    if not want:
        return list(competitors)
    out = []
    for c in competitors:
        n = (c.name or "").strip().lower()
        if n == want:
            out.append(c)
            continue
        # First word of stored name matches filter (e.g. --competitor Lark matches "Lark Hotels")
        first = n.split()[0] if n else ""
        if first == want:
            out.append(c)
    return out


def run_talent(competitor_name: Optional[str] = None, is_cancelled: Optional[Callable[[], bool]] = None) -> None:
    if _is_cancelled(is_cancelled):
        return
    with get_session() as session:
        competitors = session.query(Competitor).order_by(Competitor.name.asc()).all()
        if competitor_name:
            competitors = _filter_competitors_by_name(competitors, competitor_name)
        else:
            competitors = [c for c in competitors if getattr(c, "is_active", True)]
        if not competitors:
            log_event("competitor_not_found", competitor_filter=competitor_name)
            return
        for competitor in competitors:
            if _is_cancelled(is_cancelled):
                return
            endpoints_ordered = _endpoints_ordered([
                e for e in competitor.source_endpoints if e.channel == "talent"
            ])
            if not endpoints_ordered:
                continue

            print(
                f"\n[{datetime.now(timezone.utc).isoformat()}] === TALENT: {competitor.name} "
                f"({len(endpoints_ordered)} endpoint(s)) ==="
            )

            snapshot = None
            endpoint_used = None
            structured = None
            current_jobs = []

            for endpoint in endpoints_ordered:
                if _is_cancelled(is_cancelled):
                    return
                print(
                    f"[talent] Step 0 — Trying endpoint: {endpoint.url} ({endpoint.confidence})"
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
                raw_jobs = snapshot.get("jobs") or []
                provider = snapshot.get("provider") or "unknown"
                print(f"[talent] Step 1 — Collect: {len(raw_jobs)} jobs (provider: {provider})")
                structured = build_talent_structured(snapshot)
                before_enrich = len(structured.get("jobs") or [])
                if before_enrich > 150:
                    print(f"[talent] Step 2 — Sending first 150 of {before_enrich} jobs to LLM; rest use rule-based classification.")
                structured["jobs"] = enrich_jobs_with_llm(structured.get("jobs") or [])
                current_jobs = structured.get("jobs") or []
                after_enrich = len(current_jobs)
                print(f"[talent] Step 2 — build_structured: {before_enrich} → enrich_jobs_with_llm: {after_enrich} jobs")
                if current_jobs:
                    endpoint_used = endpoint
                    break
                if endpoint is endpoints_ordered[-1]:
                    endpoint_used = endpoint
                    break
                log_run(
                    session,
                    competitor.id,
                    "talent",
                    "skipped",
                    message="primary_returned_zero_jobs_trying_secondary",
                    extra={"url": endpoint.url},
                )

            if endpoint_used is None:
                continue

            latest = load_latest_snapshot(session, competitor.id, "talent")
            previous_jobs = (latest.structured_json or {}).get("jobs", []) if latest else []
            current_jobs = structured.get("jobs") or []

            existing_job_count = 0
            if not current_jobs:
                # Match dossier limit so we don't persist empty when a good snapshot is just beyond the window (e.g. Blueground JS careers).
                for s in (
                    session.query(Snapshot)
                    .filter(Snapshot.competitor_id == competitor.id, Snapshot.channel == "talent")
                    .order_by(Snapshot.captured_at.desc())
                    .limit(100)
                    .all()
                ):
                    jobs_in = (s.structured_json or {}).get("jobs", [])
                    if jobs_in and any(isinstance(j, dict) for j in jobs_in):
                        existing_job_count = len([j for j in jobs_in if isinstance(j, dict)])
                        break
            if not current_jobs and existing_job_count:
                log_run(
                    session,
                    competitor.id,
                    "talent",
                    "skipped",
                    message="empty_snapshot_kept_previous",
                    extra={"url": endpoint_used.url, "existing_job_count": existing_job_count},
                )
                continue

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
            if not current_jobs and snapshot.get("playwright_fallback"):
                log_run(
                    session,
                    competitor.id,
                    "talent",
                    "skipped",
                    message="talent_js_fallback_zero_jobs",
                    extra={
                        "url": endpoint_used.url,
                        "hint": "Cron needs playwright package + 'playwright install chromium' in build and PLAYWRIGHT_ENABLED=true",
                    },
                )
            seed_mode = getattr(settings, "seed_mode", False)
            is_first_snapshot = latest is None
            if seed_mode and is_first_snapshot:
                print(f"[talent] Step 3 — Seed baseline. Total jobs: {len(current_jobs)}")
                if not current_jobs:
                    log_run(
                        session,
                        competitor.id,
                        "talent",
                        "error",
                        message="No data populated - please check",
                        extra={"jobs": 0, "url": endpoint_used.url},
                    )
                else:
                    log_run(
                        session,
                        competitor.id,
                        "talent",
                        "success",
                        message="talent_seed_baseline",
                        extra={"jobs": len(current_jobs)},
                    )
                continue

            diff = diff_jobs(previous_jobs, current_jobs)
            added_jobs = diff["added"]
            removed_jobs = diff.get("removed", [])
            print(
                f"[talent] Step 3 — Persist. Diff: +{len(added_jobs)} added, -{len(removed_jobs)} removed. "
                f"Total jobs: {len(current_jobs)}"
            )
            previous_counts = compute_capability_counts(previous_jobs)
            current_counts = compute_capability_counts(current_jobs)

            for capability in current_counts.keys():
                if not capability_seen(session, competitor.id, capability):
                    store_capability(session, competitor.id, capability)
                    event = build_capability_event(capability)
                    if not settings.seed_mode and not event_recently_created(
                        session,
                        competitor.id,
                        event["type"],
                        event["title"],
                        window_days=dedupe_window_for(event["type"]),
                    ):
                        create_event(session, competitor.id, event)
            for job in added_jobs:
                if job.get("is_senior"):
                    event = build_senior_event(job)
                    if not settings.seed_mode and not event_recently_created(
                        session,
                        competitor.id,
                        event["type"],
                        event["title"],
                        window_days=dedupe_window_for(event["type"]),
                    ):
                        create_event(session, competitor.id, event)
                elif job.get("is_strategic"):
                    event = build_strategic_role_event(job)
                    if not settings.seed_mode and not event_recently_created(
                        session,
                        competitor.id,
                        event["type"],
                        event["title"],
                        window_days=dedupe_window_for(event["type"]),
                    ):
                        create_event(session, competitor.id, event)
            for capability in current_counts.keys():
                previous_count = count_recent_by_capability(previous_jobs, capability, days=30)
                current_count = count_recent_by_capability(current_jobs, capability, days=30)
                if recent_threshold_crossed(previous_count, current_count, threshold=5):
                    event = build_hiring_surge_event(capability, current_count)
                    if not settings.seed_mode and not event_recently_created(
                        session,
                        competitor.id,
                        event["type"],
                        event["title"],
                        window_days=dedupe_window_for(event["type"]),
                    ):
                        create_event(session, competitor.id, event)

            print(f"[talent] Step 4 — Done. Total jobs: {len(current_jobs)}")
            if not current_jobs:
                log_run(
                    session,
                    competitor.id,
                    "talent",
                    "error",
                    message="No data populated - please check",
                    extra={"jobs": 0, "url": endpoint_used.url},
                )
            else:
                log_run(
                    session,
                    competitor.id,
                    "talent",
                    "success",
                    message=f"Collected {len(current_jobs)} jobs",
                    extra={"added_jobs": len(added_jobs), "jobs": len(current_jobs), "url": endpoint_used.url},
                )


def run_asset(competitor_name: Optional[str] = None, is_cancelled: Optional[Callable[[], bool]] = None) -> None:
    if _is_cancelled(is_cancelled):
        return
    with get_session() as session:
        competitors = (
            session.query(Competitor)
            .options(selectinload(Competitor.source_endpoints))
            .order_by(Competitor.name.asc())
            .all()
        )
        if competitor_name:
            competitors = _filter_competitors_by_name(competitors, competitor_name)
        else:
            competitors = [c for c in competitors if getattr(c, "is_active", True)]
        if not competitors:
            log_event("competitor_not_found", competitor_filter=competitor_name)
            return
        for competitor in competitors:
            if _is_cancelled(is_cancelled):
                return
            endpoints_ordered = _endpoints_ordered([
                e for e in (competitor.source_endpoints or []) if e.channel == "asset"
            ])
            if not endpoints_ordered:
                continue

            print(
                f"\n[{datetime.now(timezone.utc).isoformat()}] === ASSET: {competitor.name} "
                f"({len(endpoints_ordered)} endpoint(s)) ==="
            )

            snapshot = None
            endpoint_used = None
            structured = None
            current_props = []

            for endpoint in endpoints_ordered:
                if _is_cancelled(is_cancelled):
                    return
                print(
                    f"[asset] Step 0 — Trying endpoint: {endpoint.url} ({endpoint.confidence})"
                )
                # Lark-style sources need Playwright + load_more for full portfolio; without it we get partial HTML only.
                opts = getattr(endpoint, "extra_options", None) or {}
                if endpoint.js_required and opts.get("load_more") and not getattr(settings, "playwright_enabled", False):
                    print(
                        f"[asset] {competitor.name}: JS+load_more source but PLAYWRIGHT_ENABLED is false; "
                        "expect partial property count. Set PLAYWRIGHT_ENABLED=true for full list."
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
                # Use a fresh session for DB after long-running collection (avoids stale connection / SSL EOF)
                with get_session() as db_session:
                    if should_skip_due_to_hash(db_session, competitor.id, "asset", snapshot.get("raw_hash")):
                        log_event(
                            "snapshot_unchanged",
                            competitor=competitor.name,
                            channel="asset",
                            url=endpoint.url,
                        )
                        log_run(
                            db_session,
                            competitor.id,
                            "asset",
                            "skipped",
                            message="snapshot_unchanged",
                            extra={"url": endpoint.url},
                        )
                        continue
                raw_count = len(snapshot.get("properties") or [])
                note = snapshot.get("note") or "unknown"
                print(f"[asset] Step 1 — Collect: {raw_count} properties (strategy: {note})")
                structured = build_asset_structured(snapshot)
                before_enrich = len(structured.get("properties") or [])
                competitor_name_normalized = (competitor.name or "").strip().lower()
                if competitor_name_normalized in ASSET_ENRICH_URL_AND_RULES_ONLY:
                    structured["properties"] = enrich_properties_url_and_rules_only(
                        structured.get("properties") or [],
                    )
                    print(f"[asset] Step 2 — build_structured: {before_enrich} → enrich (URL/rules only, no LLM): {len(structured.get('properties') or [])} properties")
                else:
                    structured["properties"] = enrich_properties_with_llm(
                        structured.get("properties") or [],
                        raw_content=snapshot.get("raw_content"),
                    )
                    current_props = structured.get("properties") or []
                    print(f"[asset] Step 2 — build_structured: {before_enrich} → enrich_properties_with_llm: {len(current_props)} properties")
                current_props = structured.get("properties") or []
                if current_props:
                    endpoint_used = endpoint
                    break
                if endpoint is endpoints_ordered[-1]:
                    endpoint_used = endpoint
                    break
                log_run(
                    session,
                    competitor.id,
                    "asset",
                    "skipped",
                    message="primary_returned_zero_properties_trying_secondary",
                    extra={"url": endpoint.url},
                )

            if endpoint_used is None:
                continue

            # Use a fresh session for all DB work after long-running collection (avoids stale connection / SSL EOF)
            with get_session() as db_session:
                latest = load_latest_snapshot(db_session, competitor.id, "asset")
                previous_props = (latest.structured_json or {}).get("properties", []) if latest else []
                current_props = structured.get("properties") or []

                persist_snapshot(
                    db_session,
                    competitor.id,
                    "asset",
                    snapshot.get("raw_content") or "",
                    snapshot.get("raw_hash") or "",
                    structured,
                )

                seed_mode = getattr(settings, "seed_mode", False)
                is_first_snapshot = latest is None
                if seed_mode and is_first_snapshot:
                    print(f"[asset] Step 3 — Seed baseline. Total properties: {len(current_props)}")
                    if not current_props:
                        log_run(
                            db_session,
                            competitor.id,
                            "asset",
                            "error",
                            message="No data populated - please check",
                            extra={"properties": 0, "url": endpoint_used.url},
                        )
                    else:
                        log_run(
                            db_session,
                            competitor.id,
                            "asset",
                            "success",
                            message="asset_seed_baseline",
                            extra={"properties": len(current_props)},
                        )
                    continue

                diff = diff_properties(previous_props, current_props)
                added_props = diff["added"]
                removed_props = diff["removed"]
                print(
                    f"[asset] Step 3 — Persist. Diff: +{len(added_props)} added, -{len(removed_props)} removed. "
                    f"Total properties: {len(current_props)}"
                )
                previous_markets = extract_markets(previous_props)
                current_markets = extract_markets(current_props)

                for market in current_markets:
                    if market not in previous_markets:
                        event = build_new_market_event(market)
                        if not settings.seed_mode and not event_recently_created(
                            db_session,
                            competitor.id,
                            event["type"],
                            event["title"],
                            window_days=dedupe_window_for(event["type"]),
                        ):
                            create_event(db_session, competitor.id, event)
                for prop in added_props:
                    status = (prop.get("status") or "").lower()
                    name = (prop.get("name") or "").lower()
                    if "coming soon" in status or "coming soon" in name:
                        event = build_pipeline_event(prop)
                        if not settings.seed_mode and not event_recently_created(
                            db_session,
                            competitor.id,
                            event["type"],
                            event["title"],
                            window_days=dedupe_window_for(event["type"]),
                        ):
                            create_event(db_session, competitor.id, event)
                if latest:
                    removed_markets = previous_markets - current_markets
                    if removed_markets:
                        older = (
                            db_session.query(Snapshot)
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
                                if not settings.seed_mode and not event_recently_created(
                                    db_session,
                                    competitor.id,
                                    event["type"],
                                    event["title"],
                                    window_days=dedupe_window_for(event["type"]),
                                ):
                                    create_event(db_session, competitor.id, event)

                print(f"[asset] Step 4 — Done. Total properties: {len(current_props)}")
                if not current_props:
                    log_run(
                        db_session,
                        competitor.id,
                        "asset",
                        "error",
                        message="No data populated - please check",
                        extra={"properties": 0, "url": endpoint_used.url},
                    )
                else:
                    log_run(
                        db_session,
                        competitor.id,
                        "asset",
                        "success",
                        message=f"Collected {len(current_props)} properties",
                        extra={"added_properties": len(added_props), "properties": len(current_props), "url": endpoint_used.url},
                    )


def run_press(competitor_name: Optional[str] = None, is_cancelled: Optional[Callable[[], bool]] = None) -> None:
    if _is_cancelled(is_cancelled):
        return
    with get_session() as session:
        competitors = (
            session.query(Competitor)
            .options(selectinload(Competitor.source_endpoints))
            .order_by(Competitor.name.asc())
            .all()
        )
        if competitor_name:
            competitors = _filter_competitors_by_name(competitors, competitor_name)
        else:
            competitors = [c for c in competitors if getattr(c, "is_active", True)]
        if not competitors:
            log_event("competitor_not_found", competitor_filter=competitor_name)
            return
        for competitor in competitors:
            if _is_cancelled(is_cancelled):
                return
            endpoints = [
                endpoint
                for endpoint in (competitor.source_endpoints or [])
                if endpoint.channel == "press"
            ]

            # Press sources: Google News + PR Newswire only (by competitor name). User press endpoints are not used.
            print(
                f"\n[{datetime.now(timezone.utc).isoformat()}] === PRESS: {competitor.name} "
                f"(Google News + PR Newswire) ==="
            )

            # Search name and keywords from any press endpoint (UI: Edit competitor → press source → Keywords).
            press_search_name = competitor.name
            all_phrases: list[str] = []
            from_endpoint = False
            for ep in endpoints:
                opts = getattr(ep, "extra_options", None) if ep else None
                if not isinstance(opts, dict):
                    continue
                if opts.get("press_search_name"):
                    press_search_name = (opts.get("press_search_name") or "").strip() or press_search_name
                    from_endpoint = True
                raw = opts.get("google_news_search_phrases")
                if isinstance(raw, list):
                    all_phrases.extend(p for p in raw if isinstance(p, str) and (p or "").strip())
                elif isinstance(raw, str) and (raw or "").strip():
                    all_phrases.append((raw or "").strip())
            # Ensure list of strings only (DB/seed may have None or mixed types)
            _safe = [p for p in all_phrases if isinstance(p, str) and (p or "").strip()]
            google_news_search_phrases = list(dict.fromkeys(_safe)) if _safe else None
            if not from_endpoint and press_search_name:
                _press_search_fallback = {"lark": "Lark Hotels"}
                key = press_search_name.strip().lower()
                if key in _press_search_fallback:
                    press_search_name = _press_search_fallback[key]

            raw_items: list[dict] = []
            source_meta: list[dict] = []
            window_days = 90
            max_per_source = settings.press_max_items_per_source

            # 1) Google News (primary). Quoted company name + optional broad keywords (from UI: Edit → press source → Keywords).
            gn_items = []
            press_enabled = getattr(settings, "press_enable_google_news", True)
            has_phrases = bool(google_news_search_phrases)
            has_name = bool((press_search_name or "").strip())
            will_run_gn = press_enabled and (has_phrases or has_name)
            print(
                f"[press] Google News check: enabled={press_enabled}, has_phrases={has_phrases},"
                f" press_search_name={press_search_name!r}, will_run={will_run_gn}"
            )
            if will_run_gn:
                try:
                    if google_news_search_phrases:
                        print(f"[press] Keywords from source: {', '.join(google_news_search_phrases)}")
                    phrases_str = (" " + " ".join(google_news_search_phrases)) if google_news_search_phrases else ""
                    print(f"[press] Step 1 — Google News: \"{press_search_name}\"{phrases_str} (90d)")
                    gn_items = collect_google_news_items(
                        press_search_name,
                        max_items=min(50, max_per_source * 2),
                        window_days=window_days,
                        search_phrases=google_news_search_phrases,
                    )
                    print(f"[press] Step 1 — Google News returned {len(gn_items)} items (raw, before any filter)")
                    raw_items.extend(gn_items)
                    if gn_items:
                        source_meta.append({"type": "google_news"})
                    elif press_search_name:
                        print(
                            f"[press]   Google News ran but returned 0 items (RSS may have no matches for quoted phrase \"{press_search_name}\")."
                        )
                except Exception as e:
                    print(f"[press] Step 1 — Google News failed: {e}")
            else:
                print(
                    f"[press] Step 1 — Google News NOT run (enabled={press_enabled}, has_phrases={has_phrases},"
                    f" press_search_name truthy={has_name})"
                )

            # 2) PR Newswire (full press run for all competitors including AKA, Landing, Rove).
            prn_items = []
            _prnewswire_skip: set[str] = set()  # No name-based exclusions; run PR Newswire for all.
            comp_key = (competitor.name or "").strip().lower()
            if press_search_name and comp_key not in _prnewswire_skip:
                try:
                    prn_items = collect_prnewswire_items(
                        press_search_name,
                        max_items=100,
                        window_days=window_days,
                    )
                    raw_items.extend(prn_items)
                    if prn_items:
                        source_meta.append({"type": "prnewswire"})
                    print(f"[press] Step 2 — PR Newswire (90d): {len(prn_items)} items")
                except Exception as e:
                    print(f"[press] Step 2 — PR Newswire failed: {e}")
            elif press_search_name and comp_key in _prnewswire_skip:
                print(f"[press] Step 2 — PR Newswire skipped (ambiguous name). Using Google News only.")

            by_provider = {}
            for it in raw_items:
                p = (it.get("provider") or "").strip() or "unknown"
                by_provider[p] = by_provider.get(p, 0) + 1
            print(f"[press] Step 3 — Raw total: {len(raw_items)} by source: {by_provider} (unfiltered; no LLM applied yet)")

            # Use a fresh session for DB after long-running collection (avoids stale connection / SSL EOF)
            with get_session() as db_session:
                if not raw_items:
                    print(
                        f"[press] Step 3 — Raw total: 0 (sources returned no items) → skipping enrichment"
                    )
                    log_run(
                        db_session,
                        competitor.id,
                        "press",
                        "error",
                        message="no_press_items",
                        extra={"search_name": press_search_name},
                    )
                    continue

                # Apply 90-day window to non–PR Newswire items; keep all PR Newswire regardless of age (no time filter for PR).
                cutoff = datetime.now(timezone.utc) - timedelta(days=90)
                filtered_items: list[dict] = []
                for item in raw_items:
                    provider = (item.get("provider") or "").strip().lower()
                    if provider == "prnewswire":
                        filtered_items.append(item)
                        continue
                    dt = _parse_press_date(item.get("date"))
                    if dt and dt < cutoff:
                        continue
                    filtered_items.append(item)

                if not filtered_items:
                    print(f"[press] Step 4 — After 90d window: 0 items → skipping (all outside window)")
                    log_run(
                        db_session,
                        competitor.id,
                        "press",
                        "skipped",
                        message="all_items_outside_window",
                        extra={"endpoints": [ep.url for ep in endpoints]},
                    )
                    continue

                # Cap by max_raw but keep all PR Newswire (put PR first so they are never cut).
                max_raw = settings.press_max_raw_items_per_competitor
                pr_items = [it for it in filtered_items if (it.get("provider") or "").strip().lower() == "prnewswire"]
                non_pr_items = [it for it in filtered_items if (it.get("provider") or "").strip().lower() != "prnewswire"]
                combined = pr_items + non_pr_items
                if len(combined) > max_raw:
                    combined = combined[:max_raw]
                filtered_items = combined
                print(f"[press] Step 4 — After 90d window + cap (PR first, all PR kept) (max_raw={max_raw}): {len(filtered_items)} items")

                # Load previous snapshot for diff/events and for fallback when enrichment returns no groups.
                latest = load_latest_snapshot(db_session, competitor.id, "press")
                previous_structured = (latest.structured_json or {}) if latest else {}
                previous_items = previous_structured.get("items") or []
                previous_canonical = previous_structured.get("canonical_items") or []
                existing_groups = previous_structured.get("press_groups") or []

            # Always run enrichment on the full pull: classify + group all qualifying articles. No skip by "no new items."
            print(f"[press] Step 6 — Enriching full pull ({len(filtered_items)} items): classify + group (late articles join groups; new topics become new groups).")

            def _normalize_domain(host: str) -> str:
                if not host:
                    return ""
                h = (host or "").lower().strip()
                return h[4:] if h.startswith("www.") else h

            company_domains: list[str] = []
            if getattr(competitor, "primary_domain", None):
                company_domains.append(competitor.primary_domain)
            for ep in endpoints:
                try:
                    netloc = urlparse(ep.url).netloc
                    if netloc:
                        company_domains.append(netloc)
                except Exception:
                    pass
            company_domains = list({_normalize_domain(d) for d in company_domains if d})

            snapshot_like = {
                "source_url": endpoints[0].url if endpoints else None,
                "items": filtered_items,
            }
            structured = build_press_structured(snapshot_like)
            structured["sources"] = source_meta
            items_for_enrich = structured.get("items") or []
            press_groups = enrich_press_items_with_llm(
                competitor.name,
                items_for_enrich,
                max_articles_to_summarize=settings.press_max_articles_to_summarize,
                company_domains=company_domains,
                previous_items=previous_items if previous_items else None,
                previous_canonical=previous_canonical if previous_canonical else None,
            )
            # If enrichment returned no groups but we had items (e.g. all dropped by company-domain filter),
            # preserve previous groupings so the dossier still displays grouped press for this competitor.
            if not press_groups and items_for_enrich and existing_groups:
                has_any_articles = any(
                    isinstance(g, dict) and (g.get("articles") or [])
                    for g in existing_groups
                )
                if has_any_articles:
                    press_groups = existing_groups
                    print(f"[press] {competitor.name}: enrichment returned 0 groups; keeping previous {len(press_groups)} groups for dossier display.")
            structured["press_groups"] = press_groups
            # Flatten groups to canonical_items for Top news, diff, and backward compatibility.
            canonical = []
            for g in press_groups:
                for art in g.get("articles") or []:
                    canonical.append({
                        "title": art.get("title") or "",
                        "url": art.get("url") or art.get("link"),
                        "date": art.get("date"),
                        "outlet": art.get("outlet"),
                        "group_title": g.get("group_title"),
                        "summary": g.get("one_line_summary") or "",
                    })
            structured["canonical_items"] = canonical

            display_count = len(canonical)
            print(f"[press] Step 7 — Final: {len(press_groups)} groups, {display_count} articles")
            for i, item in enumerate(canonical[:10], 1):
                date_str = (item.get("date") or "no date")[:10] if item.get("date") else "no date"
                title = (item.get("title") or "—")
                if len(title) > 55:
                    title = title[:55] + "…"
                url = (item.get("url") or item.get("link") or "")
                if len(url) > 70:
                    url = url[:70] + "…"
                print(f"[press]   {i}. [{date_str}] {title}")
                print(f"[press]      {url}")
            if display_count > 10:
                print(f"[press]   ... and {display_count - 10} more")

            # Build top_news (14-day bullets) for dossier and list view; store in snapshot.
            groups_with_dates = []
            for g in press_groups:
                group_latest_dt = None
                for a in g.get("articles") or []:
                    raw = (a.get("date") or "").strip()
                    if raw and len(raw) >= 10:
                        try:
                            dt = datetime.strptime(raw[:10], "%Y-%m-%d")
                            if group_latest_dt is None or dt > group_latest_dt:
                                group_latest_dt = dt
                        except ValueError:
                            pass
                group_latest_date = group_latest_dt.strftime("%Y-%m-%d") if group_latest_dt else None
                groups_with_dates.append({
                    "group_title": g.get("group_title"),
                    "one_line_summary": g.get("one_line_summary"),
                    "articles": g.get("articles") or [],
                    "group_latest_date": group_latest_date,
                })
            top_news = summarize_top_news_llm(competitor.name, groups_with_dates, days=14)
            if top_news:
                structured["top_news"] = top_news

            print(f"[press] Step 8 — Persisting snapshot for {competitor.name}")

            raw_hash = _build_press_raw_hash(filtered_items)
            current_items = structured.get("items", [])

            # Use a fresh session for DB after long-running enrichment (avoids stale connection / SSL EOF)
            with get_session() as db_session:
                persist_snapshot(
                    db_session,
                    competitor.id,
                    "press",
                    "",  # raw HTML is not retained for aggregated press; items + canonical_items are sufficient.
                    raw_hash,
                    structured,
                )
                # If seed_mode is enabled and this is the first snapshot for this competitor/channel,
                # persist a baseline but skip diff + events so future runs compare against this state.
                seed_mode = getattr(settings, "seed_mode", False)
                is_first_snapshot = latest is None
                if seed_mode and is_first_snapshot:
                    log_run(
                        db_session,
                        competitor.id,
                        "press",
                        "success",
                        message="press_seed_baseline",
                        extra={"raw_items": len(raw_items), "filtered_items": len(filtered_items)},
                    )
                    continue

                # Diff for new items and create press events (same rules as before).
                diff = diff_items(previous_items, current_items)
                added_items = diff["added"]

                for item in added_items:
                    category = classify_press(item)
                    if not category:
                        continue
                    if not is_executive_relevant(item):
                        continue
                    event = build_press_event(category, item)
                    if not settings.seed_mode and not event_recently_created(
                        db_session,
                        competitor.id,
                        event["type"],
                        event["title"],
                        window_days=dedupe_window_for(event["type"]),
                    ):
                        create_event(db_session, competitor.id, event)

                print(f"[press] Step 9 — Done. Added events: {len(added_items)}")
                log_run(
                    db_session,
                    competitor.id,
                    "press",
                    "success",
                    message=f"Collected {len(filtered_items)} articles",
                    extra={"added_items": len(added_items), "raw_items": len(raw_items), "filtered_items": len(filtered_items)},
                )


# Fallback when DB is unavailable (e.g. --local with no DB). Also used by inspect scripts.
LOCAL_PRESS_COMPETITORS = [
    ("AKA", "AKA"),
    ("Lark", "Lark Hotels"),
    ("AvantStay", "AvantStay"),
    ("Placemakr", "Placemakr"),
    ("Blueground", "Blueground"),
    ("Landing", "Landing"),
    ("Vacasa", "Vacasa"),
    ("Rove", "Rove"),
]


def get_press_competitors_list(competitor_name: Optional[str] = None) -> Optional[list[tuple[str, str, Optional[list[str]]]]]:
    """
    Load competitors for press (display_name, press_search_name, google_news_search_phrases) from the database.
    google_news_search_phrases is optional list for targeted Google News query (e.g. ["landing furnished rentals"]).
    Returns None if DB is unavailable or fails.
    """
    try:
        with get_session() as session:
            competitors = (
                session.query(Competitor)
                .options(selectinload(Competitor.source_endpoints))
                .order_by(Competitor.name.asc())
                .all()
            )
            competitors = [c for c in competitors if getattr(c, "is_active", True)]
            if competitor_name:
                competitors = _filter_competitors_by_name(competitors, competitor_name)
            out: list[tuple[str, str, Optional[list[str]]]] = []
            for c in competitors:
                press_search_name = c.name
                all_phrases: list[str] = []
                from_endpoint = False
                for ep in (c.source_endpoints or []):
                    if (getattr(ep, "channel", None) or "").strip().lower() == "press":
                        opts = getattr(ep, "extra_options", None) if ep else None
                        if isinstance(opts, dict):
                            if opts.get("press_search_name"):
                                press_search_name = (opts.get("press_search_name") or "").strip() or press_search_name
                                from_endpoint = True
                            raw = opts.get("google_news_search_phrases")
                            if isinstance(raw, list):
                                all_phrases.extend(p for p in raw if isinstance(p, str) and (p or "").strip())
                            elif isinstance(raw, str) and (raw or "").strip():
                                all_phrases.append((raw or "").strip())
                _safe = [p for p in all_phrases if isinstance(p, str) and (p or "").strip()]
                google_news_search_phrases = list(dict.fromkeys(_safe)) if _safe else None
                if not from_endpoint and press_search_name:
                    _press_search_fallback = {"lark": "Lark Hotels"}
                    key = press_search_name.strip().lower()
                    if key in _press_search_fallback:
                        press_search_name = _press_search_fallback[key]
                out.append((c.name, press_search_name, google_news_search_phrases))
            return out if out else None
    except Exception:
        return None


def run_press_local(competitor_name: Optional[str] = None) -> None:
    """
    Run press pipeline (Google News + PR Newswire, enrich, dedupe).
    Prefers competitors from DB (including UI-added); falls back to LOCAL_PRESS_COMPETITORS if DB unavailable.
    Use: python -m app.cli --local --channel press [--competitor NAME]
    """
    competitors = get_press_competitors_list(competitor_name)
    if competitors is None:
        # LOCAL_PRESS_COMPETITORS is (disp, search) only; pad with None for phrases
        competitors = [(disp, search, None) for disp, search in LOCAL_PRESS_COMPETITORS]
        if competitor_name:
            name_lower = (competitor_name or "").strip().lower()
            competitors = [
                (disp, search, phrases) for disp, search, phrases in competitors
                if name_lower in disp.lower() or name_lower in search.lower()
            ]
        if not competitors:
            print(f"[press] No competitor matching {competitor_name!r}. Options: Lark, AvantStay, Placemakr, Blueground, Landing, Vacasa, Rove.")
            return
    elif competitor_name and not competitors:
        print(f"[press] No competitor matching {competitor_name!r} in database.")
        return

    window_days = 90
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    max_per_source = getattr(settings, "press_max_items_per_source", 30)
    max_raw = getattr(settings, "press_max_raw_items_per_competitor", 120)

    for display_name, press_search_name, google_news_search_phrases in competitors:
        print(f"\n[{datetime.now(timezone.utc).isoformat()}] === PRESS (local): {display_name} ({press_search_name!r}) ===")
        raw_items: list[dict] = []

        if getattr(settings, "press_enable_google_news", True) and (google_news_search_phrases or press_search_name):
            try:
                if google_news_search_phrases:
                    print(f"[press] Keywords from source: {', '.join(google_news_search_phrases)}")
                phrases_str = (" " + " ".join(google_news_search_phrases)) if google_news_search_phrases else ""
                print(f"[press] Google News query: \"{press_search_name}\"{phrases_str} (90d)")
                gn_items = collect_google_news_items(
                    press_search_name,
                    max_items=min(50, max_per_source * 2),
                    window_days=window_days,
                    search_phrases=google_news_search_phrases,
                )
                raw_items.extend(gn_items)
                print(f"[press] Google News (90d): {len(gn_items)} items")
            except Exception as e:
                print(f"[press] Google News failed: {e}")

        # PR Newswire runs for all competitors (no name-based skip).
        _prnewswire_skip: set[str] = set()
        if (display_name or "").strip().lower() not in _prnewswire_skip:
            try:
                prn_items = collect_prnewswire_items(
                    press_search_name,
                    max_items=100,
                    window_days=window_days,
                )
                raw_items.extend(prn_items)
                print(f"[press] PR Newswire (90d): {len(prn_items)} items")
            except Exception as e:
                print(f"[press] PR Newswire failed: {e}")
        else:
            print(f"[press] PR Newswire skipped (ambiguous name: {display_name!r})")

        filtered_items = []
        for item in raw_items:
            if (item.get("provider") or "").strip().lower() == "prnewswire":
                filtered_items.append(item)
                continue
            dt = _parse_press_date(item.get("date"))
            if dt and dt < cutoff:
                continue
            filtered_items.append(item)
        # Cap but keep all PR Newswire (put PR first so they are never cut).
        pr_items = [it for it in filtered_items if (it.get("provider") or "").strip().lower() == "prnewswire"]
        non_pr_items = [it for it in filtered_items if (it.get("provider") or "").strip().lower() != "prnewswire"]
        combined = pr_items + non_pr_items
        if len(combined) > max_raw:
            combined = combined[:max_raw]
        filtered_items = combined
        print(f"[press] After 90d + cap (PR first, all PR kept): {len(filtered_items)} items (unfiltered; LLM filter applied in enrichment)")

        if not filtered_items:
            print(f"[press] No items from sources → skipping enrichment.")
            continue

        press_groups = enrich_press_items_with_llm(
            display_name,
            filtered_items,
            max_articles_to_summarize=getattr(settings, "press_max_articles_to_summarize", 40),
            company_domains=[],
            previous_items=None,
            previous_canonical=None,
        )
        canonical = [art for g in press_groups for art in (g.get("articles") or [])]
        print(f"[press] Final: {len(press_groups)} groups, {len(canonical)} articles\n")
        for i, item in enumerate(canonical[:15], 1):
            date_str = (item.get("date") or "no date")[:10] if item.get("date") else "no date"
            title = (item.get("title") or "—")[:65]
            url = (item.get("url") or item.get("link") or "")
            print(f"  {i}. [{date_str}] {title}")
            print(f"      {url}")
        if len(canonical) > 15:
            print(f"  ... and {len(canonical) - 15} more")
        print()


def _homepage_base_from_primary_domain(primary_domain: Optional[str]) -> Optional[str]:
    """Return https base URL for a competitor with no explicit homepage endpoint."""
    if not (primary_domain or "").strip():
        return None
    domain = (primary_domain or "").strip().lower()
    if not domain:
        return None
    if "://" in domain:
        return domain.rstrip("/")
    return "https://www." + domain if not domain.startswith("www.") else "https://" + domain


def _homepage_runs_for_competitor(competitor: Competitor) -> list[tuple[str, bool, list[str]]]:
    """Return list of (base_url, js_required, paths) for this competitor's homepage channel."""
    runs: list[tuple[str, bool, list[str]]] = []
    endpoints = [ep for ep in competitor.source_endpoints if ep.channel == "homepage"]
    if endpoints:
        for ep in endpoints:
            opts = getattr(ep, "extra_options", None) or {}
            paths = opts.get("product_paths") or COMMON_HOMEPAGE_PATHS
            runs.append((ep.url, getattr(ep, "js_required", False), paths))
        return runs
    base = _homepage_base_from_primary_domain(getattr(competitor, "primary_domain", None))
    if base:
        runs.append((base, False, COMMON_HOMEPAGE_PATHS))
    return runs


def run_homepage(competitor_name: Optional[str] = None, is_cancelled: Optional[Callable[[], bool]] = None) -> None:
    if _is_cancelled(is_cancelled):
        return
    with get_session() as session:
        competitors = (
            session.query(Competitor)
            .options(selectinload(Competitor.source_endpoints))
            .order_by(Competitor.name.asc())
            .all()
        )
        if competitor_name:
            competitors = _filter_competitors_by_name(competitors, competitor_name)
        else:
            competitors = [c for c in competitors if getattr(c, "is_active", True)]
        if not competitors:
            log_event("competitor_not_found", competitor_filter=competitor_name)
            return
        for competitor in competitors:
            if _is_cancelled(is_cancelled):
                return
            runs = _homepage_runs_for_competitor(competitor)
            if not runs:
                log_run(
                    session,
                    competitor.id,
                    "homepage",
                    "skipped",
                    message="no_homepage_url",
                    extra={"reason": "no primary_domain and no homepage source_endpoints"},
                )
                continue
            for base_url, js_required, paths in runs:
                if _is_cancelled(is_cancelled):
                    return
                urls = [base_url] + [
                    urljoin(base_url.rstrip("/") + "/", p.lstrip("/"))
                    for p in paths
                ]
                print(
                    f"[{datetime.now(timezone.utc).isoformat()}] homepage run: "
                    f"{competitor.name} {base_url}"
                    + (f" (+{len(paths)} paths)" if paths else "")
                )
                pages: list[dict[str, Any]] = []
                for url in urls:
                    try:
                        p = collect_homepage_snapshot(url, js_required=js_required)
                        pages.append(p)
                    except Exception as exc:
                        log_run(
                            session,
                            competitor.id,
                            "homepage",
                            "error",
                            message=str(exc),
                            extra={"url": url},
                        )
                if not pages:
                    continue
                composite_hash = build_composite_hash(pages)
                if should_skip_due_to_hash(session, competitor.id, "homepage", composite_hash):
                    log_run(
                        session,
                        competitor.id,
                        "homepage",
                        "skipped",
                        message="snapshot_unchanged",
                        extra={"url": base_url},
                    )
                    continue
                snapshot_for_structured: dict[str, Any] = {"pages": pages}
                structured = build_homepage_structured(
                    snapshot_for_structured,
                    detect_coming_soon=detect_coming_soon_phrases,
                )
                latest = load_latest_snapshot(session, competitor.id, "homepage")
                is_first_snapshot = latest is None
                first_raw = pages[0].get("raw_content") or ""
                persist_snapshot(
                    session,
                    competitor.id,
                    "homepage",
                    first_raw,
                    composite_hash,
                    structured,
                )
                seed_mode = getattr(settings, "seed_mode", False)
                if seed_mode and is_first_snapshot:
                    log_run(
                        session,
                        competitor.id,
                        "homepage",
                        "success",
                        message="homepage_seed_baseline",
                        extra={"source_url": pages[0].get("source_url")},
                    )
                    continue
                prev_pages: list[dict[str, Any]] = []
                if latest and latest.structured_json and isinstance(latest.structured_json.get("pages"), list):
                    prev_pages = latest.structured_json["pages"]
                else:
                    if latest:
                        prev_pages = [{
                            "url": base_url,
                            "raw_hash": latest.raw_hash or "",
                            "content_hash": (latest.structured_json or {}).get("content_hash"),
                        }]
                prev_by_url = {p.get("url"): p for p in prev_pages if p.get("url")}
                for page in structured.get("pages") or []:
                    url = page.get("url") or ""
                    prev = prev_by_url.get(url)
                    content_changed = prev is None or (page.get("content_hash") != prev.get("content_hash"))
                    if not content_changed:
                        continue
                    # Optional LLM interpretation: what changed, subdomain/path context, and importance.
                    url_ctx = parse_url_context(url)
                    old_snippet = (prev.get("visible_text_snippet") or "") if prev else None
                    new_snippet = page.get("visible_text_snippet") or ""
                    interpretation = interpret_website_change(
                        competitor.name,
                        url,
                        url_ctx,
                        old_snippet or None,
                        new_snippet or None,
                        page.get("coming_soon_phrases"),
                    )
                    if interpretation and not interpretation.get("is_important", True):
                        continue  # Skip creating event for trivial changes
                    ev_updated = build_homepage_updated_event(url)
                    evidence = ev_updated.get("evidence") or {}
                    evidence["subdomain"] = url_ctx.get("subdomain") or ""
                    evidence["path"] = url_ctx.get("path") or ""
                    ev_updated["evidence"] = evidence
                    if interpretation:
                        ev_updated["title"] = (interpretation.get("suggested_title") or ev_updated["title"])[:255]
                        ev_updated["summary"] = (interpretation.get("summary") or ev_updated["summary"])[:2000]
                        if interpretation.get("reason"):
                            ev_updated["why_it_matters"] = (interpretation.get("reason") or ev_updated.get("why_it_matters"))[:1000]
                    title_updated = ev_updated["title"] + " " + (url[:80] if url else "")
                    if not event_already_created_since_baseline(
                        session,
                        competitor,
                        "narrative.homepage_updated",
                        title_updated,
                        fallback_window_days=dedupe_window_for("narrative.homepage_updated"),
                    ):
                        create_event(session, competitor.id, ev_updated)
                    phrases = page.get("coming_soon_phrases") or []
                    if phrases:
                        phrase = phrases[0]
                        ev_soon = build_coming_soon_event(url, phrase)
                        soon_evidence = ev_soon.get("evidence") or {}
                        soon_evidence["subdomain"] = url_ctx.get("subdomain") or ""
                        soon_evidence["path"] = url_ctx.get("path") or ""
                        ev_soon["evidence"] = soon_evidence
                        title_soon = ev_soon["title"] + " " + (url[:80] if url else "")
                        if not event_already_created_since_baseline(
                            session,
                            competitor,
                            "narrative.coming_soon",
                            title_soon,
                            fallback_window_days=dedupe_window_for("narrative.coming_soon"),
                        ):
                            create_event(session, competitor.id, ev_soon)
                log_run(
                    session,
                    competitor.id,
                    "homepage",
                    "success",
                    message="Homepage and product paths checked",
                    extra={"source_url": pages[0].get("source_url")},
                )


def _build_social_raw_hash(posts: list[dict]) -> str:
    """Stable hash for social posts so we can skip unchanged snapshots."""
    if not posts:
        return ""
    keys = []
    for p in posts:
        pid = (p.get("id") or "").strip()
        url = (p.get("url") or "").strip()
        text = (p.get("text") or p.get("title") or "")[:200]
        date = (p.get("published_at") or "").strip()
        keys.append(f"{pid}|{url}|{text}|{date}")
    keys.sort()
    return hashlib.sha256("\n".join(keys).encode("utf-8")).hexdigest()


def run_social(competitor_name: Optional[str] = None, is_cancelled: Optional[Callable[[], bool]] = None) -> None:
    """Collect Twitter/LinkedIn posts via RSS; classify with LLM; create narrative.social_signal for executive-relevant new posts."""
    if _is_cancelled(is_cancelled):
        return
    with get_session() as session:
        competitors = session.query(Competitor).order_by(Competitor.name.asc()).all()
        if competitor_name:
            competitors = _filter_competitors_by_name(competitors, competitor_name)
        else:
            competitors = [c for c in competitors if getattr(c, "is_active", True)]
        if not competitors:
            log_event("competitor_not_found", competitor_filter=competitor_name)
            return
        bridge = getattr(settings, "twitter_rss_bridge_base", None) or None
        linkedin_state = getattr(settings, "linkedin_storage_state_path", None) or None
        for competitor in competitors:
            if _is_cancelled(is_cancelled):
                return
            endpoints = [ep for ep in competitor.source_endpoints if ep.channel == "social"]
            if not endpoints:
                continue
            print(
                f"\n[{datetime.now(timezone.utc).isoformat()}] === SOCIAL: {competitor.name} "
                f"({len(endpoints)} endpoint(s)) ==="
            )
            all_posts: list[dict] = []
            raw_content_parts: list[str] = []
            for ep in endpoints:
                if _is_cancelled(is_cancelled):
                    return
                platform = (getattr(ep, "extra_options") or {}).get("platform") if isinstance(getattr(ep, "extra_options"), dict) else None
                if not platform or platform not in ("twitter", "linkedin"):
                    platform = "linkedin" if "linkedin" in (ep.url or "").lower() else "twitter"
                try:
                    feed = collect_social_feed(
                        ep.url,
                        platform,
                        twitter_rss_bridge_base=bridge,
                        linkedin_storage_state_path=linkedin_state,
                    )
                except Exception as exc:
                    log_run(session, competitor.id, "social", "error", message=str(exc), extra={"url": ep.url})
                    continue
                items = feed.get("items") or []
                all_posts.extend(items)
                if feed.get("raw_content"):
                    raw_content_parts.append(feed["raw_content"])
            if not all_posts:
                print(f"[social] {competitor.name}: 0 posts (no RSS or bridge)")
                log_run(
                    session,
                    competitor.id,
                    "social",
                    "skipped",
                    message="no_posts",
                    extra={"endpoints": [ep.url for ep in endpoints]},
                )
                continue
            raw_hash = _build_social_raw_hash(all_posts)
            if should_skip_due_to_hash(session, competitor.id, "social", raw_hash):
                log_run(session, competitor.id, "social", "skipped", message="snapshot_unchanged")
                continue
            print(f"[social] Step 1 — Collected {len(all_posts)} posts")
            enriched = enrich_social_posts_with_llm(all_posts)
            structured = build_social_structured({"posts": enriched})
            current_posts = structured.get("posts") or []
            raw_content = "\n---\n".join(raw_content_parts) if raw_content_parts else ""
            latest = load_latest_snapshot(session, competitor.id, "social")
            seed_mode = getattr(settings, "seed_mode", False)
            is_first = latest is None
            persist_snapshot(
                session,
                competitor.id,
                "social",
                raw_content,
                raw_hash,
                structured,
            )
            if seed_mode and is_first:
                print(f"[social] Step 2 — Seed baseline. Total posts: {len(current_posts)}")
                log_run(session, competitor.id, "social", "success", message="social_seed_baseline", extra={"posts": len(current_posts)})
                continue
            previous_posts = (latest.structured_json or {}).get("posts", []) if latest else []
            diff = diff_social_posts(previous_posts, current_posts)
            added = diff["added"]
            events_created = 0
            for post in added:
                if (post.get("relevance") or "").lower() != "executive":
                    continue
                event = build_social_signal_event(post)
                if not event_recently_created(
                    session,
                    competitor.id,
                    event["type"],
                    event["title"],
                    window_days=dedupe_window_for(event["type"]),
                ):
                    create_event(session, competitor.id, event)
                    events_created += 1
            print(f"[social] Step 2 — Done. Added {len(added)} new posts; {events_created} executive-relevant events")
            log_run(
                session,
                competitor.id,
                "social",
                "success",
                message=f"Collected {len(current_posts)} posts",
                extra={"posts": len(current_posts), "added": len(added), "events": events_created},
            )


def run_public_records(competitor_name: Optional[str] = None, is_cancelled: Optional[Callable[[], bool]] = None) -> None:
    if _is_cancelled(is_cancelled):
        return
    with get_session() as session:
        competitors = session.query(Competitor).order_by(Competitor.name.asc()).all()
        if competitor_name:
            competitors = _filter_competitors_by_name(competitors, competitor_name)
        else:
            competitors = [c for c in competitors if getattr(c, "is_active", True)]
            if not competitors:
                log_event("competitor_not_found", competitor_filter=competitor_name)
                return
        for competitor in competitors:
            if _is_cancelled(is_cancelled):
                return
            endpoints = [
                ep
                for ep in competitor.source_endpoints
                if ep.channel == "public_records"
            ]
            for endpoint in endpoints:
                if _is_cancelled(is_cancelled):
                    return
                print(
                    f"[{datetime.now(timezone.utc).isoformat()}] public_records run: "
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
                    message=f"Collected {len(current_items)} filings",
                    extra={"added_items": len(added_items), "items": len(current_items)},
                )


def run_reviews(competitor_name: Optional[str] = None, is_cancelled: Optional[Callable[[], bool]] = None) -> None:
    """Fetch Google Reviews for each competitor's tracked properties; persist snapshot with trend."""
    if _is_cancelled(is_cancelled):
        return
    with get_session() as session:
        competitors = session.query(Competitor).order_by(Competitor.name.asc()).all()
        if competitor_name:
            competitors = _filter_competitors_by_name(competitors, competitor_name)
        else:
            competitors = [c for c in competitors if getattr(c, "is_active", True)]
        if not competitors:
            log_event("competitor_not_found", competitor_filter=competitor_name)
            return
        api_key = getattr(settings, "google_places_api_key", None) or ""
        if not api_key.strip():
            print("[reviews] GOOGLE_PLACES_API_KEY not set; skipping reviews channel.")
            return
        for competitor in competitors:
            if _is_cancelled(is_cancelled):
                return
            props = list(competitor.review_properties) if hasattr(competitor, "review_properties") else []
            if not props:
                continue
            print(
                f"\n[{datetime.now(timezone.utc).isoformat()}] === REVIEWS: {competitor.name} "
                f"({len(props)} properties) ==="
            )
            properties_data = []
            for rp in props:
                if _is_cancelled(is_cancelled):
                    return
                data = collect_property_review(
                    rp.place_id,
                    display_name=rp.display_name,
                    api_key=api_key,
                )
                properties_data.append(data)
                if data.get("error"):
                    print(f"[reviews] {rp.display_name or rp.place_id}: {data.get('error')}")
            latest = load_latest_snapshot(session, competitor.id, "reviews")
            previous_json = (latest.structured_json if latest else None) or None
            structured = build_reviews_structured(properties_data, previous_json)
            raw_content = json.dumps({"properties": len(structured.get("properties", []))})
            raw_hash = hashlib.sha256(raw_content.encode("utf-8")).hexdigest()
            persist_snapshot(
                session,
                competitor.id,
                "reviews",
                raw_content,
                raw_hash,
                structured,
            )
            ok_count = len([p for p in properties_data if not p.get("error")])
            log_run(
                session,
                competitor.id,
                "reviews",
                "success",
                message=f"{ok_count}/{len(props)} properties fetched",
                extra={"properties": ok_count, "total": len(props)},
            )
            print(f"[reviews] Done. {ok_count}/{len(props)} properties.")


# Website/digital-footprint event types; we only keep these since last baseline (no running log).
DIGITAL_FOOTPRINT_EVENT_TYPES = ("narrative.homepage_updated", "narrative.coming_soon")


def clear_baseline_before_force_refresh() -> None:
    """
    Set every competitor's reporting_baseline_at to None.
    Call at the start of a force refresh so the baseline_set column resets (shows "—")
    until the run completes and advance_baseline_after_full_refresh() sets the new baseline.
    """
    with get_session() as session:
        for c in session.query(Competitor).all():
            c.reporting_baseline_at = None


def advance_baseline_after_full_refresh() -> datetime:
    """
    Set every competitor's reporting_baseline_at to now.
    Call this after a full refresh (all channels) so the executive summary and dossier
    compare against the last refresh, not the original baseline—surfacing only what
    changed since the last run (e.g. last 7 days) instead of the full period since first reset.
    Also deletes old website/digital-footprint events (homepage_updated, coming_soon) so we
    only retain changes since this baseline—no heavy running log.
    Commits explicitly so the "Baseline set" column on /competitors shows the date immediately.
    """
    with get_session() as session:
        now = datetime.now(timezone.utc)
        for c in session.query(Competitor).all():
            c.reporting_baseline_at = now
            # Keep only website-change events since this baseline; drop older ones.
            session.query(Event).filter(
                Event.competitor_id == c.id,
                Event.type.in_(DIGITAL_FOOTPRINT_EVENT_TYPES),
                Event.detected_at < now,
            ).delete(synchronize_session=False)
        session.commit()
    return now


def run(
    channel: Optional[str] = None,
    competitor_name: Optional[str] = None,
    local: bool = False,
    is_cancelled: Optional[Callable[[], bool]] = None,
) -> None:
    if _is_cancelled(is_cancelled):
        return
    if local:
        if channel not in (None, "press"):
            print("[local] Only --channel press is supported without a database. Use --channel press.")
            return
        run_press_local(competitor_name=competitor_name)
        return
    if channel in (None, *RUNNER_CHANNELS):
        if channel is None and competitor_name is None:
            # Competitor-first: run all channels per competitor so each competitor completes
            # before moving to the next. Makes progress visible during long force refreshes.
            # Extract names inside session to avoid DetachedInstanceError when iterating outside.
            with get_session() as session:
                competitors = session.query(Competitor).order_by(Competitor.name.asc()).all()
                competitor_names = [
                    c.name for c in competitors
                    if getattr(c, "is_active", True)
                ]
            for name in competitor_names:
                if _is_cancelled(is_cancelled):
                    return
                print(
                    f"\n[{datetime.now(timezone.utc).isoformat()}] === {name} (all channels) ===",
                    flush=True,
                )
                run_talent(competitor_name=name, is_cancelled=is_cancelled)
                run_asset(competitor_name=name, is_cancelled=is_cancelled)
                run_press(competitor_name=name, is_cancelled=is_cancelled)
                run_homepage(competitor_name=name, is_cancelled=is_cancelled)
                run_public_records(competitor_name=name, is_cancelled=is_cancelled)
                run_social(competitor_name=name, is_cancelled=is_cancelled)
                run_reviews(competitor_name=name, is_cancelled=is_cancelled)
        else:
            # Channel-first: single channel or single competitor (unchanged)
            if channel in (None, "talent"):
                run_talent(competitor_name=competitor_name, is_cancelled=is_cancelled)
            if _is_cancelled(is_cancelled):
                return
            if channel in (None, "asset"):
                run_asset(competitor_name=competitor_name, is_cancelled=is_cancelled)
            if _is_cancelled(is_cancelled):
                return
            if channel in (None, "press"):
                run_press(competitor_name=competitor_name, is_cancelled=is_cancelled)
            if _is_cancelled(is_cancelled):
                return
            if channel in (None, "homepage"):
                run_homepage(competitor_name=competitor_name, is_cancelled=is_cancelled)
            if _is_cancelled(is_cancelled):
                return
            if channel in (None, "public_records"):
                run_public_records(competitor_name=competitor_name, is_cancelled=is_cancelled)
            if _is_cancelled(is_cancelled):
                return
            if channel in (None, "social"):
                run_social(competitor_name=competitor_name, is_cancelled=is_cancelled)
            if _is_cancelled(is_cancelled):
                return
            if channel in (None, "reviews"):
                run_reviews(competitor_name=competitor_name, is_cancelled=is_cancelled)
    if channel is not None and channel not in RUNNER_CHANNELS:
        print(f"[{datetime.now(timezone.utc).isoformat()}] unknown channel: {channel}")


if __name__ == "__main__":
    run()
