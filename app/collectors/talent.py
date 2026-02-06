import json
from datetime import datetime
from typing import Any, Optional

from bs4 import BeautifulSoup

from .http import fetch_url, fetch_url_js


# Priority order: Lever > Greenhouse > Ashby > generic (competitor career pages).
# Use the first matching provider so we prefer structured APIs over HTML scraping.


def detect_provider(url: str) -> str:
    lowered = url.lower()
    if "lever.co" in lowered or "api.lever.co" in lowered:
        return "lever"
    if "greenhouse" in lowered:
        return "greenhouse"
    if "ashbyhq.com" in lowered:
        return "ashby"
    return "generic"


def greenhouse_jobs_api(url: str) -> Optional[str]:
    # Handles https://boards.greenhouse.io/{board}[/...]
    parts = url.split("/boards.greenhouse.io/")
    if len(parts) < 2:
        return None
    board_slug = parts[1].split("/")[0].strip()
    if not board_slug:
        return None
    return f"https://boards-api.greenhouse.io/v1/boards/{board_slug}/jobs?content=true"


def lever_jobs_api(url: str) -> Optional[str]:
    # Handles https://jobs.lever.co/{company}
    parts = url.split("lever.co/")
    if len(parts) < 2:
        return None
    company = parts[1].split("/")[0].strip()
    if not company:
        return None
    return f"https://api.lever.co/v0/postings/{company}?mode=json"


def ashby_jobs_api(url: str) -> Optional[str]:
    # Public API: GET https://api.ashbyhq.com/posting-api/job-board/{JOB_BOARD_NAME}
    # Board name is the last path segment of jobs.ashbyhq.com/BoardName
    lowered = url.lower()
    if "jobs.ashbyhq.com" in lowered:
        parts = url.split("jobs.ashbyhq.com/")
    elif "ashbyhq.com" in lowered:
        parts = url.split("ashbyhq.com/")
    else:
        return None
    if len(parts) < 2:
        return None
    path = parts[1].split("?")[0].strip().rstrip("/")
    board_name = path.split("/")[0].strip()
    if not board_name:
        return None
    return f"https://api.ashbyhq.com/posting-api/job-board/{board_name}"


def extract_ashby_jobs(payload: list[dict[str, Any]]) -> list[dict[str, Any]]:
    jobs = []
    for job in payload:
        jid = job.get("id")
        jobs.append(
            {
                "job_id": str(jid) if jid is not None else None,
                "title": job.get("title"),
                "location": job.get("location"),
                "dept": job.get("department"),
                "posted_date": job.get("publishedAt"),
                "url": job.get("jobUrl"),
            }
        )
    return jobs


def extract_greenhouse_jobs(payload: list[dict[str, Any]]) -> list[dict[str, Any]]:
    jobs = []
    for job in payload:
        jobs.append(
            {
                "job_id": str(job.get("id")),
                "title": job.get("title"),
                "location": (job.get("location") or {}).get("name"),
                "dept": (job.get("departments") or [{}])[0].get("name"),
                "posted_date": job.get("updated_at") or job.get("created_at"),
                "url": job.get("absolute_url"),
            }
        )
    return jobs


def extract_lever_jobs(payload: list[dict[str, Any]]) -> list[dict[str, Any]]:
    jobs = []
    for job in payload:
        jobs.append(
            {
                "job_id": job.get("id") or job.get("shortCode"),
                "title": job.get("text"),
                "location": (job.get("categories") or {}).get("location"),
                "dept": (job.get("categories") or {}).get("team"),
                "posted_date": job.get("createdAt"),
                "url": job.get("hostedUrl"),
            }
        )
    return jobs


def _title_from_apply_link(link) -> Optional[str]:
    """For 'Apply' / 'Apply Now' links, get job title from parent card (e.g. Kula-style layout)."""
    parent = link.parent
    while parent and parent.name not in ("body", "html"):
        for tag in ("h1", "h2", "h3", "h4", "h5", "strong"):
            heading = parent.find(tag)
            if heading:
                t = (heading.get_text() or "").strip()
                if len(t) >= 4 and len(t) <= 120 and "apply" not in t.lower():
                    return t
        # Try first text-heavy child that isn't the link
        for child in parent.children:
            if hasattr(child, "get_text") and child != link:
                t = (child.get_text() or "").strip()
                if len(t) >= 4 and len(t) <= 120 and "apply" not in t.lower():
                    return t
        parent = parent.parent
    return None


