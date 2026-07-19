"""Database models.

The import pipeline is snapshot-based: clients deliver a *daily drop* — a file
listing every live lead and its current sub-stage on that date. We store each
drop's metadata (`DailyDrop`), the reconstructed per-lead state (`Lead`), and
the sequence of distinct stage observations per lead (`StageEvent`). Analytics
are computed from these three tables plus the per-stage classification overrides
(`StageClassification`) and global `Setting`s.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DailyDrop(Base):
    """One imported client file. A single calendar date may receive several
    files (e.g. the journey feed and the offer feed), so each file is its own
    ledger row — keyed by (drop_date, filename) — rather than one row per date.
    Data-Health completeness still counts distinct dates present."""

    __tablename__ = "daily_drops"

    id: Mapped[int] = mapped_column(primary_key=True)
    drop_date: Mapped[date] = mapped_column(Date, index=True)
    filename: Mapped[str] = mapped_column(String(512), default="")
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    # "received" (a full drop) or "partial" (some rows failed to parse).
    status: Mapped[str] = mapped_column(String(16), default="received")
    error_rows: Mapped[int] = mapped_column(Integer, default=0)
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)

    __table_args__ = (UniqueConstraint("drop_date", "filename", name="uq_drop_date_file"),)


class Lead(Base):
    """Reconstructed current state of a single lead across all drops seen."""

    __tablename__ = "leads"

    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    # entry_date drives range filters (falls back to the drop date when the feed
    # has no Created Date). created_on is the *real* Created Date only — null when
    # absent — so cohorts never invent an entry day for undated leads.
    entry_date: Mapped[date | None] = mapped_column(Date, index=True)
    created_on: Mapped[date | None] = mapped_column(Date, index=True)
    current_stage: Mapped[str] = mapped_column(String(128), index=True)
    # When the lead first entered its *current* stage (drives days-in-stage/aging).
    stage_entered_on: Mapped[date | None] = mapped_column(Date)
    first_seen_on: Mapped[date | None] = mapped_column(Date)
    last_seen_on: Mapped[date | None] = mapped_column(Date, index=True)

    # Offer metadata (latest non-null wins) — from the offer feed.
    max_loan_amount: Mapped[float | None] = mapped_column(Float)
    max_tenure_months: Mapped[float | None] = mapped_column(Float)
    roi: Mapped[float | None] = mapped_column(Float)
    emi: Mapped[float | None] = mapped_column(Float)
    processing_fee: Mapped[float | None] = mapped_column(Float)
    schemecode: Mapped[str | None] = mapped_column(String(64))
    # Disbursed value — from the journey feed (DIS VALUE).
    disbursed_amount: Mapped[float | None] = mapped_column(Float)

    # Explicit milestone dates (when the feed provides them) — power true cohort
    # curves: reach_day = milestone_date − entry_date. Latest non-null wins.
    offer_generated_on: Mapped[date | None] = mapped_column(Date)
    offer_selected_on: Mapped[date | None] = mapped_column(Date)
    aa_initiated_on: Mapped[date | None] = mapped_column(Date)
    disbursement_on: Mapped[date | None] = mapped_column(Date)

    # Voice-AI attribution.
    voice_connected: Mapped[bool] = mapped_column(Boolean, default=False)
    call_count: Mapped[int] = mapped_column(Integer, default=0)
    last_disposition: Mapped[str | None] = mapped_column(String(128))

    # Data-quality: number of source cells that were "#N/A"/blank for this lead.
    na_cells: Mapped[int] = mapped_column(Integer, default=0)
    had_backward_move: Mapped[bool] = mapped_column(Boolean, default=False)

    events: Mapped[list["StageEvent"]] = relationship(
        back_populates="lead", cascade="all, delete-orphan"
    )


class StageEvent(Base):
    """A distinct stage observation for a lead (consecutive duplicates collapsed)."""

    __tablename__ = "stage_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    lead_pk: Mapped[int] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"), index=True)
    stage: Mapped[str] = mapped_column(String(128))
    observed_on: Mapped[date] = mapped_column(Date, index=True)

    lead: Mapped["Lead"] = relationship(back_populates="events")


class StageClassification(Base):
    """Analyst override of a stage's bucket (Won/In-flight/Lost/Unclassified)."""

    __tablename__ = "stage_classifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    stage: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    bucket: Mapped[str] = mapped_column(String(16))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)


class Setting(Base):
    """Global key/value settings (aging threshold, default milestone)."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(256))
