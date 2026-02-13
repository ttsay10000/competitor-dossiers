from pathlib import Path
import json
from typing import Any, Optional

from .db import get_session
from .models import Competitor, CompetitorReviewProperty, SourceEndpoint


def _seed_data_path() -> Path:
    """Path to seed_data.json in project root (committed; survives redeploys)."""
    return Path(__file__).resolve().parent.parent / "seed_data.json"


def load_seed_competitors() -> list[dict[str, Any]]:
    """Load competitor list from seed_data.json. Falls back to SEED_COMPETITORS if file missing."""
    path = _seed_data_path()
    if not path.exists():
        return SEED_COMPETITORS
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return SEED_COMPETITORS
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "competitors" in data:
        return data["competitors"]
    return SEED_COMPETITORS


SEED_COMPETITORS = [
    {
        "name": "Placemakr",
        "primary_domain": "placemakr.com",
        "sources": [
            # Use Lever jobs URL so we get full list via API; placemakr.com/corporate/join-our-team only gave 3 (generic scrape).
            {"channel": "talent", "url": "https://jobs.lever.co/placemakr", "confidence": "high"},
            {
                "channel": "asset",
                "url": "https://www.placemakr.com/locations",
                "confidence": "high",
                "extra_options": {"strategy_chain": ["html"], "min_properties_accept": 1},
            },
        ],
    },
    {
        "name": "AvantStay",
        "primary_domain": "avantstay.com",
        "sources": [
            {"channel": "talent", "url": "https://careers.kula.ai/avantstay", "confidence": "high"},
            {
                "channel": "asset",
                "url": "https://avantstay.com/search",
                "confidence": "low",
                "js_required": True,
                "use_sitemap_first": True,
                "extra_options": {
                    "llm_extract": True,
                    "strategy_chain": ["sitemap_first", "html"],
                    "min_properties_accept": 5,
                },
            },
        ],
    },
    # Lark: single strategy "js_exhaust" only (no strategy_chain). Running Lark in the same
    # process as Avantstay can cause flakiness (e.g. Playwright/memory); run asset separately
    # per competitor when needed (e.g. --competitor Lark).
    {
        "name": "Lark",
        "primary_domain": "larkhospitality.com",
        "sources": [
            {"channel": "talent", "url": "https://ats.wizehire.com/career-site/lark-hospitality", "confidence": "high"},
            {
                "channel": "asset",
                "url": "https://www.larkhospitality.com/portfolio/",
                "confidence": "high",
                "js_required": True,
                "extra_options": {
                    "strategy": "js_exhaust",
                    "load_more": {
                        "button_text": "Load more hotels",
                        "post_load_wait_ms": 3000,
                        "click_selector": [
                            "a:has-text('Load more hotels')",
                            "button:has-text('Load more hotels')",
                            ":text('Load more hotels')",
                            "button:has-text('Load more')",
                            "button:has-text('Load More')",
                            "a:has-text('Load more')",
                            "a:has-text('Load More')",
                            "button:has-text('View more')",
                            "a:has-text('View more')",
                            "[data-testid='load-more']",
                            "button:has-text('Show more')",
                            "a:has-text('Show more')",
                        ],
                        "stop_when_selector_gone": True,
                        "wait_after_click_ms": 2000,
                        "wait_for_selector_timeout_ms": 10000,
                        "wait_after_gone_ms": 3000,
                        "wait_reappear_attempts": 5,
                        "max_clicks": 200,
                    },
                    "llm_extract": True,
                },
            },
        ],
    },
    # Blueground: same Playwright as Lark/AvantStay (talent=JS careers, asset=blueground_destinations).
    {
        "name": "Blueground",
        "primary_domain": "theblueground.com",
        "sources": [
            {"channel": "talent", "url": "https://www.theblueground.com/careers", "confidence": "medium", "js_required": True},
            {
                "channel": "asset",
                "url": "https://www.theblueground.com/destinations",
                "confidence": "medium",
                "extra_options": {"strategy": "blueground_destinations", "max_destinations": None},
            },
        ],
    },
    # Fallback list must include all competitors that are in seed_data.json so that if the file
    # is missing or unreadable on deploy (e.g. Render), the DB still gets them and cron runs include them.
    {
        "name": "Landing",
        "primary_domain": "hellolanding.com",
        "sources": [
            {
                "channel": "asset",
                "url": "https://www.hellolanding.com/locations",
                "confidence": "high",
                "extra_options": {"strategy": "landing_locations"},
            },
            {"channel": "talent", "url": "https://www.hellolanding.com/p/careers/", "confidence": "medium"},
        ],
    },
    # Rove: /search is SPA with infinite scroll; need Playwright + scroll to get full list (not just ~10 above fold).
    # Try js_exhaust first so we scroll and get all listings; sitemap often has only ~10 /listing/ URLs.
    {
        "name": "Rove",
        "primary_domain": "rovetravel.com",
        "sources": [
            {
                "channel": "asset",
                "url": "https://rovetravel.com/search",
                "confidence": "high",
                "js_required": True,
                "extra_options": {
                    "strategy_chain": ["js_exhaust", "sitemap_first", "html"],
                    "load_more": {"scroll_window": True, "scroll_wait_sec": 1.5, "max_scrolls": 150},
                },
            },
            {"channel": "talent", "url": "https://jobs.gem.com/rove", "confidence": "high"},
        ],
    },
    # Vacasa: sitemap first, then HTML fallback. (HTML-only was used when search had ~26k in static HTML;
    # if the site now uses JS for listings, HTML returns ~1; sitemap still lists all /unit/12345 URLs.)
    {
        "name": "Vacasa",
        "primary_domain": "vacasa.com",
        "sources": [
            {
                "channel": "asset",
                "url": "https://www.vacasa.com/search?place=/usa/",
                "confidence": "high",
                "use_sitemap_first": True,
                "extra_options": {"strategy_chain": ["sitemap_first", "html"], "min_properties_accept": 5},
            },
            {"channel": "talent", "url": "https://job-boards.greenhouse.io/vacasa", "confidence": "medium"},
        ],
    },
    # Kasa Living: asset page is like Lark — property cards/landing; needs JS + optional load_more + LLM extraction.
    {
        "name": "Kasa Living",
        "primary_domain": "kasa.com",
        "sources": [
            {
                "channel": "asset",
                "url": "https://kasa.com/locations",
                "confidence": "high",
                "js_required": True,
                "extra_options": {
                    "strategy": "js_exhaust",
                    "load_more": {
                        "button_text": "Load more",
                        "post_load_wait_ms": 3000,
                        "click_selector": [
                            "a:has-text('Load more')",
                            "button:has-text('Load more')",
                            ":text('Load more')",
                            "button:has-text('Load More')",
                            "a:has-text('Load more')",
                            "a:has-text('Load More')",
                            "button:has-text('View more')",
                            "a:has-text('View more')",
                            "[data-testid='load-more']",
                            "button:has-text('Show more')",
                            "a:has-text('Show more')",
                        ],
                        "stop_when_selector_gone": True,
                        "wait_after_click_ms": 2000,
                        "wait_for_selector_timeout_ms": 10000,
                        "wait_after_gone_ms": 3000,
                        "wait_reappear_attempts": 5,
                        "max_clicks": 200,
                    },
                    "llm_extract": True,
                },
            },
            {"channel": "talent", "url": "https://kasa.com/careers", "confidence": "high"},
            {
                "channel": "social",
                "url": "https://www.linkedin.com/company/kasa-living/posts/?feedView=all",
                "confidence": "high",
                "extra_options": {"platform": "linkedin"},
            },
        ],
    },
]


