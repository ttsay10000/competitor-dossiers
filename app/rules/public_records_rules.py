"""Rules for public records (trademark, regulatory filings) events."""

from datetime import datetime, timezone


def build_filing_event(item: dict) -> dict:
    return {
        "category": "public_record",
        "type": "public_record.filing",
        "severity": "med",
        "title": item.get("title") or "New filing detected",
        "summary": "Public record (trademark, regulatory, or similar) detected.",
        "why_it_matters": "May signal new branding, entity structure, or regulatory activity.",
        "evidence": {"item": item},
        "occurred_at": item.get("date") or datetime.now(timezone.utc).isoformat(),
    }
