from datetime import datetime
from typing import Optional

PARTNER_KEYWORDS = ["partnership", "alliance", "distribution", "channel", "platform"]
CAPITAL_KEYWORDS = ["fundraise", "funding", "debt", "restructuring", "layoff", "recap", "recapitalization"]
NARRATIVE_KEYWORDS = ["strategy change", "focus shift", "new strategy", "repositioning", "pivot"]

# Executive-level relevance: titles containing these are more likely high-signal (reduce noise).
EXECUTIVE_RELEVANCE_HINTS = [
    "ceo", "cfo", "cto", "coo", "cmo", "chief", "executive", "leadership",
    "fundraise", "funding", "series", "acquisition", "acquired", "partnership",
    "expansion", "launch", "strategic", "restructuring", "layoff",
]


def is_executive_relevant(item: dict) -> bool:
    """True if the press item title suggests executive-level relevance (reduces noise)."""
    title = (item.get("title") or "").lower()
    return any(hint in title for hint in EXECUTIVE_RELEVANCE_HINTS)


def classify_press(item: dict) -> Optional[str]:
    title = (item.get("title") or "").lower()
    if any(keyword in title for keyword in PARTNER_KEYWORDS):
        return "partner"
    if any(keyword in title for keyword in CAPITAL_KEYWORDS):
        return "capital"
    if any(keyword in title for keyword in NARRATIVE_KEYWORDS):
        return "narrative"
    return None


def build_press_event(category: str, item: dict) -> dict:
    event_type = {
        "partner": "partner.major_partnership",
        "capital": "capital.fundraise_or_restructuring",
        "narrative": "narrative.priority_shift",
    }[category]
    severity = "high" if category in {"partner", "capital"} else "med"
    return {
        "category": category,
        "type": event_type,
        "severity": severity,
        "title": item.get("title") or "Press item",
        "summary": "Press item classified as strategic signal.",
        "why_it_matters": "Indicates a strategic shift based on public narrative.",
        "evidence": {"item": item},
        "occurred_at": item.get("date") or datetime.utcnow().isoformat(),
    }
