"""Analytics, metadata, stage-classification and settings endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends
from sqlalchemy.orm import Session

from .. import analytics, catalog
from ..db import get_db
from ..models import Setting

router = APIRouter(prefix="/api", tags=["analytics"])


@router.get("/meta")
def meta(db: Session = Depends(get_db)):
    """Static-ish reference data plus per-range summaries for the header/anchor."""
    return {
        "ranges": catalog.RANGES,
        "milestones": catalog.MILESTONES,
        "dimensions": [
            {"key": d["key"], "label": d["label"], "field": d["field"]}
            for d in catalog.ATTR_DIMENSIONS
        ],
        "buckets": [
            {"key": "won", "label": "Won", "color": "#6F39F5"},
            {"key": "inflight", "label": "In-flight", "color": "#191132"},
            {"key": "lost", "label": "Lost", "color": "#8A8595"},
            {"key": "unclassified", "label": "Unclassified", "color": "#6F39F5"},
        ],
        "settings": analytics.get_settings(db),
        "summaries": analytics.range_summaries(db),
        "has_data": analytics.has_data(db),
    }


@router.get("/overview")
def overview(range: str = "30d", db: Session = Depends(get_db)):
    return analytics.overview(db, range)


@router.get("/cohort")
def cohort(milestone: str | None = None, db: Session = Depends(get_db)):
    if not milestone:
        milestone = analytics.get_settings(db)["default_milestone"]
    return analytics.cohort(db, milestone)


@router.get("/attribution")
def attribution(range: str = "30d", dim: str = "amount", db: Session = Depends(get_db)):
    return analytics.attribution(db, range, dim)


@router.get("/settings")
def get_settings(db: Session = Depends(get_db)):
    return analytics.get_settings(db)


@router.post("/settings")
def update_settings(payload: dict = Body(...), db: Session = Depends(get_db)):
    allowed = set(analytics.DEFAULT_SETTINGS.keys())
    for key, value in payload.items():
        if key not in allowed:
            continue
        existing = db.get(Setting, key)
        if existing:
            existing.value = str(value)
        else:
            db.add(Setting(key=key, value=str(value)))
    db.commit()
    return analytics.get_settings(db)
