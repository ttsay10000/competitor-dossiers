import logging
import re
import urllib.parse
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Optional

from fastapi import APIRouter, Request, Form
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import or_

from ..db import get_session, get_last_refreshed
from ..models import Competitor, CompetitorReviewProperty, Event, RunLog, Snapshot, Capability
from ..utils import to_eastern
from ..diff.asset_diff import diff_properties, delta_by_city, infer_location_for_property, is_location_treated_as_other, asset_location_display_label, parse_keys_from_details
from ..diff.talent_diff import diff_jobs
from ..executive_summary import (
    generate_executive_summary,
    generate_rollup_summary,
    format_executive_summary_for_display,
    format_rollup_summary_for_display,
    clean_location_display_for_dossier,
    clean_senior_role_bullets_for_dossier,
    aggregate_state_and_state_city_rows,
    US_STATES_LIST,
)
from ..llm_structured import _assign_state_from_url, _normalize_domain, summarize_top_news_llm
from ..rules.talent_rules import job_functional_area, FUNCTIONAL_AREA_DISPLAY_ORDER, PROPERTY_OPERATIONS_LABEL

router = APIRouter()

# Channels shown in "Refresh or populate data" (order: T A P W S R). Website = homepage/digital footprint.
DISPLAY_CHANNELS = ("talent", "asset", "press", "homepage", "social", "reviews")
CHANNEL_LETTERS = {"talent": "T", "asset": "A", "press": "P", "homepage": "W", "social": "S", "reviews": "R"}

# In-memory caches for LLM results (keyed so repeat loads / same snapshot are fast).
# Max entries to avoid unbounded growth; LRU-style eviction by clearing when over limit.
_EXEC_SUMMARY_CACHE: dict[tuple, str] = {}
_EXEC_SUMMARY_CACHE_MAX = 50
_LOCATION_CLEAN_CACHE: dict[tuple, dict] = {}
_LOCATION_CLEAN_CACHE_MAX = 100
# Bump when prompt or aggregation logic changes so cached results are invalidated and new LLM runs.
_LOCATION_CLEAN_CACHE_VERSION = 2



def _exec_summary_cache_key(competitor_id: int, context: dict) -> tuple:
    """Stable key so we reuse summary when data hasn't changed."""
    base = context.get("comparison_baseline_date") or ""
    n_events = len(context.get("events_this_week") or [])
    n_news = len(context.get("top_news") or [])
    asset_added = context.get("asset_added_since_baseline") or 0
    asset_removed = context.get("asset_removed_since_baseline") or 0
    jobs_added = context.get("jobs_added_since_baseline") or 0
    jobs_removed = context.get("jobs_removed_since_baseline") or 0
    has_asset_refresh = context.get("has_asset_refresh_since_baseline", False)
    has_talent_refresh = context.get("has_talent_refresh_since_baseline", False)
    return (competitor_id, base, n_events, n_news, asset_added, asset_removed, jobs_added, jobs_removed, has_asset_refresh, has_talent_refresh)


def _location_clean_cache_key(name: str, props: list, deltas: list, other_bullets: Optional[str] = None) -> tuple:
    props_sig = tuple((r.get("location"), r.get("count"), r.get("keys", 0)) for r in (props or [])[:100])
    deltas_sig = tuple((r.get("location"), r.get("added"), r.get("removed")) for r in (deltas or [])[:50])
    other_sig = (other_bullets or "")[:500]
    return (_LOCATION_CLEAN_CACHE_VERSION, name, props_sig, deltas_sig, other_sig)


def clear_dossier_caches_for_competitor(competitor_id: int) -> None:
    """Clear in-memory LLM caches so dossier and lazy endpoints show fresh data after a refresh."""
    global _EXEC_SUMMARY_CACHE, _LOCATION_CLEAN_CACHE
    _EXEC_SUMMARY_CACHE = {k: v for k, v in _EXEC_SUMMARY_CACHE.items() if k[0] != competitor_id}
    _LOCATION_CLEAN_CACHE.clear()  # keys are (competitor_name, ...); clear all to avoid stale location data


# Roll-up summary for competitors page: keyed by (id, hash(summary)) per competitor so cache invalidates when any summary changes.
_ROLLUP_CACHE: dict[tuple, str] = {}
_ROLLUP_CACHE_MAX = 20


def _collect_news_items_for_rollup(competitor_name: str, context: dict) -> list[dict]:
    """Extract news items with title, one-line summary, and URL from press_groups for the email report."""
    from datetime import datetime, timedelta, timezone

    items = []
    press_groups = context.get("press_groups") or []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=21)).date()
    for g in press_groups:
        group_summary = (g.get("one_line_summary") or "").strip()
        for art in g.get("articles") or []:
            url = (art.get("url") or art.get("link") or "").strip()
            if not url or not url.startswith("http"):
                continue
            title = (art.get("display_title") or art.get("title") or "Untitled").strip()
            date_str = (art.get("date") or "").strip()[:10]
            try:
                art_date = datetime.strptime(date_str, "%Y-%m-%d").date() if len(date_str) >= 10 else None
            except ValueError:
                art_date = None
            if art_date and art_date < cutoff:
                continue
            items.append({
                "competitor_name": competitor_name,
                "title": title[:200],
                "one_line_summary": group_summary[:300] if group_summary else "",
                "url": url,
                "date": date_str or "no date",
            })
    return items


def get_rollup_summary(session):
    """
    Collect executive summaries, top news (with links), and property counts for all competitors,
    then produce one roll-up via LLM. Returns (rollup_text, empty_reason).
    empty_reason: "no_summaries" when no exec summaries exist, "no_api_key", "rollup_failed"; None on success.
    """
    from ..config import settings
    import hashlib
    if not settings.openai_api_key:
        return (None, "no_api_key")
    competitors = session.query(Competitor).order_by(Competitor.created_at.desc()).all()
    collected = []  # list of (competitor_id, name, summary_text)
    all_news = []
    property_counts = []
    for c in competitors:
        if not getattr(c, "is_active", True):
            continue
        context = build_dossier_context(session, c.id, skip_property_llm=True)
        if "error" in context:
            continue
        cache_key = _exec_summary_cache_key(c.id, context)
        summary = _EXEC_SUMMARY_CACHE.get(cache_key)
        if summary is None:
            summary = generate_executive_summary(context)
            if summary and len(_EXEC_SUMMARY_CACHE) >= _EXEC_SUMMARY_CACHE_MAX:
                _EXEC_SUMMARY_CACHE.clear()
            if summary:
                _EXEC_SUMMARY_CACHE[cache_key] = summary
        if summary:
            collected.append((c.id, c.name, summary))
        all_news.extend(_collect_news_items_for_rollup(c.name, context))
        total = context.get("total_properties") or 0
        added = context.get("asset_added_since_baseline") or 0
        removed = context.get("asset_removed_since_baseline") or 0
        has_refresh = context.get("has_asset_refresh_since_baseline", False)
        property_counts.append({
            "name": c.name,
            "total": total,
            "added": added,
            "removed": removed,
            "has_refresh": has_refresh,
        })
    if not collected:
        return (None, "no_summaries")
    news_sig = hashlib.sha256(str(sorted((n.get("url", ""), n.get("date", "")) for n in all_news)).encode()).hexdigest()
    props_sig = hashlib.sha256(str([(p["name"], p["total"], p["added"], p["removed"]) for p in property_counts]).encode()).hexdigest()
    rollup_cache_key = (
        tuple((cid, hashlib.sha256(s.encode()).hexdigest()) for cid, _n, s in collected),
        news_sig,
        props_sig,
    )
    if rollup_cache_key in _ROLLUP_CACHE:
        return (_ROLLUP_CACHE[rollup_cache_key], None)
    if len(_ROLLUP_CACHE) >= _ROLLUP_CACHE_MAX:
        _ROLLUP_CACHE.clear()
    rollup = generate_rollup_summary(collected, all_news=all_news, property_counts=property_counts)
    if not rollup:
        return (None, "rollup_failed")
    _ROLLUP_CACHE[rollup_cache_key] = rollup
    return (rollup, None)


