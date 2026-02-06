import hashlib
from datetime import datetime, timedelta


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
    cutoff = datetime.utcnow() - timedelta(days=days)
    count = 0
    for job in jobs:
        if job.get("capability_bucket") != capability:
            continue
        posted = job.get("posted_date")
        if posted is None:
            continue
        try:
            posted_dt = datetime.fromisoformat(str(posted).replace("Z", "+00:00"))
        except ValueError:
            continue
        if posted_dt >= cutoff:
            count += 1
    return count
