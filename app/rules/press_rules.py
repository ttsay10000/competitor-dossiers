from datetime import datetime
from typing import Optional

PARTNER_KEYWORDS = ["partnership", "alliance", "distribution", "channel", "platform"]
CAPITAL_KEYWORDS = ["fundraise", "funding", "debt", "restructuring", "layoff", "recap", "recapitalization"]
NARRATIVE_KEYWORDS = ["strategy change", "focus shift", "new strategy", "repositioning", "pivot"]


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