# Suggested next actions based on event types (rules-based).
RECOMMENDATIONS_MAP = {
    "asset.new_market": ("Review our presence and positioning in that market.", "Footprint expansion"),
    "asset.market_exit": ("Confirm exit and assess implications for our strategy.", "Potential retreat"),
    "asset.pipeline_signal": ("Track pipeline; consider competitive response when they launch.", "Pipeline signal"),
    "talent.senior_hire_or_role_posted": ("Monitor for org/strategy shifts; benchmark our own senior hiring.", "Senior hire"),
    "talent.new_capability": ("Assess our capability in that area; consider counter-investment.", "New capability"),
    "talent.hiring_surge": ("Interpret as strategic emphasis; review our roadmap in that function.", "Hiring surge"),
    "partner.major_partnership": ("Evaluate impact on distribution; consider similar or counter partnerships.", "Partnership"),
    "partner.partnership_surge": ("Watch for distribution strategy shift.", "Partnership surge"),
    "capital.fundraise_or_restructuring": ("Monitor for positioning and pricing changes post-capital.", "Capital event"),
    "narrative.priority_shift": ("Align messaging and positioning with their stated priorities.", "Narrative shift"),
    "narrative.homepage_updated": ("Review the updated page for messaging or product changes.", "Digital footprint"),
    "narrative.coming_soon": ("Review for pipeline or market-entry signal.", "Digital footprint"),
    "narrative.social_signal": ("Review post for partnership, expansion, or positioning signal.", "Social"),
    "public_record.filing": ("Review filing for branding or entity strategy implications.", "Public record"),
}


def _utc_dt(dt: Optional[datetime]) -> Optional[datetime]:
    """Return datetime as UTC-aware; avoid naive/aware comparison errors."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_press_date(value) -> Optional[datetime]:
    """Best-effort parse for press item dates (RSS or ISO strings). Always returns UTC-aware or None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return _utc_dt(value)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc)
        except Exception:
            return None
    if isinstance(value, str):
        val = value.strip()
        if not val:
            return None
        try:
            return _utc_dt(datetime.fromisoformat(val.replace("Z", "+00:00")))
        except Exception:
            pass
        try:
            return _utc_dt(parsedate_to_datetime(val))
        except Exception:
            return None
    return None


def _event_dict(e) -> dict:
    """Serializable event for templates (avoids DetachedInstanceError)."""
    detected = e.detected_at.strftime("%Y-%m-%d") if getattr(e, "detected_at", None) else None
    occurred = e.occurred_at.strftime("%Y-%m-%d") if getattr(e, "occurred_at", None) else None
    return {
        "title": e.title,
        "summary": e.summary,
        "severity": e.severity,
        "category": e.category,
        "type": e.type,
        "detected_at_str": detected,
        "occurred_at_str": occurred,
        "why_it_matters": e.why_it_matters,
        "evidence_json": e.evidence_json,
    }


def build_recommendations(events: list) -> list[dict]:
    """Build suggested next actions from events (deduplicated by type)."""
    seen_types = set()
    out = []
    for event in events:
        if not event.type or event.type in seen_types:
            continue
        pair = RECOMMENDATIONS_MAP.get(event.type)
        if pair:
            action, reason = pair
            seen_types.add(event.type)
            out.append({"action": action, "reason": reason, "event_type": event.type})
    if not out:
        out.append({"action": "No specific actions this period; maintain routine monitoring.", "reason": "No high-signal events", "event_type": None})
    return out


def format_dossier_preview_text(context: dict) -> str:
    """Format dossier context as plain text matching what the site displays (for terminal debug)."""
    if context.get("error"):
        return f"[Error] {context['error']}"
    name = (context.get("competitor") or {}).get("name") or "Unknown"
    lines = [
        "",
        "=" * 60,
        f"  SITE PREVIEW: {name}",
        "=" * 60,
        "",
        "--- Executive summary ---",
        "(On site: AI summary or 'Loading…' / 'Set OPENAI_API_KEY…')",
        "",
        "--- Top news (up to 5) ---",
    ]
    top_news = context.get("top_news") or []
    if top_news:
        for item in top_news:
            date_str = item.get("date") or ""
            bullet = item.get("bullet") or item.get("title") or "—"
            parts = [f"  {date_str} — {bullet}" if date_str else f"  {bullet}"]
            url = item.get("url") or item.get("link") or ""
            if url:
                parts.append(f"    URL: {url}")
            group_title = item.get("group_title") or ""
            if group_title:
                parts.append(f"    Group: {group_title}")
            topic = (item.get("topic") or "").replace("_", " ")
            if topic:
                parts.append(f"    Topic: {topic}")
            outlet = item.get("outlet") or ""
            if outlet:
                parts.append(f"    Outlet: {outlet}")
            raw_summary = item.get("summary") or ""
            if raw_summary:
                summary = raw_summary[:120] + ("…" if len(raw_summary) > 120 else "")
                parts.append(f"    Summary: {summary}")
            lines.append("\n".join(parts))
            lines.append("")
    else:
        lines.append("  No top news summary.")
        lines.append("")
    lines.append("--- Properties by location ---")
    total = context.get("total_properties") or 0
    if total:
        lines.append(f"  Total: {total} propert{'ies' if total != 1 else 'y'}")
    props = context.get("properties_by_location") or []
    if props:
        for row in props:
            loc = row.get("location") or ""
            count = row.get("count") or 0
            keys = row.get("keys") or 0
            keys_str = f" ({keys} keys)" if keys else ""
            lines.append(f"  · {loc}: {count} propert{'ies' if count != 1 else 'y'}{keys_str}")
        other_display = context.get("other_properties_display") or []
        if other_display and any((r.get("location") or "").strip() == "Other" for r in props):
            for op in other_display:
                lines.append(f"      - {op.get('name') or 'Unnamed'} — {op.get('raw_location') or op.get('market') or op.get('url') or '—'}")
    else:
        lines.append("  No property data yet. Run the asset collector to populate.")
    lines.append("")
    lines.append("--- Talent snapshot ---")
    talent_jobs = context.get("talent_jobs") or []
    jobs_bf = context.get("jobs_by_function") or []
    jobs_prop = context.get("jobs_by_function_property") or []
    if talent_jobs:
        lines.append(f"  Total roles: {len(talent_jobs)}")
        lines.append("  Business & strategy:")
        if jobs_bf:
            for row in jobs_bf:
                s = f" ({row.get('senior')} senior)" if row.get("senior") else ""
                lines.append(f"    · {row.get('function')}: {row.get('total')} role{'s' if (row.get('total') or 0) != 1 else ''}{s}")
        else:
            lines.append("    No business/strategy roles in this snapshot.")
        lines.append("  Property operations:")
        if jobs_prop:
            for row in jobs_prop:
                s = f" ({row.get('senior')} senior)" if row.get("senior") else ""
                lines.append(f"    · {row.get('function')}: {row.get('total')} role{'s' if (row.get('total') or 0) != 1 else ''}{s}")
        else:
            lines.append("    No property operations roles in this snapshot.")
    else:
        lines.append("  No talent data yet. Run the talent collector to populate.")
    lines.append("")
    lines.append("--- Press (90d, business-focused) ---")
    press_90d = context.get("press_90d") or []
    if press_90d:
        lines.append(f"  Total items: {len(press_90d)}")
        for item in press_90d[:5]:
            date_str = item.get("date") or "Date unknown"
            title = item.get("display_title") or item.get("title") or "Article"
            lines.append(f"  · {date_str} — {title}")
        if len(press_90d) > 5:
            lines.append(f"  … and {len(press_90d) - 5} more")
    else:
        lines.append("  No business-relevant press.")
    lines.append("")
    lines.append("--- Recent events (90 days) ---")
    events = context.get("events") or []
    if events:
        lines.append(f"  Total events: {len(events)}")
        for e in events[:5]:
            title = e.get("title") or "Event"
            when = e.get("occurred_at_str") or e.get("detected_at_str") or ""
            cat = e.get("category") or ""
            typ = e.get("type") or ""
            lines.append(f"  · {when} — {title} [{cat} / {typ}]")
        if len(events) > 5:
            lines.append(f"  … and {len(events) - 5} more")
    else:
        lines.append("  No recent events.")
    lines.append("")
    lines.append("--- Website changes / digital footprint ---")
    df_events = context.get("digital_footprint_events") or []
    if df_events:
        for e in df_events[:5]:
            title = e.get("title") or "Event"
            when = e.get("occurred_at_str") or e.get("detected_at_str") or ""
            typ = e.get("type") or ""
            lines.append(f"  · {when} — {title} [{typ}]")
        if len(df_events) > 5:
            lines.append(f"  … and {len(df_events) - 5} more")
    else:
        lines.append("  No website or digital footprint changes since last refresh.")
    lines.append("")
    lines.append("--- Social media updates ---")
    social_posts = context.get("social_posts") or []
    if social_posts:
        lines.append(f"  Total posts: {len(social_posts)}")
        for p in social_posts[:5]:
            platform = p.get("platform") or "Social"
            text = (p.get("text") or p.get("title") or "—")[:80]
            if len((p.get("text") or p.get("title") or "")) > 80:
                text += "…"
            rel = " [executive-relevant]" if p.get("relevance") == "executive" else ""
            lines.append(f"  · {platform}{rel}: {text}")
        if len(social_posts) > 5:
            lines.append(f"  … and {len(social_posts) - 5} more")
    else:
        lines.append("  No social posts yet. Add Twitter/LinkedIn on Edit competitor, run social channel.")
    lines.append("")
    lines.append("--- Google reviews per property ---")
    reviews_minimal = context.get("reviews_minimal") or []
    review_properties = context.get("review_properties") or []
    if not review_properties:
        lines.append("  No properties added for reviews.")
    elif reviews_minimal:
        for r in reviews_minimal[:10]:
            name = r.get("display_name") or "Property"
            line = (r.get("line") or "—")[:80]
            trend = r.get("trend") or ""
            if trend:
                line = f"{line} [{trend}]"
            lines.append(f"  · {name}: {line}")
        if len(reviews_minimal) > 10:
            lines.append(f"  … and {len(reviews_minimal) - 10} more")
    else:
        lines.append("  Run reviews channel to see sentiment.")
    lines.append("")
    return "\n".join(lines)


