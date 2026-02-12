from datetime import datetime, timezone
from typing import Optional


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)

from sqlalchemy import (
    String,
    DateTime,
    ForeignKey,
    Text,
    JSON,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


class Competitor(Base):
    __tablename__ = "competitors"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    primary_domain: Mapped[Optional[str]] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_now, nullable=False)
    # Optional manual baseline for reporting (seed run). When set, dossier
    # summaries and timelines treat this as the comparison start date so that
    # only events/press after this point are considered "new".
    reporting_baseline_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # When False, excluded from cron/global refresh. Default True for new competitors.
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)

    source_endpoints: Mapped[list["SourceEndpoint"]] = relationship(
        back_populates="competitor",
        cascade="all, delete-orphan",
    )
    review_properties: Mapped[list["CompetitorReviewProperty"]] = relationship(
        back_populates="competitor",
        cascade="all, delete-orphan",
    )


class SourceEndpoint(Base):
    __tablename__ = "source_endpoints"

    id: Mapped[int] = mapped_column(primary_key=True)
    competitor_id: Mapped[int] = mapped_column(ForeignKey("competitors.id"), nullable=False)
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    url: Mapped[str] = mapped_column(String(1024), nullable=False)
    confidence: Mapped[str] = mapped_column(String(16), default="high", nullable=False)
    js_required: Mapped[bool] = mapped_column(default=False)
    use_sitemap_first: Mapped[bool] = mapped_column(default=False)
    extra_options: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_now, nullable=False)

    competitor: Mapped[Competitor] = relationship(back_populates="source_endpoints")


class Snapshot(Base):
    __tablename__ = "snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    competitor_id: Mapped[int] = mapped_column(ForeignKey("competitors.id"), nullable=False)
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    raw_content: Mapped[Optional[str]] = mapped_column(Text)
    raw_hash: Mapped[Optional[str]] = mapped_column(String(128))
    structured_json: Mapped[Optional[dict]] = mapped_column(JSON)
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_now, nullable=False)


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    competitor_id: Mapped[int] = mapped_column(ForeignKey("competitors.id"), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(8), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    why_it_matters: Mapped[Optional[str]] = mapped_column(Text)
    evidence_json: Mapped[Optional[dict]] = mapped_column(JSON)
    occurred_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    detected_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_now, nullable=False)


class Capability(Base):
    __tablename__ = "capabilities"

    id: Mapped[int] = mapped_column(primary_key=True)
    competitor_id: Mapped[int] = mapped_column(ForeignKey("competitors.id"), nullable=False)
    capability: Mapped[str] = mapped_column(String(64), nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_now, nullable=False)


class CompetitorReviewProperty(Base):
    """User-tracked properties for Google Reviews per competitor. Add/remove via UI."""
    __tablename__ = "competitor_review_properties"

    id: Mapped[int] = mapped_column(primary_key=True)
    competitor_id: Mapped[int] = mapped_column(ForeignKey("competitors.id"), nullable=False)
    place_id: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[Optional[str]] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_now, nullable=False)

    competitor: Mapped["Competitor"] = relationship(back_populates="review_properties")


class RunLog(Base):
    __tablename__ = "run_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    competitor_id: Mapped[Optional[int]] = mapped_column(ForeignKey("competitors.id"))
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    message: Mapped[Optional[str]] = mapped_column(Text)
    extra_json: Mapped[Optional[dict]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_now, nullable=False)
