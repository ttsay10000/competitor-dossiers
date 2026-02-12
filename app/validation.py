"""
URL validation and suggestions for Add Competitor flow.
- Format: require valid http(s) URL with netloc.
- Reachability: optional HEAD request (short timeout).
- Suggestions: build talent/asset/press URLs from primary domain.
"""
from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urlparse

# Optional reachability check (avoid adding requests to every save)
try:
    import requests
except ImportError:
    requests = None

# Allowed schemes for source URLs
ALLOWED_SCHEMES = ("http", "https")

# Common path suggestions per channel (without leading slash; we add it)
SUGGEST_TALENT_PATHS = ["careers", "jobs", "work-with-us", "join-us", "career", "opportunities"]
SUGGEST_ASSET_PATHS = ["locations", "portfolio", "properties", "our-properties", "find-a-home", "listings"]
SUGGEST_PRESS_PATHS = ["blog", "press", "news", "media", "press-room", "articles"]


def normalize_domain(raw: str) -> Optional[str]:
    """Return a clean domain (host only, lowercase) or None if empty/invalid."""
    s = (raw or "").strip().lower()
    if not s:
        return None
    # Allow user to paste "example.com" or "https://example.com/path"
    if "://" in s:
        parsed = urlparse(s)
        if parsed.netloc:
            return parsed.netloc.split(":")[0]
        return None
    # Remove path if they pasted "example.com/path"
    if "/" in s:
        s = s.split("/")[0]
    # Remove port for display
    if ":" in s:
        s = s.split(":")[0]
    return s if s else None


def validate_url_format(url: str) -> tuple[bool, Optional[str]]:
    """
    Validate that the string is a well-formed http(s) URL with a host.
    Returns (True, None) if valid, (False, "error message") if invalid.
    """
    s = (url or "").strip()
    if not s:
        return True, None  # empty is allowed (optional field)
    parsed = urlparse(s)
    if not parsed.scheme:
        return False, "URL must start with http:// or https://"
    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        return False, f"URL scheme must be http or https, not {parsed.scheme}"
    if not parsed.netloc:
        return False, "URL must include a host (e.g. example.com)"
    # Basic host: allow domain and optional port
    host = parsed.netloc.split(":")[0]
    if not host or len(host) < 2:
        return False, "URL host is invalid"
    # Reject obvious non-URLs
    if re.match(r"^[\d.]+$", host) and len(host) > 6:
        # Could be IP; allow it
        pass
    elif "." not in host and host != "localhost":
        return False, "URL host should look like a domain (e.g. example.com)"
    return True, None


def check_reachability(url: str, timeout: int = 8) -> tuple[bool, Optional[str]]:
    """
    Perform a HEAD request to check if the URL is reachable.
    Returns (True, None) if reachable, (False, "error message") otherwise.
    """
    s = (url or "").strip()
    if not s:
        return False, "No URL provided"
    ok, err = validate_url_format(s)
    if not ok:
        return False, err or "Invalid URL"
    if requests is None:
        return True, None  # skip check if requests not available
    try:
        resp = requests.head(
            s,
            timeout=timeout,
            allow_redirects=True,
            headers={"User-Agent": "competitor-signals/0.1"},
        )
        if resp.status_code < 400:
            return True, None
        return False, f"URL returned HTTP {resp.status_code}"
    except requests.exceptions.Timeout:
        return False, "Request timed out"
    except requests.exceptions.SSLError as e:
        return False, "SSL error (certificate problem)"
    except requests.exceptions.ConnectionError:
        return False, "Could not connect to host"
    except Exception as e:
        return False, str(e)[:80]


def suggest_urls_from_domain(domain: str) -> dict[str, list[str]]:
    """
    Build suggested talent/asset/press URLs from a primary domain.
    domain: e.g. "example.com" (no scheme).
    Returns {"talent": [...], "asset": [...], "press": [...]} with full https URLs.
    """
    base = normalize_domain(domain)
    if not base:
        return {"talent": [], "asset": [], "press": []}
    if not base.startswith("http"):
        base = "https://" + base
    else:
        base = base.rstrip("/")

    def build(paths: list[str]) -> list[str]:
        base_stripped = base.rstrip("/")
        return [f"{base_stripped}/{p}" for p in paths]

    return {
        "talent": build(SUGGEST_TALENT_PATHS),
        "asset": build(SUGGEST_ASSET_PATHS),
        "press": build(SUGGEST_PRESS_PATHS),
    }