def _is_plausible_senior_display_title(title: str) -> bool:
    """False if text is clearly a salary/location line mis-parsed as a job title (e.g. Kula AvantStay)."""
    if not title or len(title) < 4:
        return False
    if "•" in title:
        return False
    if "/ year" in title or "/ hour" in title:
        return False
    if "USD" in title and ("Full Time" in title or "Remote" in title):
        return False
    return True


def build_dossier_context(session, competitor_id: int, *, skip_property_llm: bool = False) -> dict:
    """Build dossier context. When skip_property_llm=True, use URL-derived locations only (no LLM) for fast load."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=90)
    competitor = session.get(Competitor, competitor_id)
    if competitor is None:
        return {"error": "Competitor not found."}

    latest_asset = (
        session.query(Snapshot)
        .filter(Snapshot.competitor_id == competitor_id, Snapshot.channel == "asset")
        .order_by(Snapshot.captured_at.desc())
        .first()
    )
    # Prefer most recent talent snapshot that has jobs (Lark/AvantStay/Blueground often get 0 jobs on cron without Playwright).
    # Look beyond 20 so we don't hide a good snapshot when many recent runs persisted empty (e.g. JS careers page returning 0 jobs).
    talent_candidates = (
        session.query(Snapshot)
        .filter(Snapshot.competitor_id == competitor_id, Snapshot.channel == "talent")
        .order_by(Snapshot.captured_at.desc())
        .limit(100)
        .all()
    )
    latest_talent = None
    for s in talent_candidates:
        jobs_in = (s.structured_json or {}).get("jobs", [])
        if jobs_in and any(isinstance(j, dict) for j in jobs_in):
            latest_talent = s
            break
    if not latest_talent and talent_candidates:
        latest_talent = talent_candidates[0]  # show latest even if empty
    latest_press = (
        session.query(Snapshot)
        .filter(Snapshot.competitor_id == competitor_id, Snapshot.channel == "press")
        .order_by(Snapshot.captured_at.desc())
        .first()
    )

    # Include events that are "recent" by either detection or occurrence (90 days).
    # This ensures events that occurred within 90 days are shown even if they were detected earlier.
    events = (
        session.query(Event)
        .filter(
            Event.competitor_id == competitor_id,
            or_(
                Event.detected_at >= cutoff,
                (Event.occurred_at.isnot(None)) & (Event.occurred_at >= cutoff),
            ),
        )
        .order_by(Event.detected_at.desc())
        .all()
    )

    # Reporting baseline = manual seed date when set, otherwise oldest snapshot.
    # Only events and news *after* this count as "new" in summaries.
    reporting_baseline_date = None
    if getattr(competitor, "reporting_baseline_at", None):
        reporting_baseline_date = competitor.reporting_baseline_at.strftime("%Y-%m-%d")
    else:
        for ch in ("asset", "talent", "press"):
            oldest = (
                session.query(Snapshot)
                .filter(Snapshot.competitor_id == competitor_id, Snapshot.channel == ch)
                .order_by(Snapshot.captured_at.asc())
                .first()
            )
            if oldest:
                d = oldest.captured_at.strftime("%Y-%m-%d")
                if reporting_baseline_date is None or d < reporting_baseline_date:
                    reporting_baseline_date = d
    if reporting_baseline_date:
        baseline_cutoff = datetime.strptime(reporting_baseline_date, "%Y-%m-%d")
        baseline_date = baseline_cutoff.date()
        events = [
            e
            for e in events
            if e.detected_at.date() >= baseline_date
            or (e.occurred_at is not None and _utc_dt(e.occurred_at) and _utc_dt(e.occurred_at).date() >= baseline_date)
        ]
    # Order events by date published (occurred_at) when available, else detected_at, newest first.
    # Use timestamp to avoid naive/aware comparison errors.
    def _event_sort_key(e):
        dt = e.occurred_at or e.detected_at
        return (_utc_dt(dt) or datetime.min.replace(tzinfo=timezone.utc)).timestamp()
    events = sorted(events, key=_event_sort_key, reverse=True)

    capabilities = (
        session.query(Capability)
        .filter(Capability.competitor_id == competitor_id)
        .order_by(Capability.first_seen_at.asc())
        .all()
    )

    raw_asset_props = (latest_asset.structured_json or {}).get("properties", []) if latest_asset else []
    asset_props = [p for p in raw_asset_props if isinstance(p, dict)]
    # Use URL-derived state only (e.g. Avantstay path -> state). No per-property LLM; the list
    # (location - count) is sent once to the LLM in clean_location_display_for_dossier to bucket by state.
    if latest_asset and asset_props:
        asset_props = [_assign_state_from_url(p) for p in asset_props]
    # Inferred locations (URL-derived state or path slug) for summary and counts
    _locations = [infer_location_for_property(p) for p in asset_props]
    markets = sorted({loc for loc in _locations if loc != "Unspecified"})

    raw_talent_jobs = (latest_talent.structured_json or {}).get("jobs", []) if latest_talent else []
    talent_jobs = [j for j in raw_talent_jobs if isinstance(j, dict)]

    # Summarize jobs by functional area (LLM-set or rule-based fallback); split into business vs property operations.
    # When stored value is missing or "Other", try rule-based so keyword-matched roles (e.g. Property operations) are used.
    jobs_by_function = []
    jobs_by_function_property = []
    by_func: dict[str, list[dict]] = {}
    for job in talent_jobs:
        stored = job.get("functional_area")
        if stored and stored != "Other":
            func = stored
        else:
            func = job_functional_area(job)
        by_func.setdefault(func, []).append(job)
    order = {name: i for i, name in enumerate(FUNCTIONAL_AREA_DISPLAY_ORDER)}
    for func in sorted(by_func.keys(), key=lambda f: (order.get(f, 99), f)):
        jobs_list = by_func[func]
        senior_count = sum(1 for j in jobs_list if j.get("is_senior"))
        row = {"function": func, "total": len(jobs_list), "senior": senior_count}
        if senior_count > 0 and len(jobs_list) <= 5:
            def _job_location_str(j):
                loc = j.get("location")
                if isinstance(loc, str) and loc.strip():
                    return loc.strip()
                if isinstance(loc, dict):
                    return (loc.get("name") or loc.get("location_str") or "").strip() or ""
                return ""
            raw_titles = []
            for j in jobs_list:
                if not j.get("is_senior"):
                    continue
                title = (j.get("title") or "").strip()
                if not title:
                    continue
                loc_str = _job_location_str(j)
                bullet = f"{title} ({loc_str})" if loc_str else title
                raw_titles.append(bullet)
            # Exclude salary/location lines mis-parsed as titles (e.g. Kula AvantStay "United StatesUSD 190,000.../ yearFull Time• Remote")
            filtered = [t for t in raw_titles if _is_plausible_senior_display_title(t)]
            row["senior_titles"] = clean_senior_role_bullets_for_dossier(filtered) if filtered else []
        if func == PROPERTY_OPERATIONS_LABEL:
            jobs_by_function_property.append(row)
        else:
            jobs_by_function.append(row)

    raw_press_items = (latest_press.structured_json or {}).get("items", []) if latest_press else []
    press_items = [i for i in raw_press_items if isinstance(i, dict)]
    raw_canonical_press = (latest_press.structured_json or {}).get("canonical_items", []) if latest_press else []
    canonical_press = [i for i in raw_canonical_press if isinstance(i, dict)]
    raw_press_groups = (latest_press.structured_json or {}).get("press_groups", []) if latest_press else []
    press_groups_snapshot = [g for g in raw_press_groups if isinstance(g, dict) and g.get("articles")]

    takeaways = []
    if any(event.type == "asset.new_market" for event in events):
        takeaways.append("Recent footprint expansion activity detected.")
    if any(event.type == "talent.hiring_surge" for event in events):
        takeaways.append("Hiring surge suggests strategic buildout.")
    if any(event.category == "partner" for event in events):
        takeaways.append("Partnership activity indicates distribution focus.")
    if any((event.type or "").startswith("narrative.") for event in events):
        takeaways.append("Digital footprint or messaging changes detected (homepage/product/coming-soon).")
    if not takeaways:
        takeaways.append("No major strategic shifts detected in the last 120 days.")

    recommendations = build_recommendations(events)

    week_cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    events_this_week = [e for e in events if _utc_dt(e.detected_at) and _utc_dt(e.detected_at) >= week_cutoff]
    events_this_week_dicts = [_event_dict(e) for e in events_this_week]

    def _press_display_title(raw_title: str) -> str:
        """Clean slug-derived titles: strip trailing numeric IDs (e.g. PR Newswire) and title-case if all lowercase."""
        if not raw_title or not isinstance(raw_title, str):
            return raw_title or ""
        t = raw_title.strip()
        t = re.sub(r"\s+\d{7,}$", "", t)  # strip trailing space + long numeric ID (e.g. 302576370)
        if not t:
            return raw_title.strip()
        if t.islower() and len(t) > 3:
            return t.title()
        return t

    def _press_article_date_key(a: dict) -> str:
        d = (a.get("date") or "").strip()
        if d and d != "no date" and len(d) >= 10:
            return d[:10]
        return "0000-00-00"

    # Company domains: primary_domain + all source endpoint URLs. Exclude any article whose URL is on the competitor's site.
    _company_domains_norm: set[str] = set()
    if getattr(competitor, "primary_domain", None):
        _company_domains_norm.add(_normalize_domain(competitor.primary_domain))
    for ep in getattr(competitor, "source_endpoints", []) or []:
        u = (getattr(ep, "url", None) or (ep.get("url") if isinstance(ep, dict) else None) or "").strip()
        if u:
            try:
                netloc = (urllib.parse.urlparse(u).netloc or "").strip()
                if netloc:
                    _company_domains_norm.add(_normalize_domain(netloc))
            except Exception:
                pass
    # Fallback: snapshot sources if endpoints not loaded (e.g. minimal query)
    for src in ((latest_press.structured_json or {}).get("sources") or []) if latest_press else []:
        if isinstance(src, dict) and (src.get("type") == "press_endpoint" or "url" in src):
            u = (src.get("url") or "").strip()
            if u:
                try:
                    netloc = (urllib.parse.urlparse(u).netloc or "").strip()
                    if netloc:
                        _company_domains_norm.add(_normalize_domain(netloc))
                except Exception:
                    pass
    # Extra company domains from first press endpoint (e.g. extra_options.company_domains) and known aliases
    for ep in getattr(competitor, "source_endpoints", []) or []:
        if getattr(ep, "channel", None) != "press":
            continue
        opts = getattr(ep, "extra_options", None) or {}
        if isinstance(opts, dict) and opts.get("company_domains"):
            for d in opts["company_domains"]:
                if isinstance(d, str) and d.strip():
                    _company_domains_norm.add(_normalize_domain(d.strip()))
        break
    _company_domain_aliases = {"lark": ["lark.com"], "lark hotels": ["lark.com"]}
    comp_key = (competitor.name or "").strip().lower()
    for alias in _company_domain_aliases.get(comp_key, []):
        _company_domains_norm.add(_normalize_domain(alias))

    def _is_url_on_competitor_domain(url: str) -> bool:
        """True if URL is on the competitor's own site (or has no host, e.g. relative) — exclude from output."""
        if not url:
            return False
        try:
            host = (urllib.parse.urlparse(url).netloc or "").strip()
            if not host:
                return True  # Relative/path-only URL → from company page scrape → exclude
            host_norm = _normalize_domain(host)
            if host_norm in _company_domains_norm:
                return True
            # Subdomain match: e.g. press.larkhospitality.com when domain is larkhospitality.com
            for dom in _company_domains_norm:
                if dom and (host_norm == dom or host_norm.endswith("." + dom)):
                    return True
            return False
        except Exception:
            return False

    # Build press_groups for template: use snapshot LLM groupings when present so every competitor's Press section shows grouped press (group_title, one_line_summary, articles). Fallback: one group per canonical item for old snapshots.
    # Filter out articles on competitor's own domain; sort groups by latest article date (most recent first); tag each group with that date.
    if press_groups_snapshot:
        press_groups = []
        for g in press_groups_snapshot:
            articles = []
            for art in g.get("articles") or []:
                if _is_url_on_competitor_domain(art.get("url") or art.get("link") or ""):
                    continue
                a = dict(art)
                a["display_title"] = _press_display_title(a.get("title") or "")
                articles.append(a)
            if not articles:
                continue
            articles.sort(key=_press_article_date_key, reverse=True)
            # Tag group with latest article date for ordering and display
            group_latest_dt = None
            for a in articles:
                dt = _parse_press_date(a.get("date"))
                if dt and (group_latest_dt is None or dt > group_latest_dt):
                    group_latest_dt = dt
            group_latest_date = group_latest_dt.strftime("%Y-%m-%d") if group_latest_dt else None
            press_groups.append({
                "group_title": g.get("group_title") or "News",
                "one_line_summary": g.get("one_line_summary") or "",
                "articles": articles,
                "group_latest_date": group_latest_date,
            })
        # Order groups by most recent first (group_latest_date descending; no-date groups last)
        def _group_sort_key(grp):
            gd = grp.get("group_latest_date")
            return (gd is None, -(datetime.strptime(gd, "%Y-%m-%d").timestamp() if gd and len(gd) >= 10 else 0))
        press_groups.sort(key=_group_sort_key)
    else:
        press_groups = []
        for item in canonical_press:
            if (item.get("topic") or "").strip().lower() in {"irrelevant", "promo_or_brand_marketing"}:
                continue
            if _is_url_on_competitor_domain(item.get("url") or item.get("link") or ""):
                continue
            art = {
                **dict(item),
                "display_title": _press_display_title(item.get("title") or ""),
                "url": item.get("url") or item.get("link"),
                "outlet": item.get("outlet"),
            }
            dt = _parse_press_date(item.get("date"))
            group_latest_date = dt.strftime("%Y-%m-%d") if dt else None
            press_groups.append({
                "group_title": _press_display_title(item.get("title") or ""),
                "one_line_summary": (item.get("summary") or "").strip(),
                "articles": [art],
                "group_latest_date": group_latest_date,
            })
        def _group_sort_key(grp):
            gd = grp.get("group_latest_date")
            return (gd is None, -(datetime.strptime(gd, "%Y-%m-%d").timestamp() if gd and len(gd) >= 10 else 0))
        press_groups.sort(key=_group_sort_key)

    # Flat list for sorting and Top news: from built press_groups (already filtered; no competitor-domain).
    # Attach group_title so top news can show "most relevant groupings".
    flat_for_sort = []
    for g in press_groups:
        group_title = (g.get("group_title") or "").strip() or "News"
        for art in g.get("articles") or []:
            a = dict(art)
            a["group_title"] = group_title
            flat_for_sort.append(a)
    press_90d = []
    for item in flat_for_sort:
        dt = _parse_press_date(item.get("date"))
        display_date = dt.strftime("%Y-%m-%d") if dt else None
        out = dict(item)
        if display_date:
            out["date"] = display_date
        elif item.get("date") and isinstance(item.get("date"), str) and (item.get("date") or "").strip():
            out["date"] = (item.get("date", "") or "").strip()[:20]
        out["display_title"] = _press_display_title(out.get("title") or "")
        out["_sort_dt"] = dt
        press_90d.append(out)
    press_90d.sort(key=lambda x: (x["_sort_dt"] is None, -(x["_sort_dt"].timestamp() if x["_sort_dt"] else 0)))
    for p in press_90d:
        p.pop("_sort_dt", None)

    # Top news: LLM summarizes the most interesting business news from final press groupings (past 2-3 weeks).
    # Executive summary should only include latest news; we use 21 days and send only top_news (no raw press_90d).
    top_news_raw = summarize_top_news_llm(competitor.name, press_groups, days=21)
    top_news = (top_news_raw or [])
    # Sort by date descending (most recent first); missing dates appear last
    def _top_news_date_key(item):
        d = (item.get("date") or "").strip()
        return (d[:10] if len(d) >= 10 else d) or "0000-00-00"
    top_news = sorted(top_news, key=_top_news_date_key, reverse=True)
    # If top news is empty but we have press items, pull in 1-2 most recent as fallback
    if not top_news and press_90d:
        for art in press_90d[:2]:
            top_news.append({
                "bullet": art.get("display_title") or art.get("title") or "—",
                "date": (art.get("date") or "").strip()[:10] or None,
            })
        top_news = sorted(top_news, key=_top_news_date_key, reverse=True)

    # Properties by location (state/city) for high-level week-over-week tracking.
    # Aggregate count and total keys per location (keys parsed from property details).
    # When snapshot has location_counts (e.g. Landing), use it so 0-property markets appear (upcoming areas).
    raw_location_counts = (latest_asset.structured_json or {}).get("location_counts", []) if latest_asset else []
    location_counts: dict[str, int] = {}
    location_keys: dict[str, int] = {}
    for p in asset_props:
        loc = infer_location_for_property(p)
        location_counts[loc] = location_counts.get(loc, 0) + 1
        location_keys[loc] = location_keys.get(loc, 0) + parse_keys_from_details(p.get("details"))
    if raw_location_counts:
        for item in raw_location_counts:
            if isinstance(item, dict) and "market" in item and "count" in item:
                m = (item.get("market") or "").strip()
                if m:
                    location_counts[m] = int(item.get("count", 0))
    properties_by_location = [
        {"location": loc, "count": n, "keys": location_keys.get(loc, 0)}
        for loc, n in sorted(location_counts.items(), key=lambda x: (-x[1], x[0]))
    ]
    # Per-location summary only (one bullet per location: "Location - N properties (M keys)"); no sublists.
    by_loc_list: dict[str, list[dict]] = {}
    for p in asset_props:
        loc = infer_location_for_property(p)
        by_loc_list.setdefault(loc, []).append({
            "name": (p.get("name") or "").strip() or "Unnamed",
            "details": (p.get("details") or "").strip() or None,
        })
    if raw_location_counts:
        for item in raw_location_counts:
            if isinstance(item, dict) and "market" in item:
                m = (item.get("market") or "").strip()
                if m and m not in by_loc_list:
                    by_loc_list[m] = []
    def _location_sort_key(item):
        loc, plist = item
        is_trailing = 1 if (loc or "").strip() in ("Other", "Unspecified") else 0
        return (is_trailing, -len(plist), (loc or "").lower())

    properties_by_location_with_list = [
        {
            "location": loc,
            "count": len(plist),
            "keys": location_keys.get(loc, 0),
        }
        for loc, plist in sorted(by_loc_list.items(), key=_location_sort_key)
    ]
    total_properties = len(asset_props)

    # Asset comparison baseline: when reporting_baseline_at is set, use the latest snapshot
    # at or before that time so property deltas = "since reset". Otherwise oldest snapshot (seed).
    asset_baseline_date = None
    asset_added_since_baseline = 0
    asset_removed_since_baseline = 0
    asset_delta_by_city = []
    has_asset_refresh_since_baseline = False
    if latest_asset:
        baseline_asset = None
        if getattr(competitor, "reporting_baseline_at", None):
            baseline_asset = (
                session.query(Snapshot)
                .filter(
                    Snapshot.competitor_id == competitor_id,
                    Snapshot.channel == "asset",
                    Snapshot.captured_at <= competitor.reporting_baseline_at,
                )
                .order_by(Snapshot.captured_at.desc())
                .first()
            )
        if baseline_asset is None:
            baseline_asset = (
                session.query(Snapshot)
                .filter(Snapshot.competitor_id == competitor_id, Snapshot.channel == "asset")
                .order_by(Snapshot.captured_at.asc())
                .first()
            )
        if baseline_asset:
            raw_baseline = (baseline_asset.structured_json or {}).get("properties", [])
            baseline_props = [p for p in raw_baseline if isinstance(p, dict)]
            diff = diff_properties(baseline_props, asset_props)
            asset_added_since_baseline = len(diff["added"])
            asset_removed_since_baseline = len(diff["removed"])
            asset_baseline_date = baseline_asset.captured_at.strftime("%Y-%m-%d")
            asset_delta_by_city = delta_by_city(diff["added"], diff["removed"])
            has_asset_refresh_since_baseline = baseline_asset.id != latest_asset.id

    # Talent comparison vs baseline: roles added and removed since baseline.
    jobs_added_since_baseline = 0
    jobs_removed_since_baseline = 0
    has_talent_refresh_since_baseline = False
    if latest_talent and talent_jobs:
        baseline_talent = None
        if getattr(competitor, "reporting_baseline_at", None):
            baseline_talent = (
                session.query(Snapshot)
                .filter(
                    Snapshot.competitor_id == competitor_id,
                    Snapshot.channel == "talent",
                    Snapshot.captured_at <= competitor.reporting_baseline_at,
                )
                .order_by(Snapshot.captured_at.desc())
                .first()
            )
        if baseline_talent is None:
            baseline_talent = (
                session.query(Snapshot)
                .filter(Snapshot.competitor_id == competitor_id, Snapshot.channel == "talent")
                .order_by(Snapshot.captured_at.asc())
                .first()
            )
        if baseline_talent and baseline_talent.id != latest_talent.id:
            raw_baseline_jobs = (baseline_talent.structured_json or {}).get("jobs", [])
            baseline_jobs = [j for j in raw_baseline_jobs if isinstance(j, dict)]
            talent_diff = diff_jobs(baseline_jobs, talent_jobs)
            jobs_added_since_baseline = len(talent_diff["added"])
            jobs_removed_since_baseline = len(talent_diff["removed"])
            has_talent_refresh_since_baseline = True

    # Other (non-state) properties: list kept for rollup/exec-summary context only. We never show subbullets in the UI.
    other_properties_display = []
    other_props = [p for p in asset_props if is_location_treated_as_other(infer_location_for_property(p))]
    if other_props:
        other_properties_display = [
            {
                "name": (p.get("name") or "").strip() or "Unnamed",
                "url": (p.get("url") or "").strip() or "",
                "market": (p.get("market") or "").strip() or "",
                "raw_location": infer_location_for_property(p),
            }
            for p in other_props
        ]

    # One LLM step: send only the summarized bullets (location: count, keys). No per-property data.
    # LLM reviews if grouped by state; if not, adds totals and maps regions to closest state (or keeps separate).
    # Pre-aggregate "State - City" rows into one row per state so the LLM receives state-level rows.
    properties_by_location_for_llm = aggregate_state_and_state_city_rows(properties_by_location)
    cleaned = None
    if not skip_property_llm:
        loc_key = _location_clean_cache_key(
            competitor.name, properties_by_location_for_llm, asset_delta_by_city, None
        )
        if loc_key in _LOCATION_CLEAN_CACHE:
            cleaned = _LOCATION_CLEAN_CACHE[loc_key]
        else:
            cleaned = clean_location_display_for_dossier(
                competitor.name,
                properties_by_location_for_llm,
                asset_delta_by_city,
                other_sub_bullets_text=None,
            )
            if cleaned and len(_LOCATION_CLEAN_CACHE) >= _LOCATION_CLEAN_CACHE_MAX:
                _LOCATION_CLEAN_CACHE.clear()
            if cleaned:
                _LOCATION_CLEAN_CACHE[loc_key] = cleaned
    location_totals_match = True
    if cleaned:
        properties_by_location = cleaned.get("properties_by_location") or properties_by_location_for_llm
        asset_delta_by_city = cleaned.get("asset_delta_by_city") or asset_delta_by_city
        location_totals_match = cleaned.get("location_totals_match", True)
    else:
        # No LLM or rejected: still use state-aggregated rows so display is one row per state (e.g. Vermont – 2 properties (62 keys))
        properties_by_location = properties_by_location_for_llm
    # Use same list for single-bullet display (one line per location: "Location – N properties (M keys)").
    # Other/sitemap/undefined locations show as "Other (regions, undefined, etc.)" in bullets.
    properties_by_location_with_list = [
        {"location": asset_location_display_label(r.get("location") or ""), "count": r["count"], "keys": r.get("keys", 0)}
        for r in properties_by_location
    ]
    us_states_set = frozenset(US_STATES_LIST)
    properties_by_state = [r for r in properties_by_location_with_list if (r.get("location") or "").strip() in us_states_set]
    properties_other = [r for r in properties_by_location_with_list if (r.get("location") or "").strip() not in us_states_set]

    first_talent_endpoint = next((e for e in competitor.source_endpoints if e.channel == "talent"), None)
    talent_job_board_url = first_talent_endpoint.url if first_talent_endpoint else None

    # Reviews: list of tracked properties (for "no properties added" vs populated) and minimal top-line from snapshot.
    review_properties = [
        {"id": rp.id, "place_id": rp.place_id, "display_name": rp.display_name or rp.place_id}
        for rp in (getattr(competitor, "review_properties", None) or [])
    ]
    if not review_properties:
        review_properties = [
            {"id": rp.id, "place_id": rp.place_id, "display_name": rp.display_name or rp.place_id}
            for rp in session.query(CompetitorReviewProperty).filter(CompetitorReviewProperty.competitor_id == competitor_id).all()
        ]
    latest_reviews = (
        session.query(Snapshot)
        .filter(Snapshot.competitor_id == competitor_id, Snapshot.channel == "reviews")
        .order_by(Snapshot.captured_at.desc())
        .first()
    )
    latest_social = (
        session.query(Snapshot)
        .filter(Snapshot.competitor_id == competitor_id, Snapshot.channel == "social")
        .order_by(Snapshot.captured_at.desc())
        .first()
    )
    latest_homepage = (
        session.query(Snapshot)
        .filter(Snapshot.competitor_id == competitor_id, Snapshot.channel == "homepage")
        .order_by(Snapshot.captured_at.desc())
        .first()
    )
    social_posts = []
    if latest_social and latest_social.structured_json:
        raw = (latest_social.structured_json or {}).get("posts") or []
        social_posts = [p for p in raw if isinstance(p, dict)]
    reviews_minimal = []
    if latest_reviews and latest_reviews.structured_json:
        for p in (latest_reviews.structured_json.get("properties") or []):
            if p.get("error"):
                reviews_minimal.append({
                    "display_name": p.get("display_name") or p.get("place_id") or "Property",
                    "line": p.get("error"),
                    "trend": None,
                })
            else:
                line = (p.get("sentiment_summary") or "").strip() or "—"
                reviews_minimal.append({
                    "display_name": (p.get("display_name") or p.get("place_id") or "Property").strip(),
                    "line": line,
                    "trend": p.get("trend"),
                })

    has_any_snapshot = bool(latest_asset or latest_talent or latest_press or latest_reviews or latest_social or latest_homepage)
    has_snapshots = {
        "talent": bool(latest_talent),
        "asset": bool(latest_asset),
        "press": bool(latest_press),
        "homepage": bool(latest_homepage),
        "social": bool(latest_social),
        "reviews": bool(latest_reviews),
    }
    # Latest run per channel (for timer / "last run" on pills) and currently running channels
    last_runs = {}
    for log in (
        session.query(RunLog)
        .filter(RunLog.competitor_id == competitor_id, RunLog.channel.in_(DISPLAY_CHANNELS))
        .order_by(RunLog.created_at.desc())
    ):
        if log.channel not in last_runs:
            last_runs[log.channel] = {
                "status": log.status,
                "created_at_iso": log.created_at.isoformat() if log.created_at else None,
                "created_at_str": to_eastern(log.created_at) if log.created_at else None,
            }
    running_channels = {
        log.channel
        for log in session.query(RunLog)
        .filter(RunLog.competitor_id == competitor_id, RunLog.status == "running")
        .all()
    }
    digital_footprint_events = [
        _event_dict(e) for e in events
        if (getattr(e, "type") or "") in ("narrative.homepage_updated", "narrative.coming_soon")
    ]

    context = {
        "competitor": {"id": competitor.id, "name": competitor.name},
        "has_any_snapshot": has_any_snapshot,
        "has_snapshots": has_snapshots,
        "last_runs": last_runs,
        "running_channels": running_channels,
        "display_channels": DISPLAY_CHANNELS,
        "channel_letters": CHANNEL_LETTERS,
        "markets": markets,
        "capabilities": [{"capability": c.capability} for c in capabilities],
        "events": [_event_dict(e) for e in events],
        "digital_footprint_events": digital_footprint_events,
        "talent_jobs": talent_jobs,
        "jobs_by_function": jobs_by_function,
        "jobs_by_function_property": jobs_by_function_property,
        "talent_job_board_url": talent_job_board_url,
        "press_items": press_items,
        "press_90d": press_90d,
        "press_groups": press_groups,
        "takeaways": takeaways,
        "recommendations": recommendations,
        "events_this_week": events_this_week_dicts,
        "top_news": top_news,
        "properties_by_location": properties_by_location,
        "properties_by_location_with_list": properties_by_location_with_list,
        "properties_by_state": properties_by_state,
        "properties_other": properties_other,
        "us_states": US_STATES_LIST,
        "total_properties": total_properties,
        "location_totals_match": location_totals_match,
        "asset_baseline_date": asset_baseline_date,
        "asset_added_since_baseline": asset_added_since_baseline,
        "asset_removed_since_baseline": asset_removed_since_baseline,
        "asset_delta_by_city": asset_delta_by_city,
        "has_asset_refresh_since_baseline": has_asset_refresh_since_baseline,
        "jobs_added_since_baseline": jobs_added_since_baseline,
        "jobs_removed_since_baseline": jobs_removed_since_baseline,
        "has_talent_refresh_since_baseline": has_talent_refresh_since_baseline,
        "other_properties_display": other_properties_display,
        "comparison_baseline_date": reporting_baseline_date,
        "reviews_minimal": reviews_minimal,
        "review_properties": review_properties,
        "social_posts": social_posts,
    }
    # Executive summary is loaded lazily via JS (see GET /dossier/{id}/executive-summary) so page renders fast.
    context["executive_summary"] = None
    # Set by route when skip_property_llm and API key is set, so frontend can lazy-load refined properties
    context["properties_refinement_available"] = False
    return context


