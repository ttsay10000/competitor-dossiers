import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from .http import fetch_url, fetch_url_js, fetch_url_js_exhaust, fetch_url_js_wait_for_spa


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
    # Handles https://boards.greenhouse.io/{board}[/...] and https://job-boards.greenhouse.io/{board}[/...]
    for prefix in ("/boards.greenhouse.io/", "/job-boards.greenhouse.io/"):
        if prefix in url:
            parts = url.split(prefix)
            if len(parts) < 2:
                return None
            board_slug = parts[1].split("/")[0].split("?")[0].strip()
            if not board_slug:
                return None
            return f"https://boards-api.greenhouse.io/v1/boards/{board_slug}/jobs?content=true"
    return None


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


def _is_cta_or_section_text(text: str) -> bool:
    """True if text is a button/CTA or section heading (e.g. 'View Job', \"We're hiring\") rather than a job title."""
    if not text or len(text) > 50:
        return False
    lower = text.strip().lower()
    # Exact CTA / section phrases (never use as job title)
    skip_phrases = (
        "apply", "apply now", "view", "view all", "see more", "learn more",
        "view job", "view jobs", "view details", "view role", "view position",
        "see job", "read more", "view opening",
        "we're hiring", "we are hiring", "open positions", "join our team",
    )
    if lower in skip_phrases:
        return True
    # "View ..." or "Apply ..." with no substantive title (e.g. "View Job", "Apply Here")
    if lower.startswith("view ") or lower.startswith("apply "):
        return True
    return False


def _is_cta_link_text(text: str) -> bool:
    """True if link text is a button/CTA rather than a job title. Use for link-based extraction."""
    return _is_cta_or_section_text(text)


def _title_from_apply_link(link) -> Optional[str]:
    """For 'Apply' / 'View Job' / CTA links, get job title from parent card (e.g. Kula-style, WizeHire)."""
    # Often the title is a previous sibling (e.g. <h3>Title</h3> then <a>View Job</a>)
    for sib in link.previous_siblings:
        if getattr(sib, "name", None) in ("h1", "h2", "h3", "h4", "h5", "strong"):
            t = (sib.get_text() or "").strip() if hasattr(sib, "get_text") else ""
            if len(t) >= 4 and len(t) <= 120 and not _is_cta_link_text(t):
                return t
        if hasattr(sib, "get_text") and sib != link:
            t = (sib.get_text() or "").strip()
            if len(t) >= 4 and len(t) <= 120 and not _is_cta_link_text(t):
                return t
    parent = link.parent
    while parent and parent.name not in ("body", "html"):
        for tag in ("h1", "h2", "h3", "h4", "h5", "strong"):
            heading = parent.find(tag)
            if heading:
                t = (heading.get_text() or "").strip()
                if len(t) >= 4 and len(t) <= 120 and not _is_cta_link_text(t):
                    return t
        # Try first text-heavy child that isn't the link
        for child in parent.children:
            if hasattr(child, "get_text") and child != link:
                t = (child.get_text() or "").strip()
                if len(t) >= 4 and len(t) <= 120 and not _is_cta_link_text(t):
                    return t
        parent = parent.parent
    return None


def extract_jobs_from_html(html: str) -> list[dict[str, Any]]:
    # Fallback to capture job links; allow common ATS path segments (WizeHire, Kula, etc.).
    soup = BeautifulSoup(html, "html.parser")
    href_lower_ok = ("job", "career", "position", "opening", "role", "career-site", "apply", "wizehire")
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
            if "apply" not in link_text_lower and "view" not in link_text_lower:
                continue
        raw_title = (link.get_text() or "").strip()
        # If link text is a CTA (e.g. "View Job"), get real title from parent card
        if _is_cta_link_text(raw_title) or not raw_title or len(raw_title) < 2:
            title = _title_from_apply_link(link)
        else:
            title = raw_title
        if not title or len(title) < 4 or len(title) > 120:
            continue
        if _is_cta_link_text(title):
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


