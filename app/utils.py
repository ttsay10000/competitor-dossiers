"""Shared utilities."""
from datetime import datetime
from typing import Any, Optional
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")


def to_eastern(dt: Optional[datetime], fmt: str = "%Y-%m-%d %H:%M") -> str:
    """
    Convert a datetime (stored as UTC, may be naive) to Eastern Time and format.
    Returns empty string if dt is None.
    """
    if dt is None:
        return ""
    # SQLAlchemy may return naive datetimes assumed UTC
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    et = dt.astimezone(ET)
    return et.strftime(fmt) + " ET"


def parse_url_context(url: str) -> dict[str, Any]:
    """
    Parse URL into subdomain, path, and host for LLM/display context.
    E.g. https://blog.example.com/locations -> subdomain=blog, path=/locations, host=blog.example.com
    """
    if not (url or "").strip():
        return {"subdomain": "", "path": "", "host": ""}
    parsed = urlparse((url or "").strip())
    host = (parsed.netloc or "").strip().lower()
    path = (parsed.path or "/").strip() or "/"
    parts = host.split(".")
    subdomain = (parts[0] or "") if len(parts) > 2 else ""
    return {"subdomain": subdomain, "path": path, "host": host}
