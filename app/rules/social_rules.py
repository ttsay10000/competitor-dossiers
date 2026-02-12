"""Rules for social channel: build narrative.social_signal events from executive-relevant posts."""

from datetime import datetime, timezone


def build_social_signal_event(post: dict) -> dict:
    """Build event dict for an executive-relevant social post. category=narrative, type=narrative.social_signal."""
    title = (post.get("executive_summary") or post.get("text") or post.get("title") or "Social post")[:255]
    platform = (post.get("platform") or "social").lower()
    return {
        "category": "narrative",
        "type": "narrative.social_signal",
        "severity": "med",
        "title": title.strip() or "Company social signal",
        "summary": post.get("executive_summary") or post.get("text") or post.get("title") or "",
        "why_it_matters": f"Strategic signal from company {platform} channel; may indicate partnership, expansion, or positioning change.",
        "evidence": {
            "url": post.get("url"),
            "platform": platform,
            "text": (post.get("text") or post.get("title") or "")[:500],
        },
        "occurred_at": post.get("published_at") or datetime.now(timezone.utc).isoformat(),
    }
