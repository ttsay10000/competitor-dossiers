from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import Any, Optional
from urllib.parse import urlparse

from .collectors.talent import collect_talent_snapshot, build_structured_json as build_talent_structured
from .collectors.asset import collect_asset_snapshot, build_structured_json as build_asset_structured
from .collectors.press import collect_press_snapshot, build_structured_json as build_press_structured
from .collectors.global_press import collect_google_news_items, collect_prnewswire_items
from .collectors.homepage import collect_homepage_snapshot, build_structured_json as build_homepage_structured
from .collectors.public_records import collect_public_records_snapshot, build_structured_json as build_public_records_structured
from .config import settings
from .db import get_session
from .diff.talent_diff import diff_jobs, count_recent_by_capability
from .diff.asset_diff import diff_properties, extract_markets
from .llm_structured import enrich_properties_with_llm, enrich_jobs_with_llm, enrich_press_items_with_llm
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

# All channels that support per-competitor seed baseline (SEED_MODE): first snapshot
# per competitor/channel persists as baseline; no diff or events until the next run.
RUNNER_CHANNELS = ("talent", "asset", "press", "homepage", "public_records")


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


def run_talent(competitor_name: Optional[str] = None) -> None:
    with get_session() as session:
        competitors = session.query(Competitor).order_by(Competitor.name.asc()).all()
        if competitor_name:
            competitors = _filter_competitors_by_name(competitors, competitor_name)
            if not competitors:
                log_event("competitor_not_found", competitor_filter=competitor_name)
                return
        for competitor in competitors:
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
                for s in (
                    session.query(Snapshot)
                    .filter(Snapshot.competitor_id == competitor.id, Snapshot.channel == "talent")
                    .order_by(Snapshot.captured_at.desc())
                    .limit(20)
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
            log_run(
                session,
                competitor.id,
                "talent",
                "success",
                extra={"added_jobs": len(added_jobs), "url": endpoint_used.url},
            )


def run_asset(competitor_name: Optional[str] = None) -> None:
    with get_session() as session:
        competitors = session.query(Competitor).order_by(Competitor.name.asc()).all()
        if competitor_name:
            competitors = _filter_competitors_by_name(competitors, competitor_name)
            if not competitors:
                log_event("competitor_not_found", competitor_filter=competitor_name)
                return
        for competitor in competitors:
            endpoints_ordered = _endpoints_ordered([
                e for e in competitor.source_endpoints if e.channel == "asset"
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
                raw_count = len(snapshot.get("properties") or [])
                note = snapshot.get("note") or "unknown"
                print(f"[asset] Step 1 — Collect: {raw_count} properties (strategy: {note})")
                structured = build_asset_structured(snapshot)
                before_enrich = len(structured.get("properties") or [])
                structured["properties"] = enrich_properties_with_llm(
                    structured.get("properties") or [],
                    raw_content=snapshot.get("raw_content"),
                )
                current_props = structured.get("properties") or []
                after_enrich = len(current_props)
                print(f"[asset] Step 2 — build_structured: {before_enrich} → enrich_properties_with_llm: {after_enrich} properties")
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

            latest = load_latest_snapshot(session, competitor.id, "asset")
            previous_props = (latest.structured_json or {}).get("properties", []) if latest else []
            current_props = structured.get("properties") or []

            persist_snapshot(
                session,
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
                log_run(
                    session,
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
                        session,
                        competitor.id,
                        event["type"],
                        event["title"],
                        window_days=dedupe_window_for(event["type"]),
                    ):
                        create_event(session, competitor.id, event)
            for prop in added_props:
                status = (prop.get("status") or "").lower()
                name = (prop.get("name") or "").lower()
                if "coming soon" in status or "coming soon" in name:
                    event = build_pipeline_event(prop)
                    if not settings.seed_mode and not event_recently_created(
                        session,
                        competitor.id,
                        event["type"],
                        event["title"],
                        window_days=dedupe_window_for(event["type"]),
                    ):
                        create_event(session, competitor.id, event)
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
                            if not settings.seed_mode and not event_recently_created(
                                session,
                                competitor.id,
                                event["type"],
                                event["title"],
                                window_days=dedupe_window_for(event["type"]),
                            ):
                                create_event(session, competitor.id, event)

            print(f"[asset] Step 4 — Done. Total properties: {len(current_props)}")
            log_run(
                session,
                competitor.id,
                "asset",
                "success",
                extra={"added_properties": len(added_props), "url": endpoint_used.url},
            )


def run_press(competitor_name: Optional[str] = None) -> None:
    with get_session() as session:
        competitors = session.query(Competitor).order_by(Competitor.name.asc()).all()
        if competitor_name:
            competitors = _filter_competitors_by_name(competitors, competitor_name)
            if not competitors:
                log_event("competitor_not_found", competitor_filter=competitor_name)
                return
        for competitor in competitors:
            endpoints = [
                endpoint
                for endpoint in competitor.source_endpoints
                if endpoint.channel == "press"
            ]

            print(
                f"\n[{datetime.now(timezone.utc).isoformat()}] === PRESS: {competitor.name} "
                f"({len(endpoints)} user endpoint(s)) ==="
            )

            # Optional press search name for external sources (Google News, PR Newswire, etc.).
            # Use when display name is ambiguous (e.g. "Lark") but press uses a fuller name ("Lark Hotels").
            press_search_name = competitor.name
            from_endpoint = False
            for ep in endpoints:
                opts = getattr(ep, "extra_options", None) if ep else None
                if isinstance(opts, dict) and opts.get("press_search_name"):
                    press_search_name = (opts.get("press_search_name") or "").strip() or press_search_name
                    from_endpoint = True
                    break
            # Fallback when no endpoint has press_search_name (e.g. Lark added via UI): use canonical search name
            # so "Lark" and "Lark Hotels" both get external search with "Lark Hotels" (Google News also fetches "Lark").
            if not from_endpoint and press_search_name:
                _press_search_fallback = {"lark": "Lark Hotels"}
                key = press_search_name.strip().lower()
                if key in _press_search_fallback:
                    press_search_name = _press_search_fallback[key]

            # 1) Primary: user-provided company news links (saved in DB on Add company / Add Source).
            #    These run first and are highest priority for dedup (e.g. Lark company news page).
            raw_items: list[dict] = []
            source_meta: list[dict] = []

            for endpoint in endpoints:
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
                items = snapshot.get("items") or []
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    url = (item.get("url") or item.get("link") or "").strip()
                    if url and not url.startswith("http"):
                        from urllib.parse import urljoin

                        url = urljoin(snapshot.get("source_url") or endpoint.url, url)
                    raw_items.append(
                        {
                            "title": item.get("title"),
                            "url": url,
                            "date": item.get("date"),
                            "source": item.get("source") or snapshot.get("source_url"),
                            "provider": "press_endpoint",
                        }
                    )
                source_meta.append({"type": "press_endpoint", "url": endpoint.url})

            endpoint_count = sum(1 for it in raw_items if (it.get("provider") or "").strip() == "press_endpoint")
            print(f"[press] Step 1 — User press endpoints: {endpoint_count} items")

            max_per_source = settings.press_max_items_per_source
            # Only user press endpoints, PR Newswire, and Google News (90-day window).
            window_days = 90

            # 2) Google News: 90-day window, quoted competitor name (and first-word + partnership fallbacks).
            gn_items = []
            if getattr(settings, "press_enable_google_news", True) and press_search_name:
                try:
                    gn_items = collect_google_news_items(
                        press_search_name,
                        max_items=min(50, max_per_source * 2),
                        window_days=window_days,
                    )
                    raw_items.extend(gn_items)
                    if gn_items:
                        source_meta.append({"type": "google_news"})
                    print(f"[press] Step 2 — Google News (90d): {len(gn_items)} items")
                    if not gn_items and press_search_name:
                        print(
                            f"[press]   (0 items for {press_search_name!r}; RSS may omit if exact phrase not in headline)"
                        )
                except Exception as e:
                    print(f"[press] Step 2 — Google News failed: {e}")

            # 3) PR Newswire (company name search). 90-day window to match Google News.
            prn_items = []
            try:
                prn_items = collect_prnewswire_items(
                    press_search_name,
                    max_items=100,
                    window_days=window_days,
                )
                raw_items.extend(prn_items)
                if prn_items:
                    source_meta.append({"type": "prnewswire"})
            except Exception as e:
                print(f"[press] Step 3 — PR Newswire failed: {e}")
            print(f"[press] Step 3 — PR Newswire (90d): {len(prn_items)} items")

            # Log raw counts per source.
            by_provider = {}
            for it in raw_items:
                p = (it.get("provider") or "").strip() or "unknown"
                by_provider[p] = by_provider.get(p, 0) + 1
            print(f"[press] Step 4 — Raw total: {len(raw_items)} by source: {by_provider}")

            if not raw_items:
                print(f"[press] Step 4 — Raw total: 0 → skipping (no items)")
                log_run(
                    session,
                    competitor.id,
                    "press",
                    "skipped",
                    message="no_press_items",
                    extra={"endpoints": [ep.url for ep in endpoints]},
                )
                continue

            # Apply 90-day window to non–PR Newswire items; keep all PR Newswire (press releases) regardless of age.
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
                print(f"[press] Step 5 — After 90d window: 0 items → skipping (all outside window)")
                log_run(
                    session,
                    competitor.id,
                    "press",
                    "skipped",
                    message="all_items_outside_window",
                    extra={"endpoints": [ep.url for ep in endpoints]},
                )
                continue

            max_raw = settings.press_max_raw_items_per_competitor
            if len(filtered_items) > max_raw:
                filtered_items = filtered_items[:max_raw]
            print(f"[press] Step 5 — After 90d window + cap (PR Newswire always kept) (max_raw={max_raw}): {len(filtered_items)} items")

            # 4) Load previous snapshot; only run enrichment when there are new items (LLM applies to new articles only).
            latest = load_latest_snapshot(session, competitor.id, "press")
            previous_structured = (latest.structured_json or {}) if latest else {}
            previous_items = previous_structured.get("items") or []
            previous_canonical = previous_structured.get("canonical_items") or []
            previous_urls = {(it.get("url") or it.get("link") or "").strip() for it in previous_items if (it.get("url") or it.get("link") or "").strip()}
            new_items = [it for it in filtered_items if (it.get("url") or it.get("link") or "").strip() not in previous_urls]

            # Snapshot has valid LLM groupings if it has at least one group with articles (used for dossier "Press" section).
            existing_groups = previous_structured.get("press_groups") or []
            has_valid_press_groups = any(
                isinstance(g, dict) and (g.get("articles") or [])
                for g in existing_groups
            )

            if not new_items and has_valid_press_groups:
                # No new URLs and we already have groupings — skip to avoid redundant LLM.
                print(f"[press] {competitor.name}: no new items since last pull — skipping Steps 6–9 (enrich/dedupe/persist).")
                log_event(
                    "snapshot_unchanged",
                    competitor=competitor.name,
                    channel="press",
                    url=None,
                )
                log_run(
                    session,
                    competitor.id,
                    "press",
                    "skipped",
                    message="no_new_press_items",
                    extra={"endpoints": [ep.url for ep in endpoints], "filtered_items": len(filtered_items)},
                )
                continue

            # Run enrichment when: we have new items, or we have items but no valid groupings (backfill so dossier shows LLM groupings).
            if not new_items:
                print(f"[press] {competitor.name}: no new items but snapshot missing LLM groupings — re-running enrichment to backfill press_groups.")
            else:
                print(f"[press] Step 6 — Enriching (LLM for {len(new_items)} new item(s) only; merge with previous canonical)...")

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
            press_groups = enrich_press_items_with_llm(
                competitor.name,
                structured.get("items") or [],
                max_articles_to_summarize=settings.press_max_articles_to_summarize,
                company_domains=company_domains,
                previous_items=previous_items if previous_items else None,
                previous_canonical=previous_canonical if previous_canonical else None,
            )
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

            print(f"[press] Step 8 — Persisting snapshot for {competitor.name}")

            raw_hash = _build_press_raw_hash(filtered_items)
            current_items = structured.get("items", [])

            persist_snapshot(
                session,
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
                    session,
                    competitor.id,
                    "press",
                    "success",
                    message="press_seed_baseline",
                    extra={"raw_items": len(raw_items), "filtered_items": len(filtered_items)},
                )
                continue

            # 6) Diff for new items and create press events (same rules as before).
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
                    session,
                    competitor.id,
                    event["type"],
                    event["title"],
                    window_days=dedupe_window_for(event["type"]),
                ):
                    create_event(session, competitor.id, event)

            print(f"[press] Step 9 — Done. Added events: {len(added_items)}")
            log_run(
                session,
                competitor.id,
                "press",
                "success",
                extra={"added_items": len(added_items), "raw_items": len(raw_items), "filtered_items": len(filtered_items)},
            )


# Hardcoded list for --local press run (no database).
LOCAL_PRESS_COMPETITORS = [
    ("Lark", "Lark Hotels"),
    ("AvantStay", "AvantStay"),
    ("Placemakr", "Placemakr"),
]


def run_press_local(competitor_name: Optional[str] = None) -> None:
    """
    Run press pipeline (Google News + PR Newswire, enrich, dedupe) without database.
    Uses LOCAL_PRESS_COMPETITORS; always fetches fresh (no snapshot skip).
    Use: python -m app.cli --local --channel press [--competitor NAME]
    """
    competitors = list(LOCAL_PRESS_COMPETITORS)
    if competitor_name:
        name_lower = (competitor_name or "").strip().lower()
        competitors = [
            (disp, search) for disp, search in competitors
            if name_lower in disp.lower() or name_lower in search.lower()
        ]
        if not competitors:
            print(f"[press] No local competitor matching {competitor_name!r}. Options: Lark, AvantStay, Placemakr.")
            return

    window_days = 90
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    max_per_source = getattr(settings, "press_max_items_per_source", 30)
    max_raw = getattr(settings, "press_max_raw_items_per_competitor", 120)

    for display_name, press_search_name in competitors:
        print(f"\n[{datetime.now(timezone.utc).isoformat()}] === PRESS (local): {display_name} ({press_search_name!r}) ===")
        raw_items: list[dict] = []

        if getattr(settings, "press_enable_google_news", True):
            try:
                gn_items = collect_google_news_items(
                    press_search_name,
                    max_items=min(50, max_per_source * 2),
                    window_days=window_days,
                )
                raw_items.extend(gn_items)
                print(f"[press] Google News (90d): {len(gn_items)} items")
            except Exception as e:
                print(f"[press] Google News failed: {e}")

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

        filtered_items = []
        for item in raw_items:
            if (item.get("provider") or "").strip().lower() == "prnewswire":
                filtered_items.append(item)
                continue
            dt = _parse_press_date(item.get("date"))
            if dt and dt < cutoff:
                continue
            filtered_items.append(item)
        if len(filtered_items) > max_raw:
            filtered_items = filtered_items[:max_raw]
        print(f"[press] After 90d + cap (PR Newswire always kept): {len(filtered_items)} items")

        if not filtered_items:
            print(f"[press] No items → skipping enrichment.")
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


def run_homepage(competitor_name: Optional[str] = None) -> None:
    with get_session() as session:
        competitors = session.query(Competitor).order_by(Competitor.name.asc()).all()
        if competitor_name:
            competitors = _filter_competitors_by_name(competitors, competitor_name)
            if not competitors:
                log_event("competitor_not_found", competitor_filter=competitor_name)
                return
        for competitor in competitors:
            endpoints = [
                ep
                for ep in competitor.source_endpoints
                if ep.channel == "homepage"
            ]
            for endpoint in endpoints:
                print(
                    f"[{datetime.now(timezone.utc).isoformat()}] homepage run: "
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
                latest = load_latest_snapshot(session, competitor.id, "homepage")
                is_first_snapshot = latest is None
                persist_snapshot(
                    session,
                    competitor.id,
                    "homepage",
                    snapshot.get("raw_content") or "",
                    snapshot.get("raw_hash") or "",
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
                        extra={"source_url": snapshot.get("source_url")},
                    )
                    continue
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


def run_public_records(competitor_name: Optional[str] = None) -> None:
    with get_session() as session:
        competitors = session.query(Competitor).order_by(Competitor.name.asc()).all()
        if competitor_name:
            competitors = _filter_competitors_by_name(competitors, competitor_name)
            if not competitors:
                log_event("competitor_not_found", competitor_filter=competitor_name)
                return
        for competitor in competitors:
            endpoints = [
                ep
                for ep in competitor.source_endpoints
                if ep.channel == "public_records"
            ]
            for endpoint in endpoints:
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
                    extra={"added_items": len(added_items)},
                )


def advance_baseline_after_full_refresh() -> None:
    """
    Set every competitor's reporting_baseline_at to now.
    Call this after a full refresh (all channels) so the executive summary and dossier
    compare against the last refresh, not the original baseline—surfacing only what
    changed since the last run (e.g. last 7 days) instead of the full period since first reset.
    """
    with get_session() as session:
        now = datetime.now(timezone.utc)
        for c in session.query(Competitor).all():
            c.reporting_baseline_at = now


def run(
    channel: Optional[str] = None,
    competitor_name: Optional[str] = None,
    local: bool = False,
) -> None:
    if local:
        if channel not in (None, "press"):
            print("[local] Only --channel press is supported without a database. Use --channel press.")
            return
        run_press_local(competitor_name=competitor_name)
        return
    if channel in (None, *RUNNER_CHANNELS):
        if channel in (None, "talent"):
            run_talent(competitor_name=competitor_name)
        if channel in (None, "asset"):
            run_asset(competitor_name=competitor_name)
        if channel in (None, "press"):
            run_press(competitor_name=competitor_name)
        if channel in (None, "homepage"):
            run_homepage(competitor_name=competitor_name)
        if channel in (None, "public_records"):
            run_public_records(competitor_name=competitor_name)
    if channel is not None and channel not in RUNNER_CHANNELS:
        print(f"[{datetime.now(timezone.utc).isoformat()}] unknown channel: {channel}")


if __name__ == "__main__":
    run()
