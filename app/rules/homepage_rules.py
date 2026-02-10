"""Rules for homepage / digital footprint change events."""

from datetime import datetime, timezone


def build_homepage_updated_event(source_url: str) -> dict:
    return {
        "category": "narrative",
        "type": "narrative.homepage_updated",
        "severity": "med",
        "title": "Homepage or product page updated",
        "summary": "Meaningful change detected on a tracked page (content hash changed).",
        "why_it_matters": "Signals possible messaging, product, or positioning update.",
        "evidence": {"source_url": source_url},
        "occurred_at": datetime.now(timezone.utc).isoformat(),
    }