def _looks_like_job_title(title: str) -> bool:
    """True if text looks like a job title (not a department filter, location, or junk)."""
    if not title or len(title) < 4:
        return False
    lower = title.lower().strip()
    # Skip filter/section labels and UI junk
    if lower in ("all departments", "all locations", "all jobs", "• on-site", "• remote", "on-site", "remote",
                 "made with", "retail sales", "field operations", "executive"):
        return False
    if "•" in title:  # Bullet in filter UI
        return False
    # Skip department/category headings (these appear in filters, not as job titles)
    if re.search(r"\([a-z&]+\)$", lower):  # e.g. (R&D), (Market Ops)
        return False
    dept_phrases = ("people & talent", "corporate and group", "regulatory affairs", "onboarding & supply",
                    "consumer marketing", "growth marketing", "owner experience", "revenue management",
                    "strategic initiatives", "it & systems", "concierge & events", "field operations executive",
                    "central operations", "field operations -", "guest experience", "united states")
    if any(d in lower for d in dept_phrases) and not any(
        r in lower for r in ("manager", "associate", "engineer", "director", "coordinator", "specialist", "analyst")
    ):
        return False
    # Job titles typically contain role keywords
    job_keywords = (
        "manager", "associate", "agent", "engineer", "director", "coordinator",
        "specialist", "assistant", "analyst", "supervisor", "technician", "chef",
        "housekeeper", "valet", "bellman", "concierge", "receptionist", "attendant",
        "inspector", "paralegal", "strategist", "head of", "vp ", " vp", "executive",
    )
    if any(kw in lower for kw in job_keywords):
        return True
    # Multi-word titles that aren't obviously locations (e.g. "Front Desk" as two words)
    words = title.split()
    if len(words) >= 2 and not _looks_like_location_filter(title):
        return True
    return False


def _clean_location_field(location: Optional[str]) -> Optional[str]:
    """Strip salary/USD artifacts from location (Kula/AvantStay uses css-7pftiu for both location and salary)."""
    if location is None or not isinstance(location, str):
        return location
    text = location.strip()
    if not text:
        return None
    # Remove trailing " == $0", " == $50,000", " $123", " USD $50,000", " - $60,000" etc.
    text = re.sub(r"\s*==\s*\$[\d,]+(?:\.\d+)?\s*$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+USD\s+\$[\d,]+(?:\.\d+)?(?:\s*[-–]\s*\$[\d,]+(?:\.\d+)?)?\s*$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+\$[\d,]+(?:\.\d+)?(?:\s*[-–]\s*\$[\d,]+(?:\.\d+)?)?\s*$", "", text)
    # Remove leading salary if it got concatenated (e.g. "$0 Blakeslee, Pennsylvania")
    text = re.sub(r"^\s*\$[\d,]+(?:\.\d+)?(?:\s*[-–]\s*\$[\d,]+(?:\.\d+)?)?\s+", "", text)
    text = re.sub(r"^\s*USD\s+\$[\d,]+(?:\.\d+)?(?:\s*[-–]\s*\$[\d,]+(?:\.\d+)?)?\s+", "", text, flags=re.IGNORECASE)
    text = text.strip().rstrip(",")
    return text or None


def _looks_like_location_filter(text: str) -> bool:
    """True if text looks like a location filter option (city, country, address)."""
    lower = text.lower().strip()
    # Geographic suffixes
    if ", usa" in lower or ", united states" in lower or " usa" in lower:
        return True
    if any(lower.endswith(s) for s in (" argentina", " brazil", " mexico", " colombia", " chile")):
        return True
    # Address-like (starts with number, contains "suite", "unit", "blvd", "ave")
    if re.search(r"^\d+\s+\w+", lower) or re.search(r"\b(suite|unit|blvd|ave|dr|st)\b", lower):
        return True
    # Common US state abbreviations - "City, ST" pattern
    if re.search(r",\s*(ca|tx|fl|ny|pa|sc|tn|or|hi|la|ut|wa)\s*(,|$)", lower):
        return True
    return False