def upsert_competitor(session, name: str, primary_domain: Optional[str], is_active: bool = True) -> Competitor:
    competitor = session.query(Competitor).filter(Competitor.name == name).first()
    if competitor:
        competitor.primary_domain = primary_domain
        competitor.is_active = is_active
        return competitor
    competitor = Competitor(name=name, primary_domain=primary_domain, is_active=is_active)
    session.add(competitor)
    session.flush()
    return competitor


def upsert_source(
    session,
    competitor_id: int,
    channel: str,
    url: str,
    confidence: str,
    js_required: bool = False,
    use_sitemap_first: bool = False,
    extra_options: Optional[dict] = None,
) -> None:
    # Match by competitor + channel. For channel "social", also match by extra_options.platform so Twitter and LinkedIn both persist.
    q = session.query(SourceEndpoint).filter(
        SourceEndpoint.competitor_id == competitor_id,
        SourceEndpoint.channel == channel,
    )
    if channel == "social" and extra_options and extra_options.get("platform"):
        platform = extra_options.get("platform")
        candidates = q.all()
        existing = next((e for e in candidates if (e.extra_options or {}).get("platform") == platform), None)
    else:
        existing = q.first()
    if existing:
        existing.url = url
        existing.confidence = confidence
        existing.js_required = js_required
        existing.use_sitemap_first = use_sitemap_first
        existing.extra_options = extra_options
        return
    session.add(
        SourceEndpoint(
            competitor_id=competitor_id,
            channel=channel,
            url=url,
            confidence=confidence,
            js_required=js_required,
            use_sitemap_first=use_sitemap_first,
            extra_options=extra_options,
        )
    )


