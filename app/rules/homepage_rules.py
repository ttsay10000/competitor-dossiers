"""Rules for homepage / digital footprint change events."""

from datetime import datetime, timezone

# Phrases that indicate pipeline / market-entry / "coming soon" signals.
COMING_SOON_PHRASES = [
    "coming soon",
    "launching soon",
    "beta",
    "coming to",
    "stay tuned",
    "coming in 2025",
    "coming in 2026",
    "opening soon",
    "now available in",
    "expand to",
    "new market",
]


def detect_coming_soon_phrases(text: str) -> list[str]:
    """Scan text for COMING_SOON_PHRASES; return list of matched phrases (lowercased)."""
    if not text:
        return []
    lower = text.lower()
    found = []
    for phrase in COMING_SOON_PHRASES:
        if phrase in lower:
            found.append(phrase)
    return found


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


def build_coming_soon_event(source_url: str, phrase_or_snippet: str) -> dict:
    return {
        "category": "narrative",
        "type": "narrative.coming_soon",
        "severity": "med",
        "title": "Coming soon or pipeline signal on page",
        "summary": f"Page contains pipeline/market signal: \"{phrase_or_snippet}\".",
        "why_it_matters": "May indicate new market entry, product launch, or beta.",
        "evidence": {"source_url": source_url, "phrase": phrase_or_snippet},
        "occurred_at": datetime.now(timezone.utc).isoformat(),
    }
