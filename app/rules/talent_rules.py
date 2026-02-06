from datetime import datetime, timedelta
from typing import Optional

CAPABILITY_KEYWORDS = {
    "ai_data": ["ai", "ml", "machine learning", "data", "analytics", "automation"],
    "strategy_finance": ["strategy", "corp dev", "bizops", "strategic finance", "fp&a"],
    "partnerships": ["partnerships", "enterprise", "institutional", "alliances", "bd"],
    "real_estate": ["acquisitions", "development", "portfolio", "asset management"],
}

SENIOR_TITLES = [
    "chief",
    "cfo",
    "ceo",
    "coo",
    "cto",
    "cmo",
    "president",
    "vice president",
    "vp",
    "head",
]

GENERIC_LOCATION_HINTS = [
    "remote",
    "hybrid",
    "multiple",
    "various",
    "anywhere",
    "usa",
    "us",
    "global",
    "worldwide",
    "europe",
    "emea",
    "apac",
]


def is_specific_location(location: Optional[str]) -> bool:
    if not location:
        return False
    lowered = location.lower()
    if any(token in lowered for token in GENERIC_LOCATION_HINTS):
        return False
    if "/" in lowered or "," in lowered:
        return False
    if " or " in lowered:
        return False
    if len(lowered) > 40:
        return False
    return True


def format_role_title(job: dict) -> str:
    title = job.get("title") or "Role"
    location = job.get("location")
    if is_specific_location(location):
        return f"{title} — {location}"
    return title


def detect_capability(title: Optional[str], dept: Optional[str]) -> Optional[str]:
    haystack = " ".join([title or "", dept or ""]).lower()
    for capability, keywords in CAPABILITY_KEYWORDS.items():
        for keyword in keywords:
            if keyword in haystack:
                return capability
    return None


def is_senior_role(title: Optional[str]) -> bool:
    if not title:
        return False
    lowered = title.lower()
    return any(token in lowered for token in SENIOR_TITLES)


def assign_job_flags(job: dict) -> dict:
    capability = detect_capability(job.get("title"), job.get("dept"))
    job["capability_bucket"] = capability
    job["is_senior"] = is_senior_role(job.get("title"))
    job["is_strategic"] = capability in {"ai_data", "strategy_finance", "partnerships", "real_estate"}
    return job


def build_senior_event(job: dict) -> dict:
    role_title = format_role_title(job)
    return {
        "category": "talent",
        "type": "talent.senior_hire_or_role_posted",
        "severity": "high",
        "title": f"Senior role posted: {role_title}",
        "summary": f"New senior role posted ({role_title}) in {job.get('dept') or 'unassigned'}.",
        "why_it_matters": "Signals investment in senior leadership or strategic capability.",
        "evidence": {"job": job},
        "occurred_at": job.get("posted_date"),
    }


def build_capability_event(capability: str) -> dict:
    return {
        "category": "talent",
        "type": "talent.new_capability",
        "severity": "high",
        "title": f"New capability signal: {capability}",
        "summary": f"First observed job postings in {capability} capability area.",
        "why_it_matters": "Indicates a new functional investment that can shift competitive capabilities.",
        "evidence": {"capability": capability},
        "occurred_at": datetime.utcnow().isoformat(),
    }


def build_hiring_surge_event(capability: str, count: int) -> dict:
    return {
        "category": "talent",
        "type": "talent.hiring_surge",
        "severity": "high",
        "title": f"Hiring surge in {capability}",
        "summary": f"{count} roles posted in {capability} within 30 days.",
        "why_it_matters": "Sustained hiring indicates strategic emphasis in this capability.",
        "evidence": {"capability": capability, "count": count},
        "occurred_at": datetime.utcnow().isoformat(),
    }


def build_strategic_role_event(job: dict) -> dict:
    severity = "high" if job.get("is_senior") else "med"
    role_title = format_role_title(job)
    return {
        "category": "talent",
        "type": "talent.senior_hire_or_role_posted",
        "severity": severity,
        "title": f"Strategic role posted: {role_title}",
        "summary": f"New strategic role posted ({role_title}) in {job.get('dept') or 'unassigned'}.",
        "why_it_matters": "Suggests focused buildout in a strategic function.",
        "evidence": {"job": job},
        "occurred_at": job.get("posted_date"),
    }


def recent_threshold_crossed(previous_count: int, current_count: int, threshold: int) -> bool:
    return previous_count < threshold <= current_count


def should_dedupe(existing_events: list[dict], event: dict, window_days: int = 30) -> bool:
    cutoff = datetime.utcnow() - timedelta(days=window_days)
    for existing in existing_events:
        if existing.get("type") != event.get("type"):
            continue
        if existing.get("title") == event.get("title"):
            detected_at = existing.get("detected_at")
            if detected_at and detected_at >= cutoff:
                return True
    return False