def _title_is_likely_city(title: str, location: Optional[str]) -> bool:
    """True if title+location suggests the title is a city/region (e.g. 'San Diego' with 'California, United States')."""
    if not location:
        return False
    lower_loc = location.lower()
    lower_title = title.lower()
    # "ST, USA" or "State, USA" or "State, United States"
    if re.search(r"^[a-z]{2},?\s*usa", lower_loc) or ", usa" in lower_loc:
        return True
    if ", united states" in lower_loc or lower_loc.endswith(" united states"):
        return True  # "California, United States", "Florida, United States"
    if any(lower_loc.endswith(s) for s in (", argentina", ", brazil", ", mexico", "argentina", "usa")):
        return True
    # Title has no job keyword and looks like a place name
    job_kw = ("manager", "associate", "agent", "engineer", "director", "coordinator", "specialist", "assistant")
    if not any(kw in lower_title for kw in job_kw) and len(title.split()) <= 3:
        if re.search(r"(beach|city|valley|lake|island|springs|harbor|bay)$", lower_title):
            return True
    return False


def extract_jobs_from_kula(html: str) -> list[dict[str, Any]]:
    """Extract jobs from Kula/AvantStay career pages. DOM: department in span.css-ypynmf,
    job line in p.chakra-text.css-f8zk62 as 'Title, Property, Location' (e.g. 'Hotel Valet & Bellman, The Code Hotel, Austin').
    Location/salary also use css-7pftiu; we only use f8zk62 for job lines and clean salary artifacts from location."""
    soup = BeautifulSoup(html, "html.parser")
    jobs = []
    seen = set()
    # Only use span.ypynmf that looks like a department (not "All departments", not a city)
    # Only use p.chakra-text.css-f8zk62 for job lines (avoid 7pftiu-only which may be location/salary only)
    current_dept = None
    for elem in soup.find_all(["span", "p"]):
        if elem.name == "span" and elem.get("class"):
            cls = " ".join(elem.get("class", []))
            if "ypynmf" in cls:
                dept_text = (elem.get_text() or "").strip()
                # Only treat as department if it looks like one (has "–" or " - ", or contains "Operations", "Marketing", etc.)
                if dept_text and dept_text not in ("All departments", "All locations"):
                    if _looks_like_location_filter(dept_text) or len(dept_text) < 3:
                        continue  # Skip city names, single chars
                    if " – " in dept_text or " - " in dept_text or any(
                        x in dept_text for x in ("Operations", "Marketing", "Finance", "Housekeeping", "Sales", "Legal", "Executive", "Guest", "Field", "Product")
                    ):
                        current_dept = dept_text
        elif elem.name == "p" and elem.get("class"):
            cls = " ".join(elem.get("class", []))
            if "chakra-text" not in cls or "f8zk62" not in cls:
                continue
            raw = (elem.get_text() or "").strip()
            if not raw or len(raw) < 4 or len(raw) > 200:
                continue
            if _is_cta_or_section_text(raw):
                continue
            if raw.startswith("USD ") or re.search(r"^\d+\.\d+-\d+\.\d+\s*/\s*(hour|year)", raw):
                continue
            # Skip compensation line (salary range + / year or / hour), e.g. "United StatesUSD 190,000.00-225,000.00 / yearFull Time• Remote"
            if re.search(r"[\d,]+\.?\d*\s*[-–]\s*[\d,]+\.?\d*\s*/\s*(year|hour)", raw):
                continue
            if "Full Time" in raw or ("Remote" in raw and "," not in raw):
                continue
            if "; " in raw and "locations" in raw.lower():  # "Mexico; Brazil; Argentina + 1 locations"
                continue
            # Parse: "Title, Location (Property)" or "Title, Property, Location"
            # Handle "Title (Scope), Location" e.g. "Head of FP&A (Global), Remote"
            if "," in raw:
                title_part, loc_part = raw.split(",", 1)
                title = title_part.strip()
                location = loc_part.strip() or None
                # Fix split inside parens: "Head of FP&A (Global), Remote" -> title ends with "(", loc="Remote)"
                if location and location.endswith(")") and "(" in title and not title.endswith(")"):
                    title = title + ")"
                    location = location[:-1].strip() or None
            else:
                title = raw
                location = None
            if not title or len(title) < 4 or len(title) > 120:
                continue
            if not _looks_like_job_title(title):
                continue
            if _looks_like_location_filter(title):
                continue
            # Skip when title is likely a city (e.g. "Great Barrington" with location "MA, USA")
            if _title_is_likely_city(title, location):
                continue
            location = _clean_location_field(location)
            key = (title[:80], location or "")
            if key in seen:
                continue
            seen.add(key)
            posted_date = _posted_date_from_element(elem)
            jobs.append({
                "job_id": None,
                "title": title,
                "location": location,
                "dept": current_dept,
                "posted_date": posted_date,
                "url": None,
            })
    return jobs


