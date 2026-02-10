from datetime import datetime
from typing import Optional

from .db import get_session
from .models import Competitor, SourceEndpoint


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
            {"channel": "press", "url": "https://www.placemakr.com/blog", "confidence": "high"},
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
            {"channel": "press", "url": "https://avantstay.com/blog/", "confidence": "high"},
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
            {
                "channel": "press",
                "url": "https://www.larkhospitality.com/press/",
                "confidence": "high",
                "extra_options": {"press_search_name": "Lark Hotels"},
            },
        ],
    },
]


def upsert_competitor(session, name: str, primary_domain: Optional[str]) -> Competitor:
    competitor = session.query(Competitor).filter(Competitor.name == name).first()
    if competitor:
        competitor.primary_domain = primary_domain
        return competitor
    competitor = Competitor(name=name, primary_domain=primary_domain)
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
    # Match by competitor + channel so re-seeding updates URL when we change it (e.g. to Lever).
    existing = (
        session.query(SourceEndpoint)
        .filter(
            SourceEndpoint.competitor_id == competitor_id,
            SourceEndpoint.channel == channel,
        )
        .first()
    )
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


def run_seed() -> None:
    with get_session() as session:
        for entry in SEED_COMPETITORS:
            competitor = upsert_competitor(
                session,
                entry["name"],
                entry.get("primary_domain"),
            )
            for source in entry["sources"]:
                upsert_source(
                    session,
                    competitor.id,
                    source["channel"],
                    source["url"],
                    source["confidence"],
                    js_required=bool(source.get("js_required")),
                    use_sitemap_first=bool(source.get("use_sitemap_first")),
                    extra_options=source.get("extra_options"),
                )


if __name__ == "__main__":
    run_seed()