def extract_jobs_from_html(html: str) -> list[dict[str, Any]]:
    # Fallback to capture job links; allow common ATS path segments (WizeHire, Kula, etc.).
    soup = BeautifulSoup(html, "html.parser")
    href_lower_ok = ("job", "career", "position", "opening", "role", "career-site", "apply")
    jobs = []
    seen = set()
    for link in soup.find_all("a"):
        href = link.get("href") or ""
        if not href:
            continue
        h = href.lower()
        if not any(seg in h for seg in href_lower_ok):
            link_text = (link.get_text() or "").strip().lower()
            if "apply" not in link_text:
                continue
        title = (link.get_text() or "").strip()
        if not title or len(title) < 2:
            title = None
        if not title or title.lower() in ("apply", "apply now", "view"):
            title = _title_from_apply_link(link)
        if not title or len(title) < 4 or len(title) > 120:
            continue
        # Dedupe by normalized url (or title if url is #)
        url_norm = href.split("?")[0].rstrip("/") or ("title:" + title[:80])
        if url_norm in seen:
            continue
        seen.add(url_norm)
        jobs.append(
            {
                "job_id": None,
                "title": title,
                "location": None,
                "dept": None,
                "posted_date": None,
                "url": href if href.startswith("http") else None,
            }
        )
    return jobs


def normalize_jobs(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = []
    for job in jobs:
        normalized.append(
            {
                "job_id": job.get("job_id"),
                "title": (job.get("title") or "").strip(),
                "location": (job.get("location") or "").strip() or None,
                "dept": (job.get("dept") or "").strip() or None,
                "posted_date": job.get("posted_date"),
                "url": job.get("url"),
            }
        )
    return normalized


def collect_talent_snapshot(source_url: str) -> dict[str, Any]:
    """Pull all current jobs for a talent source. Priority: Lever API > Greenhouse API > Ashby API > generic HTML.
    Every run persists the full job list so we can diff later (surges, new executive postings)."""
    provider = detect_provider(source_url)

    # 1. Lever
    if provider == "lever":
        api_url = lever_jobs_api(source_url)
        if api_url:
            try:
                fetched = fetch_url(api_url)
                payload = json.loads(fetched.text)
                jobs = extract_lever_jobs(payload)
                return {
                    "provider": "lever",
                    "source_url": fetched.url,
                    "raw_content": fetched.text,
                    "raw_hash": fetched.raw_hash,
                    "jobs": normalize_jobs(jobs),
                }
            except (json.JSONDecodeError, KeyError):
                pass
        # Fall through to generic if API fails

    # 2. Greenhouse
    if provider == "greenhouse":
        api_url = greenhouse_jobs_api(source_url)
        if api_url:
            try:
                fetched = fetch_url(api_url)
                payload = json.loads(fetched.text)
                jobs = extract_greenhouse_jobs(payload.get("jobs", payload))
                return {
                    "provider": "greenhouse",
                    "source_url": fetched.url,
                    "raw_content": fetched.text,
                    "raw_hash": fetched.raw_hash,
                    "jobs": normalize_jobs(jobs),
                }
            except (json.JSONDecodeError, KeyError):
                pass
        # Fall through to generic if API fails

    # 3. Ashby
    if provider == "ashby":
        api_url = ashby_jobs_api(source_url)
        if api_url:
            try:
                fetched = fetch_url(api_url)
                data = json.loads(fetched.text)
                raw_jobs = data.get("jobs", [])
                jobs = extract_ashby_jobs(raw_jobs)
                return {
                    "provider": "ashby",
                    "source_url": fetched.url,
                    "raw_content": fetched.text,
                    "raw_hash": fetched.raw_hash,
                    "jobs": normalize_jobs(jobs),
                }
            except (json.JSONDecodeError, KeyError):
                pass
        # Fall through to generic if API fails

    # 4. Generic: competitor career page (HTML scrape). Kula (careers.kula.ai) is JS-rendered.
    if "kula.ai" in source_url.lower():
        try:
            fetched = fetch_url_js(source_url)
        except (RuntimeError, Exception):
            fetched = fetch_url(source_url)
    else:
        fetched = fetch_url(source_url)
    jobs = extract_jobs_from_html(fetched.text)
    return {
        "provider": "generic",
        "source_url": fetched.url,
        "raw_content": fetched.text,
        "raw_hash": fetched.raw_hash,
        "jobs": normalize_jobs(jobs),
    }


def build_structured_json(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "provider": snapshot.get("provider"),
        "source_url": snapshot.get("source_url"),
        "fetched_at": datetime.utcnow().isoformat(),
        "jobs": snapshot.get("jobs", []),
    }
