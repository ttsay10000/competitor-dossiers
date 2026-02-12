"""Shared utilities."""
from datetime import datetime
from typing import Optional
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
