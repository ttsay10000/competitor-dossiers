import hashlib
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional


def _posted_to_datetime(posted: Any) -> Optional[datetime]:
    """Convert posted_date from API (ms, ISO str) or stored value to datetime for comparison."""
    if posted is None:
        return None
    if isinstance(posted, (int, float)):
        return datetime.fromtimestamp(posted / 1000.0, tz=timezone.utc)
    if isinstance(posted, datetime):
        return posted
    s = str(posted).strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def job_identity(job: dict) -> str:
    if job.get("job_id"):
        return str(job["job_id"])
    key = f"{job.get('title','')}|{job.get('location','')}|{job.get('dept','')}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def diff_jobs(previous: list[dict], current: list[dict]) -> dict[str, list[dict]]:
    prev_map = {job_identity(job): job for job in previous}
    curr_map = {job_identity(job): job for job in current}

    added = [job for key, job in curr_map.items() if key not in prev_map]
    removed = [job for key, job in prev_map.items() if key not in curr_map]

    return {"added": added, "removed": removed}


def count_recent_by_capability(jobs: list[dict], capability: str, days: int = 30) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    count = 0
    for job in jobs:
        if job.get("capability_bucket") != capability:
            continue
        posted_dt = _posted_to_datetime(job.get("posted_date"))
        if posted_dt is None:
            continue
        if posted_dt >= cutoff:
            count += 1
    return count
