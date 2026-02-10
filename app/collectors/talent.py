import json
import re
from datetime import datetime, timedelta, timezone
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


def _posted_date_from_element(element) -> Optional[str]:
    """Try to extract job posted date from a job card element (e.g. <time datetime="">, data-posted-date). Returns ISO date string or None."""
    if element is None:
        return None
    # Walk up to find a reasonable card container
    node = element
    for _ in range(15):
        if node is None or node.name in ("body", "html"):
            break
        # <time datetime="2024-01-15T...">
        time_tag = node.find("time", datetime=True)
        if time_tag:
            dt = time_tag.get("datetime")
            if dt:
                return dt
        # data-posted-date, data-date, data-published (some ATSes use these)
        for attr in ("data-posted-date", "data-date", "data-published", "data-created"):
            val = node.get(attr)
            if val:
                return val
        # Text like "Posted 3 days ago" (best-effort)
        text = (node.get_text() or "").strip()
        if text:
            m = re.search(r"posted\s+(\d+)\s+day", text, re.I)
            if m:
                try:
                    d = datetime.now(timezone.utc) - timedelta(days=int(m.group(1)))
                    return d.strftime("%Y-%m-%d")
                except (ValueError, TypeError):
                    pass
        node = node.parent
    return None


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
    href_lower_ok = ("job", "career", "position", "opening", "role", "career-site", "apply", "wizehire")
    generic_cta = ("apply", "apply now", "view", "view all", "see more", "learn more")
    jobs = []
    seen = set()
    for link in soup.find_all("a"):
        href = link.get("href") or ""
        if not href:
            continue
        h = href.lower()
        href_ok = any(seg in h for seg in href_lower_ok)
        # Many ATSes (e.g. WizeHire) use <a href="#">Job Title</a> or fragment-only; accept when link text looks like a job title
        is_fragment = h in ("#", "") or h.startswith("#")
        if not href_ok and not is_fragment:
            link_text_lower = (link.get_text() or "").strip().lower()
            if "apply" not in link_text_lower:
                continue
        title = (link.get_text() or "").strip()
        if not title or len(title) < 2:
            title = None
        if not title or title.lower() in generic_cta:
            title = _title_from_apply_link(link)
        if not title or len(title) < 4 or len(title) > 120:
            continue
        if title.lower() in generic_cta:
            continue
        # Dedupe by normalized url (or title if url is #)
        url_norm = href.split("?")[0].rstrip("/") or ("title:" + title[:80])
        if url_norm in seen:
            continue
        seen.add(url_norm)
        posted_date = _posted_date_from_element(link)
        jobs.append(
            {
                "job_id": None,
                "title": title,
                "location": None,
                "dept": None,
                "posted_date": posted_date,
                "url": href if href.startswith("http") else None,
            }
        )
    return jobs


def extract_jobs_from_headings(html: str) -> list[dict[str, Any]]:
    """Fallback when link-based extraction finds nothing (e.g. WizeHire SPA with titles in headings)."""
    soup = BeautifulSoup(html, "html.parser")
    jobs = []
    seen = set()
    skip_phrases = ("we're hiring", "open position", "check out", "join our", "careers at", "job board")
    for tag in soup.find_all(["h2", "h3", "h4"]):
        title = (tag.get_text() or "").strip()
        if not title or len(title) < 4 or len(title) > 120:
            continue
        lower = title.lower()
        if any(phrase in lower for phrase in skip_phrases):
            continue
        if lower in ("apply", "apply now", "view", "other", "all departments"):
            continue
        key = title[:80]
        if key in seen:
            continue
        seen.add(key)
        posted_date = _posted_date_from_element(tag)
        jobs.append(
            {"job_id": None, "title": title, "location": None, "dept": None, "posted_date": posted_date, "url": None}
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

    # 4. Generic: competitor career page (HTML scrape). Kula and WizeHire are JS-rendered.
    use_js = "kula.ai" in source_url.lower() or "wizehire.com" in source_url.lower()
    playwright_fallback = False
    if use_js:
        try:
            fetched = fetch_url_js(source_url)
        except (RuntimeError, Exception):
            fetched = fetch_url(source_url)
            playwright_fallback = True  # Playwright disabled or not installed; plain HTML usually gives 0 jobs
    else:
        fetched = fetch_url(source_url)
    jobs = extract_jobs_from_html(fetched.text)
    if not jobs and "wizehire.com" in source_url.lower():
        jobs = extract_jobs_from_headings(fetched.text)
    out = {
        "provider": "generic",
        "source_url": fetched.url,
        "raw_content": fetched.text,
        "raw_hash": fetched.raw_hash,
        "jobs": normalize_jobs(jobs),
    }
    if playwright_fallback:
        out["playwright_fallback"] = True
    return out


def build_structured_json(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "provider": snapshot.get("provider"),
        "source_url": snapshot.get("source_url"),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "jobs": snapshot.get("jobs", []),
    }
