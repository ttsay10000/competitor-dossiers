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
            {"channel": "asset", "url": "https://www.placemakr.com/locations", "confidence": "high"},
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
            },
            {"channel": "press", "url": "https://avantstay.com/blog/", "confidence": "high"},
        ],
    },
    {
        "name": "Lark",
        "primary_domain": "larkhospitality.com",
        "sources": [
            {"channel": "talent", "url": "https://ats.wizehire.com/career-site/lark-hospitality", "confidence": "high"},
            {"channel": "asset", "url": "https://www.larkhospitality.com/portfolio/", "confidence": "high"},
            {"channel": "press", "url": "https://www.larkhospitality.com/press/", "confidence": "high"},
        ],
    },
]


def upsert_competitor(session, name: str, primary_domain: Optional[str]) -> Competitor:
    competitor = session.query(Competitor).filter(Competitor.name == name).first()
    if competitor:
        competitor.primary_domain = primary_domain
        return competitor
    competitor = Competitor(name=name, primary_domain=primary_domain, created_at=datetime.utcnow())
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
        return
    session.add(
        SourceEndpoint(
            competitor_id=competitor_id,
            channel=channel,
            url=url,
            confidence=confidence,
            js_required=js_required,
            use_sitemap_first=use_sitemap_first,
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
                )


if __name__ == "__main__":
    run_seed()
