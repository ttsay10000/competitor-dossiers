"""
Google Reviews collector: fetch Place Details (rating, review count, up to 5 reviews)
per property; LLM summarizes sentiment. Does not store full review text.
"""
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlencode

import requests

from ..config import settings


# Legacy Place Details: fields=name,rating,user_ratings_total,reviews
PLACES_DETAILS_URL = "https://maps.googleapis.com/maps/api/place/details/json"
FIND_PLACE_URL = "https://maps.googleapis.com/maps/api/place/findplacefromtext/json"
FIELDS = "name,rating,user_ratings_total,reviews"


def resolve_place_id_from_text(query: str, api_key: str) -> Optional[tuple[str, str]]:
    """
    Resolve a search query (e.g. property name + address or Place ID) to (place_id, display_name).
    If query looks like a Place ID (starts with ChIJ), return (query, query) and do not call API.
    """
    q = (query or "").strip()
    if not q:
        return None
    if q.startswith("ChIJ") and len(q) > 20:
        return (q, q)
    key = (api_key or getattr(settings, "google_places_api_key", None) or "").strip()
    if not key:
        return None
    params = {
        "input": q,
        "inputtype": "textquery",
        "fields": "place_id,name",
        "key": key,
    }
    url = f"{FIND_PLACE_URL}?{urlencode(params)}"
    try:
        r = requests.get(url, timeout=10)
        data = r.json()
        candidates = data.get("candidates") or []
        if not candidates:
            return None
        first = candidates[0]
        place_id = first.get("place_id")
        name = first.get("name") or ""
        if place_id:
            return (place_id, name)
        return None
    except Exception:
        return None


def fetch_place_details(place_id: str, api_key: str) -> dict[str, Any]:
    """Call Google Place Details (Legacy); return result dict or empty on error."""
    params = {
        "place_id": place_id,
        "fields": FIELDS,
        "key": api_key,
    }
    url = f"{PLACES_DETAILS_URL}?{urlencode(params)}"
    try:
        r = requests.get(url, timeout=15)
        data = r.json()
        if data.get("status") == "OK" and "result" in data:
            return data["result"]
        return {}
    except Exception:
        return {}


def _recent_reviews_for_llm(reviews: list[dict]) -> str:
    """Build a short text blob of recent reviews (rating + snippet) for LLM. No full text storage."""
    if not reviews:
        return ""
    parts = []
    for i, rev in enumerate(reviews[:5], 1):
        rating = rev.get("rating")
        text = (rev.get("text") or "")[:300]  # truncate for context
        if not text:
            parts.append(f"Review {i}: rating {rating}; (no text)")
        else:
            parts.append(f"Review {i}: rating {rating}; {text}")
    return "\n\n".join(parts)


def summarize_sentiment_with_llm(property_name: str, rating_5: float, review_count: int, recent_reviews_text: str) -> str:
    """
    One short sentence summarizing overall sentiment. Uses OpenAI if key set; else fallback.
    We do not store or return full review text.
    """
    from ..config import get_openai_client
    client = get_openai_client()
    if not client or not recent_reviews_text.strip():
        if rating_5 >= 4:
            return "Mostly positive."
        if rating_5 <= 2:
            return "Generally negative."
        return "Mixed sentiment."
    prompt = (
        f"Property: {property_name}. Overall rating: {rating_5}/5 from {review_count} reviews.\n\n"
        "Recent review excerpts (rating + snippet):\n"
        f"{recent_reviews_text}\n\n"
        "In one short sentence, summarize the general sentiment (e.g. 'Mostly positive' or 'Complaints about X'). Do not quote full reviews."
    )
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=80,
        )
        text = (resp.choices[0].message.content or "").strip()
        return text[:200] if text else "Mixed sentiment."
    except Exception:
        return "Mixed sentiment."


def collect_property_review(
    place_id: str,
    display_name: Optional[str] = None,
    api_key: Optional[str] = None,
) -> dict[str, Any]:
    """
    Fetch one property's Place Details; return dict with rating_5, review_count,
    sentiment_summary, and recent_reviews (list of {rating, text_snippet} for trend use only).
    """
    key = (api_key or getattr(settings, "google_places_api_key", None) or "").strip()
    if not key:
        return {
            "place_id": place_id,
            "display_name": display_name,
            "error": "GOOGLE_PLACES_API_KEY not set",
        }
    result = fetch_place_details(place_id, key)
    if not result:
        return {
            "place_id": place_id,
            "display_name": display_name,
            "error": "Place not found or API error",
        }
    name = result.get("name") or display_name or ""
    rating = result.get("rating")
    total = result.get("user_ratings_total", 0)
    reviews = result.get("reviews") or []
    rating_5 = float(rating) if rating is not None else None
    recent_reviews_text = _recent_reviews_for_llm(reviews)
    sentiment_summary = summarize_sentiment_with_llm(name, rating_5 or 0, total, recent_reviews_text)
    # Keep only rating + short snippet per review for "recent" avg and trend (no full text stored)
    recent_reviews = []
    for r in reviews[:5]:
        recent_reviews.append({
            "rating": r.get("rating"),
            "text_snippet": (r.get("text") or "")[:150],
        })
    return {
        "place_id": place_id,
        "display_name": display_name or name,
        "rating_5": rating_5,
        "review_count": total,
        "sentiment_summary": sentiment_summary,
        "recent_reviews": recent_reviews,
    }


def build_structured_json(
    properties_data: list[dict],
    previous_snapshot: Optional[dict] = None,
) -> dict[str, Any]:
    """
    Build snapshot structured_json. For each property, add new_since_last (count, avg_rating, summary)
    and trend (NEGATIVE / POSITIVE / NEUTRAL) by comparing recent-reviews avg to overall rating.
    """
    prev_by_place = {}
    if previous_snapshot:
        for p in (previous_snapshot.get("properties") or []):
            pid = p.get("place_id")
            if pid:
                prev_by_place[pid] = p
    captured_at = datetime.now(timezone.utc).isoformat()
    out_properties = []
    for prop in properties_data:
        if prop.get("error"):
            out_properties.append({
                "place_id": prop.get("place_id"),
                "display_name": prop.get("display_name"),
                "error": prop.get("error"),
            })
            continue
        place_id = prop.get("place_id")
        prev = prev_by_place.get(place_id)
        prev_count = (prev.get("review_count") or 0) if prev else 0
        curr_count = prop.get("review_count") or 0
        new_count = max(0, curr_count - prev_count)
        recent = prop.get("recent_reviews") or []
        recent_ratings = [r.get("rating") for r in recent if r.get("rating") is not None]
        recent_avg = sum(recent_ratings) / len(recent_ratings) if recent_ratings else None
        overall_rating = prop.get("rating_5")
        # Trend: compare recent avg to overall
        if recent_avg is not None and overall_rating is not None:
            diff = recent_avg - overall_rating
            if diff >= 0.3:
                trend = "POSITIVE"
            elif diff <= -0.3:
                trend = "NEGATIVE"
            else:
                trend = "NEUTRAL"
        else:
            trend = "NEUTRAL"
        new_since_last = {
            "count": new_count,
            "avg_rating": round(recent_avg, 1) if recent_avg is not None else None,
            "summary": prop.get("sentiment_summary") if new_count else None,
        }
        out_properties.append({
            "place_id": place_id,
            "display_name": prop.get("display_name"),
            "rating_5": prop.get("rating_5"),
            "review_count": curr_count,
            "sentiment_summary": prop.get("sentiment_summary"),
            "new_since_last": new_since_last,
            "trend": trend,
        })
    return {
        "captured_at": captured_at,
        "properties": out_properties,
    }