def extract_jobs_from_wizehire_title_divs(html: str) -> list[dict[str, Any]]:
    """Extract job titles from WizeHire/Lark career pages where titles are in div.jss83 (e.g. 'Hotel Housekeeper', 'Building Maintenance Technician')."""
    soup = BeautifulSoup(html, "html.parser")
    jobs = []
    seen = set()
    # Lark/WizeHire use JSS classes; job title is in a div with class jss83 (may be multiple elements with same class)
    for div in soup.find_all("div", class_=lambda c: c and "jss83" in (c if isinstance(c, str) else " ".join(c))):
        title = (div.get_text() or "").strip()
        if not title or len(title) < 4 or len(title) > 120:
            continue
        if _is_cta_or_section_text(title):
            continue
        key = title[:80]
        if key in seen:
            continue
        seen.add(key)
        posted_date = _posted_date_from_element(div)
        jobs.append(
            {"job_id": None, "title": title, "location": None, "dept": None, "posted_date": posted_date, "url": None}
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
        if lower in ("apply", "apply now", "view", "view job", "view details", "other", "all departments"):
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


def extract_jobs_from_applytojob(html: str, base_url: str = "https://landing.applytojob.com") -> list[dict[str, Any]]:
    """Extract jobs from JazzHR/applytojob.com job list (Landing's embedded careers iframe).
    Jobs are in table rows: td with job link, adjacent td with location."""
    soup = BeautifulSoup(html, "html.parser")
    jobs = []
    seen = set()
    current_dept = None
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        # Department header row: single td, no job link
        if len(tds) == 1:
            txt = (tds[0].get_text() or "").strip()
            if txt and len(txt) < 80 and not tds[0].find("a", href=lambda h: h and "/jobs/details/" in (h or "")):
                current_dept = txt
            continue
        # Job row: td with link, next td with location
        link = tr.find("a", href=lambda h: h and "/jobs/details/" in (h or ""))
        if not link or len(tds) < 2:
            continue
        title = (link.get_text() or "").strip()
        if not title or len(title) < 4 or len(title) > 120:
            continue
        href = link.get("href") or ""
        job_url = urljoin(base_url, href) if href else None
        location = (tds[1].get_text() or "").strip() or None
        key = (title[:80], location or "")
        if key in seen:
            continue
        seen.add(key)
        posted_date = _posted_date_from_element(link)
        jobs.append({
            "job_id": None,
            "title": title,
            "location": _clean_location_field(location),
            "dept": current_dept,
            "posted_date": posted_date,
            "url": job_url,
        })
    return jobs


def extract_jobs_from_landing(html: str, base_url: str = "https://www.hellolanding.com") -> list[dict[str, Any]]:
    """Legacy stub. Jobs are in applytojob iframe; use fetch from applytojob URL + extract_jobs_from_applytojob."""
    return []


def extract_jobs_from_blueground_page_data(html: str) -> list[dict[str, Any]]:
    """Extract jobs from Blueground careers page when job list is in embedded JSON: Blueground.pageData = {"jobs":[...]}.
    Clean interface like Placemakr/Lever - no Playwright needed."""
    match = re.search(r"Blueground\.pageData\s*=\s*(\{)", html)
    if not match:
        return []
    start = match.start(1)
    depth = 0
    for i in range(start, min(start + 500000, len(html))):
        if html[i] == "{":
            depth += 1
        elif html[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    data = json.loads(html[start : i + 1])
                except json.JSONDecodeError:
                    return []
                raw_jobs = data.get("jobs") or []
                jobs = []
                for j in raw_jobs:
                    if not isinstance(j, dict):
                        continue
                    title = (j.get("title") or j.get("full_title") or "").strip()
                    if not title or len(title) < 2:
                        continue
                    loc_obj = j.get("location")
                    if isinstance(loc_obj, dict):
                        location = (loc_obj.get("location_str") or "").strip() or None
                    else:
                        location = (loc_obj or "").strip() or None
                    url = (j.get("url") or j.get("application_url") or "").strip() or None
                    dept = (j.get("department") or "").strip() or None
                    created = j.get("created_at")
                    posted_date = created if isinstance(created, str) else None
                    job_id = j.get("id") or j.get("shortcode")
                    jobs.append({
                        "job_id": str(job_id) if job_id is not None else None,
                        "title": title,
                        "location": _clean_location_field(location),
                        "dept": dept,
                        "posted_date": posted_date,
                        "url": url or None,
                    })
                return jobs
    return []


def extract_jobs_from_blueground(html: str, base_url: str = "https://www.theblueground.com") -> list[dict[str, Any]]:
    """Extract jobs from Blueground careers page. Prefer embedded JSON (Blueground.pageData.jobs); fall back to HTML section/link scraping."""
    # 1) Embedded JSON (clean, no JS required) - same idea as Placemakr/Lever
    jobs = extract_jobs_from_blueground_page_data(html)
    if jobs:
        return jobs

    # 2) Fallback: scrape HTML section and links
    soup = BeautifulSoup(html, "html.parser")
    jobs = []
    seen = set()
    current_dept = None

    def _is_job_link(href: str) -> bool:
        if not href:
            return False
        h = href.lower()
        if "workable.com" in h or "/jobs/" in h:
            return True
        if "theblueground.com" in h and ("career" in h or "job" in h or "opening" in h or "role" in h):
            return True
        return False

    def _collect_from_section(section) -> None:
        nonlocal current_dept
        for elem in section.descendants:
            if not hasattr(elem, "name"):
                continue
            if elem.name == "div" and elem.get("class"):
                classes = elem.get("class")
                cls_str = " ".join(classes) if isinstance(classes, list) else str(classes)
                if "bg-col-xs-12" in cls_str:
                    dept_text = (elem.get_text() or "").strip()
                    if dept_text and len(dept_text) < 80 and not _is_cta_or_section_text(dept_text):
                        current_dept = dept_text
            if elem.name == "a" and elem.get("href"):
                href = elem.get("href") or ""
                if not _is_job_link(href):
                    continue
                title = elem.get("title") or (elem.get_text() or "").strip()
                if not title or len(title) < 4 or len(title) > 120:
                    continue
                if _is_cta_or_section_text(title):
                    continue
                job_url = href if href.startswith("http") else urljoin(base_url, href)
                location = None
                loc_span = elem.find_next("span", class_=lambda c: c and "job_location" in (c if isinstance(c, str) else " ".join(c)))
                if loc_span:
                    location = (loc_span.get_text() or "").strip() or None
                key = (title[:80], location or "", current_dept or "")
                if key in seen:
                    continue
                seen.add(key)
                posted_date = _posted_date_from_element(elem)
                jobs.append({
                    "job_id": None,
                    "title": title,
                    "location": _clean_location_field(location),
                    "dept": current_dept,
                    "posted_date": posted_date,
                    "url": job_url,
                })

    # 1) Prefer section with known layout classes
    section = soup.find(class_=lambda c: c and "u-vs__top--3" in (c if isinstance(c, str) else " ".join(c)) and "u-vs__bottom--4" in (c if isinstance(c, str) else " ".join(c)))
    if section:
        _collect_from_section(section)
        if jobs:
            return jobs

    # 2) Fallback: find section under "openings" / "open roles" heading
    current_dept = None
    seen.clear()
    for tag in soup.find_all(["h2", "h3", "h4", "h5"]):
        text = (tag.get_text() or "").strip().lower()
        if "open" not in text and "role" not in text:
            continue
        if "opening" in text or "open role" in text or "open roles" in text:
            parent = tag.find_parent(["section", "div"])
            if parent:
                _collect_from_section(parent)
                if jobs:
                    return jobs

    # 3) Last resort: whole body, any job-like link
    current_dept = None
    seen.clear()
    _collect_from_section(soup.body if soup.body else soup)
    return jobs


def extract_jobs_from_gem(html: str, source_url: str = "https://jobs.gem.com") -> list[dict[str, Any]]:
    """Extract jobs from Gem job board SPA (e.g. Rove). Links are like /rove/am9icG9zd... (base64 job id), not /j/."""
    soup = BeautifulSoup(html, "html.parser")
    jobs = []
    seen = set()
    # Base URL for resolving relative hrefs (e.g. /rove/xxx -> https://jobs.gem.com/rove/xxx)
    base = "https://jobs.gem.com"
    if source_url.startswith("http"):
        parts = source_url.split("/")
        if len(parts) >= 3:
            base = parts[0] + "//" + parts[2]
    # Board slug is last path segment of source (e.g. rove from https://jobs.gem.com/rove)
    board_slug = ""
    if "/" in source_url.rstrip("/"):
        board_slug = source_url.rstrip("/").split("/")[-1].lower()
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if not href or href == "#":
            continue
        # Gem uses paths like /rove/am9icG9zd... (board slug + base64 id) or /j/ shortcode
        href_lower = href.lower()
        is_job_path = (
            "/j/" in href_lower
            or "job" in href_lower
            or "position" in href_lower
            or (board_slug and f"/{board_slug}/" in href_lower and len(href) > len(board_slug) + 10)
        )
        if not is_job_path:
            continue
        raw_text = (a.get_text() or "").strip()
        # Gem often appends location on same line (e.g. "TitleCity•Hybrid" or "TitleCity1, City2, ...")
        if "\n" in raw_text:
            raw_text = raw_text.split("\n")[0].strip()
        title = raw_text
        location = None
        if "•" in raw_text:
            parts = raw_text.split("•", 1)
            title = parts[0].strip()
            location = (parts[1].strip() or None) if len(parts) > 1 else None
        # If title looks like "TitleCity, City2" (no space before city), take last run of comma-separated as location
        if len(title) > 100:
            title = title[:97] + "..."
        if not title or len(title) < 4:
            continue
        if _is_cta_or_section_text(title):
            continue
        job_url = href if href.startswith("http") else urljoin(base, href)
        key = (title[:80], job_url)
        if key in seen:
            continue
        seen.add(key)
        jobs.append({
            "job_id": None,
            "title": title,
            "location": _clean_location_field(location),
            "dept": None,
            "posted_date": None,
            "url": job_url,
        })
    return jobs


def _extract_jobs_from_generic_html(html: str, source_url: str) -> list[dict[str, Any]]:
    """Extract jobs from HTML using site-specific or generic extractor. Used for generic (non-API) career pages.
    Not used for WizeHire, which has its own fetch + scroll path."""
    if "kula.ai" in source_url.lower():
        jobs = extract_jobs_from_kula(html)
        return jobs if jobs else extract_jobs_from_html(html)
    if "hellolanding.com" in source_url.lower():
        jobs = extract_jobs_from_landing(html, base_url="https://www.hellolanding.com")
        return jobs if jobs else extract_jobs_from_html(html)
    if "theblueground.com" in source_url.lower():
        jobs = extract_jobs_from_blueground(html, base_url="https://www.theblueground.com")
        return jobs if jobs else extract_jobs_from_html(html)
    if "jobs.gem.com" in source_url.lower() or "gem.com" in source_url.lower():
        jobs = extract_jobs_from_gem(html, source_url=source_url)
        return jobs if jobs else extract_jobs_from_html(html)
    return extract_jobs_from_html(html)


def normalize_jobs(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = []
    for job in jobs:
        raw_loc = (job.get("location") or "").strip() or None
        normalized.append(
            {
                "job_id": job.get("job_id"),
                "title": (job.get("title") or "").strip(),
                "location": _clean_location_field(raw_loc),
                "dept": (job.get("dept") or "").strip() or None,
                "posted_date": job.get("posted_date"),
                "url": job.get("url"),
            }
        )
    return normalized


def collect_talent_snapshot(source_url: str) -> dict[str, Any]:
    """Pull all current jobs for a talent source. Priority: Lever API > Greenhouse API > Ashby API > generic HTML.
    Every run persists the full job list so we can diff later (surges, new executive postings).

    Plain fetch (no Playwright): Placemakr (Lever), Vacasa (Greenhouse), Blueground (embedded pageData),
    Landing (applytojob iframe). Playwright/scroll needed: Lark (WizeHire), AvantStay (Kula), Rove (Gem)."""
    provider = detect_provider(source_url)

    # 1. Lever
    if provider == "lever":
        api_url = lever_jobs_api(source_url)
        if api_url:
            try:
                try:
                    fetched = fetch_url(api_url, timeout=45)
                except requests.exceptions.Timeout:
                    fetched = fetch_url(api_url, timeout=60)
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
                fetched = fetch_url(api_url, timeout=45)
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
                fetched = fetch_url(api_url, timeout=45)
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

    # 4. Generic: competitor career page (HTML scrape).
    # Landing: jobs are in JazzHR/applytojob iframe; fetch that URL directly.
    if "hellolanding.com" in source_url.lower() and "/careers" in source_url.lower():
        try:
            iframe_url = "https://landing.applytojob.com/apply/jobs/"
            fetched = fetch_url(iframe_url)
            if fetched.status_code == 200:
                jobs = extract_jobs_from_applytojob(fetched.text, base_url="https://landing.applytojob.com")
                return {
                    "provider": "generic",
                    "source_url": source_url,
                    "raw_content": fetched.text,
                    "raw_hash": fetched.raw_hash,
                    "jobs": normalize_jobs(jobs),
                }
        except Exception:
            pass

    # Gem (e.g. Rove): SPA with no public API; use Playwright with short wait for client-side render.
    if "jobs.gem.com" in source_url.lower():
        try:
            fetched = fetch_url_js_wait_for_spa(source_url, wait_after_load_sec=8.0)
        except (RuntimeError, Exception):
            fetched = fetch_url(source_url)
        jobs = _extract_jobs_from_generic_html(fetched.text, source_url)
        return {
            "provider": "generic",
            "source_url": fetched.url,
            "raw_content": fetched.text,
            "raw_hash": fetched.raw_hash,
            "jobs": normalize_jobs(jobs),
        }

    # WizeHire (Lark): JS + scroll exhaust only — keep as-is, do not change.
    if "wizehire.com" in source_url.lower():
        try:
            scroll_options = {
                "scroll_window": True,
                "scroll_by_viewport": True,
                "max_scrolls": 100,
                "scroll_wait_sec": 1.0,
                "scroll_batch_wait_sec": 4.0,
                "scroll_no_progress_limit": 5,
                "scroll_job_count_selector": 'div[class*="jss83"]',
                "post_load_wait_ms": 3000,
            }
            fetched = fetch_url_js_exhaust(source_url, scroll_options)
        except (RuntimeError, Exception):
            fetched = fetch_url(source_url)
        jobs = extract_jobs_from_wizehire_title_divs(fetched.text)
        if not jobs:
            jobs = extract_jobs_from_html(fetched.text)
        if not jobs:
            jobs = extract_jobs_from_headings(fetched.text)
        return {
            "provider": "generic",
            "source_url": fetched.url,
            "raw_content": fetched.text,
            "raw_hash": fetched.raw_hash,
            "jobs": normalize_jobs(jobs),
        }

    # All other generic sites: try HTML first; if 0 or very few jobs, retry with JS.
    _MIN_JOBS_JS_FALLBACK = 3
    fetched = fetch_url(source_url)
    jobs = _extract_jobs_from_generic_html(fetched.text, source_url)
    playwright_fallback = False
    if len(jobs) < _MIN_JOBS_JS_FALLBACK:
        try:
            fetched_js = fetch_url_js(source_url)
            jobs_js = _extract_jobs_from_generic_html(fetched_js.text, source_url)
            if len(jobs_js) > len(jobs):
                fetched = fetched_js
                jobs = jobs_js
        except (RuntimeError, Exception):
            playwright_fallback = True
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