def build_summary_context(session, competitor_id: int, days: int = 7) -> dict:
    """Weekly executive summary: high-signal events from last N days, grouped by category."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    competitor = session.get(Competitor, competitor_id)
    if competitor is None:
        return {"error": "Competitor not found."}

    events = (
        session.query(Event)
        .filter(
            Event.competitor_id == competitor_id,
            or_(
                Event.detected_at >= cutoff,
                (Event.occurred_at.isnot(None)) & (Event.occurred_at >= cutoff),
            ),
        )
        .order_by(Event.detected_at.desc())
        .all()
    )
    # Prefer high/med for executive summary; include low only if needed.
    events = [e for e in events if e.severity in ("high", "med")]

    by_category = {}
    for e in events:
        by_category.setdefault(e.category, []).append(_event_dict(e))

    recommendations = build_recommendations(events)
    new_this_week = [e for e in events if e.category == "asset" or (e.type or "").startswith("asset.")]

    return {
        "competitor": {"id": competitor.id, "name": competitor.name},
        "events": [_event_dict(e) for e in events],
        "events_by_category": by_category,
        "recommendations": recommendations,
        "new_this_week": [_event_dict(e) for e in new_this_week],
        "summary_days": days,
    }


@router.get("/dossier/{competitor_id}/summary")
def summary(request: Request, competitor_id: int, days: int = 7):
    """Per-competitor weekly executive summary."""
    with get_session() as session:
        context = build_summary_context(session, competitor_id, days=days)
        context["last_refreshed"] = get_last_refreshed(session)
        all_competitors = session.query(Competitor).order_by(Competitor.created_at.desc()).all()
        context["nav_competitors"] = [{"id": c.id, "name": c.name, "created_at": c.created_at} for c in all_competitors]
    if "error" in context:
        return request.app.state.templates.TemplateResponse(
            "summary.html",
            {"request": request, "error": context["error"], "last_refreshed": context.get("last_refreshed"), "nav_competitors": context.get("nav_competitors", [])},
        )
    return request.app.state.templates.TemplateResponse(
        "summary.html",
        {"request": request, **context},
    )


@router.get("/dossier/{competitor_id}/json")
def dossier_json(competitor_id: int):
    """Full dossier as JSON (final output). Includes press_groups (group_title, one_line_summary, articles)."""
    with get_session() as session:
        context = build_dossier_context(session, competitor_id, skip_property_llm=True)
    if "error" in context:
        return JSONResponse(status_code=404, content={"error": context["error"]})
    # Build JSON-safe payload; include press groupings so final output has group_title, one_line_summary, articles.
    payload = {
        "competitor": context.get("competitor"),
        "events": context.get("events") or [],
        "press_groups": context.get("press_groups") or [],
        "press_90d": context.get("press_90d") or [],
        "top_news": context.get("top_news") or [],
        "talent_jobs": context.get("talent_jobs") or [],
        "jobs_by_function": context.get("jobs_by_function") or [],
        "properties_by_location": context.get("properties_by_location") or [],
        "total_properties": context.get("total_properties", 0),
        "asset_added_since_baseline": context.get("asset_added_since_baseline", 0),
        "asset_removed_since_baseline": context.get("asset_removed_since_baseline", 0),
        "comparison_baseline_date": context.get("comparison_baseline_date"),
    }
    return payload


@router.get("/dossier/{competitor_id}/properties-by-location")
def dossier_properties_by_location(competitor_id: int):
    """Lazy-loaded AI-refined properties by location (JSON). Used when initial page load skipped LLM for speed."""
    with get_session() as session:
        context = build_dossier_context(session, competitor_id, skip_property_llm=False)
    if "error" in context:
        return {"error": context["error"]}
    return {
        "total_properties": context.get("total_properties", 0),
        "properties_by_location": context.get("properties_by_location") or [],
        "us_states": context.get("us_states") or [],
        "other_properties_display": context.get("other_properties_display") or [],
        "location_totals_match": context.get("location_totals_match", True),
    }


@router.get("/dossier/{competitor_id}/executive-summary")
def dossier_executive_summary(competitor_id: int):
    """Lazy-loaded executive summary (JSON). One LLM call only; uses URL-derived locations and state-level topline."""
    from ..config import settings
    if not settings.openai_api_key:
        return {"summary": None, "error": "OPENAI_API_KEY not set"}
    with get_session() as session:
        # Skip property/location LLM so we do exactly one call (summary). State-level aggregation in executive_summary.
        context = build_dossier_context(session, competitor_id, skip_property_llm=True)
    if "error" in context:
        return {"summary": None, "error": context["error"]}
    cache_key = _exec_summary_cache_key(competitor_id, context)
    if cache_key in _EXEC_SUMMARY_CACHE:
        cached = _EXEC_SUMMARY_CACHE[cache_key]
        return {"summary": cached, "summary_display": format_executive_summary_for_display(cached)}
    summary = generate_executive_summary(context)
    if summary and len(_EXEC_SUMMARY_CACHE) >= _EXEC_SUMMARY_CACHE_MAX:
        _EXEC_SUMMARY_CACHE.clear()
    if summary:
        _EXEC_SUMMARY_CACHE[cache_key] = summary
    return {
        "summary": summary,
        "summary_display": format_executive_summary_for_display(summary) if summary else None,
    }


@router.post("/dossier/run-seed", status_code=303)
def run_seed_from_ui(request: Request):
    """
    Load seed_data.json and upsert competitors, sources, and review properties into the DB.
    Use after editing seed_data.json or to sync file → DB before a refresh.
    """
    from ..seed import run_seed
    run_seed()
    logging.info("Seed run from UI: upserted competitors/sources from seed_data.json.")
    return RedirectResponse(url="/competitors?seed_run=1", status_code=303)


@router.post("/dossier/export-seed", status_code=303)
def export_seed_from_ui(request: Request, next_url: Optional[str] = Form(None, alias="next")):
    """
    Write current DB competitors and sources to seed_data.json.
    Use after adding/editing competitors in the UI so the file is updated (e.g. for commit/deploy).
    If next_url is provided (e.g. from competitor-added page), redirect there with export_seed=1 or failed.
    """
    from ..seed import export_seed_to_file
    base = (next_url or "/competitors").strip()
    sep = "&" if "?" in base else "?"
    try:
        export_seed_to_file()
        logging.info("Export seed from UI: wrote seed_data.json.")
        return RedirectResponse(url=f"{base}{sep}export_seed=1", status_code=303)
    except Exception as e:
        logging.warning("Export seed from UI failed: %s", e, exc_info=True)
        return RedirectResponse(url=f"{base}{sep}export_seed=failed", status_code=303)


@router.post("/dossier/force-refresh-and-reset-baseline", status_code=303)
def force_refresh_and_reset_baseline(request: Request):
    """
    Run seed first (seed_data.json → DB), then clear all snapshots (every channel, every
    competitor), run all channels for all active competitors, then set each competitor's
    reporting_baseline_at to now. The first refresh establishes the baseline; subsequent
    refreshes compare against it so executive summaries surface what changed.
    """
    from ..runner import run as run_all_channels, advance_baseline_after_full_refresh, clear_all_snapshots, clear_all_events
    from ..db import get_session
    from ..seed import run_seed

    try:
        run_seed()
        logging.info("Force refresh: ran seed (seed_data.json → DB).")
        with get_session() as session:
            deleted = clear_all_snapshots(session, channel=None)
            events_deleted = clear_all_events(session)
        logging.info("Force refresh: cleared %d snapshot(s) and %d event(s) (all channels, all competitors).", deleted, events_deleted)
        run_all_channels()
        advance_baseline_after_full_refresh()  # Set baseline AFTER run so new snapshots become the baseline
        return RedirectResponse(url="/competitors?refreshed=1&forced=1", status_code=303)
    except Exception as e:
        logging.exception("Force refresh failed: %s", e)
        return RedirectResponse(url="/competitors?force_refresh=failed", status_code=303)


def _dossier_frame_context(session, competitor_id: int, error: Optional[str] = None) -> dict:
    """Minimal context for dossier frame so layout (h1, refresh forms, cards) always renders."""
    competitor = session.get(Competitor, competitor_id)
    comp = {"id": competitor_id, "name": (competitor.name if competitor else "Dossier")}
    all_competitors = session.query(Competitor).order_by(Competitor.created_at.desc()).all()
    nav = [{"id": c.id, "name": c.name, "created_at": c.created_at} for c in all_competitors]
    ctx = {
        "competitor": comp,
        "error": error,
        "last_refreshed": get_last_refreshed(session),
        "nav_competitors": nav,
        "has_any_snapshot": False,
        "has_snapshots": {ch: False for ch in DISPLAY_CHANNELS},
        "display_channels": DISPLAY_CHANNELS,
        "channel_letters": CHANNEL_LETTERS,
        "executive_summary": None,
        "executive_summary_lazy": False,
        "properties_refinement_available": False,
        "top_news": [],
        "total_properties": 0,
        "properties_by_location": [],
        "properties_by_state": [],
        "properties_other": [],
        "other_properties_display": [],
        "location_totals_match": True,
        "talent_jobs": [],
        "jobs_by_function": [],
        "jobs_by_function_property": [],
        "talent_job_board_url": None,
        "digital_footprint_events": [],
        "press_groups": [],
        "social_posts": [],
        "review_properties": [],
        "reviews_minimal": [],
        "events": [],
        "review_error": None,
    }
    return ctx


@router.get("/dossier/{competitor_id}")
def dossier(request: Request, competitor_id: int):
    _NO_STORE_HEADERS = {"Cache-Control": "no-store, no-cache, must-revalidate"}
    try:
        from ..config import settings
        with get_session() as session:
            # Fast load: skip property/location LLM; refined data lazy-loaded via JS
            context = build_dossier_context(session, competitor_id, skip_property_llm=True)
            context["last_refreshed"] = get_last_refreshed(session)
            all_competitors = session.query(Competitor).order_by(Competitor.created_at.desc()).all()
            context["nav_competitors"] = [{"id": c.id, "name": c.name, "created_at": c.created_at} for c in all_competitors]
            context["executive_summary_lazy"] = bool(settings.openai_api_key)
            context["properties_refinement_available"] = bool(settings.openai_api_key)
            context["review_error"] = request.query_params.get("review_error")
            context["run_blocked"] = request.query_params.get("run_blocked") == "1"
            context["run_blocked_running"] = request.query_params.get("running") or ""
    except Exception as exc:
        import traceback
        traceback.print_exc()
        with get_session() as session:
            frame = _dossier_frame_context(session, competitor_id, error=f"Dossier failed to load: {exc!s}. Check server logs for details.")
        return request.app.state.templates.TemplateResponse(
            "dossier.html",
            {"request": request, **frame},
            headers=_NO_STORE_HEADERS,
        )
    if "error" in context:
        with get_session() as session:
            frame = _dossier_frame_context(session, competitor_id, error=context["error"])
        return request.app.state.templates.TemplateResponse(
            "dossier.html",
            {"request": request, **frame},
            headers=_NO_STORE_HEADERS,
        )
    return request.app.state.templates.TemplateResponse(
        "dossier.html",
        {"request": request, **context},
        headers=_NO_STORE_HEADERS,
    )


@router.get("/dossier/{competitor_id}/run-status")
def dossier_run_status(competitor_id: int):
    """Return running and completed run logs for this competitor so the dossier can show timers and success/failure."""
    with get_session() as session:
        if session.get(Competitor, competitor_id) is None:
            return JSONResponse(content={"error": "Competitor not found."}, status_code=404)
        running = (
            session.query(RunLog)
            .filter(RunLog.competitor_id == competitor_id, RunLog.status == "running")
            .order_by(RunLog.created_at.desc())
            .all()
        )
        completed_logs = (
            session.query(RunLog)
            .filter(
                RunLog.competitor_id == competitor_id,
                RunLog.status.in_(["success", "error", "skipped"]),
            )
            .order_by(RunLog.created_at.desc())
            .all()
        )
        # Latest completion per channel
        completed_by_channel: dict[str, dict] = {}
        for log in completed_logs:
            if log.channel not in completed_by_channel:
                completed_by_channel[log.channel] = {
                    "channel": log.channel,
                    "status": log.status,
                    "message": log.message or "",
                    "created_at": log.created_at.isoformat() if log.created_at else None,
                }
        return JSONResponse(
            content={
                "running": [
                    {"channel": log.channel, "created_at": log.created_at.isoformat() if log.created_at else None}
                    for log in running
                ],
                "completed": list(completed_by_channel.values()),
            }
        )


@router.post("/dossier/{competitor_id}/refresh", status_code=303)
async def dossier_refresh(request: Request, competitor_id: int):
    """
    Run all collectors (asset, talent, press, etc.) for all competitors—same as the cron job.
    After the run, advances every competitor's comparison baseline to now so the executive
    summary and dossier compare to the last refresh (e.g. changes in the last 7 days), not
    the original baseline.
    If form field "force" is set (e.g. "Force full refresh" checked), the latest press
    snapshot for this competitor is deleted before running so the press run is not skipped
    due to hash (ensures Google News and other sources are re-fetched and new code paths run).
    """
    from ..runner import run as run_all_channels, advance_baseline_after_full_refresh, load_latest_snapshot

    form = await request.form()
    with get_session() as session:
        competitor = session.get(Competitor, competitor_id)
        if competitor is None:
            return RedirectResponse(url="/", status_code=303)
        # Force full refresh: clear latest press snapshot so run does not skip on hash.
        if form.get("force"):
            latest_press = load_latest_snapshot(session, competitor_id, "press")
            if latest_press:
                session.delete(latest_press)
    run_all_channels()
    advance_baseline_after_full_refresh()
    clear_dossier_caches_for_competitor(competitor_id)
    # Cache-busting query param so the browser doesn't serve a cached dossier page
    ts = int(datetime.now(timezone.utc).timestamp())
    return RedirectResponse(url=f"/dossier/{competitor_id}?r={ts}&refreshed=1", status_code=303)


@router.post("/dossier/{competitor_id}/seed", status_code=303)
def dossier_seed(request: Request, competitor_id: int):
    """
    Clear all snapshots (so the next run re-collects everything), run a full refresh (all
    channels), then set every competitor's comparison baseline to now. The run's new data
    becomes the baseline; the next refresh will show only changes since this run.
    """
    from ..runner import run as run_all_channels, advance_baseline_after_full_refresh, clear_all_snapshots

    with get_session() as session:
        competitor = session.get(Competitor, competitor_id)
        if competitor is None:
            frame = _dossier_frame_context(session, competitor_id, error="Competitor not found.")
            return request.app.state.templates.TemplateResponse(
                "dossier.html",
                {"request": request, **frame},
            )
        clear_all_snapshots(session, channel=None)

    run_all_channels()
    advance_baseline_after_full_refresh()
    clear_dossier_caches_for_competitor(competitor_id)
    ts = int(datetime.now(timezone.utc).timestamp())
    return RedirectResponse(url=f"/dossier/{competitor_id}?r={ts}&refreshed=1", status_code=303)