def upsert_review_property(
    session,
    competitor_id: int,
    place_id: str,
    display_name: Optional[str] = None,
) -> None:
    existing = (
        session.query(CompetitorReviewProperty)
        .filter(
            CompetitorReviewProperty.competitor_id == competitor_id,
            CompetitorReviewProperty.place_id == place_id,
        )
        .first()
    )
    if existing:
        existing.display_name = display_name
        return
    session.add(
        CompetitorReviewProperty(
            competitor_id=competitor_id,
            place_id=place_id,
            display_name=display_name,
        )
    )


def run_seed() -> None:
    """Upsert competitors, sources, and review properties from seed_data.json. Never deletes existing DB rows."""
    with get_session() as session:
        for entry in load_seed_competitors():
            competitor = upsert_competitor(
                session,
                entry["name"],
                entry.get("primary_domain"),
                is_active=entry.get("is_active", True),
            )
            # Prefer "sources" (list); allow "source" (single dict) so typos don't leave competitor with no endpoints.
            raw_sources = entry.get("sources") if entry.get("sources") is not None else entry.get("source")
            if isinstance(raw_sources, dict):
                raw_sources = [raw_sources]
            sources = raw_sources if isinstance(raw_sources, list) else []
            for source in sources:
                if not isinstance(source, dict) or not source.get("channel") or not source.get("url"):
                    continue
                upsert_source(
                    session,
                    competitor.id,
                    source["channel"],
                    source["url"],
                    source.get("confidence", "high"),
                    js_required=bool(source.get("js_required")),
                    use_sitemap_first=bool(source.get("use_sitemap_first")),
                    extra_options=source.get("extra_options"),
                )
            for rp in entry.get("review_properties", []):
                place_id = (rp.get("place_id") or "").strip()
                if place_id:
                    upsert_review_property(
                        session,
                        competitor.id,
                        place_id,
                        (rp.get("display_name") or "").strip() or None,
                    )


def export_seed_to_file() -> None:
    """Write current DB competitors and sources to seed_data.json. Run after adding competitors in the UI."""
    with get_session() as session:
        competitors = session.query(Competitor).order_by(Competitor.name.asc()).all()
        out = []
        for c in competitors:
            endpoints = sorted(c.source_endpoints, key=lambda e: (e.channel, e.id))
            sources = []
            for e in endpoints:
                s: dict[str, Any] = {
                    "channel": e.channel,
                    "url": e.url,
                    "confidence": e.confidence or "high",
                }
                if e.js_required:
                    s["js_required"] = True
                if e.use_sitemap_first:
                    s["use_sitemap_first"] = True
                if e.extra_options:
                    s["extra_options"] = e.extra_options
                sources.append(s)
            review_properties = []
            for rp in getattr(c, "review_properties", []) or []:
                review_properties.append({
                    "place_id": rp.place_id,
                    "display_name": rp.display_name or None,
                })
            row = {
                "name": c.name,
                "primary_domain": c.primary_domain or None,
                "sources": sources,
            }
            if review_properties:
                row["review_properties"] = review_properties
            if not getattr(c, "is_active", True):
                row["is_active"] = False
            out.append(row)
    path = _seed_data_path()
    path.write_text(json.dumps({"competitors": out}, indent=2) + "\n")
    print(f"Wrote {len(out)} competitor(s) to {path}", flush=True)


if __name__ == "__main__":
    run_seed()
