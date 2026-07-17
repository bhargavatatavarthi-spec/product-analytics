"""Manual data-import endpoints: preview, commit, list and reset."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .. import ingest
from ..db import get_db
from ..models import DailyDrop, Lead, StageEvent

router = APIRouter(prefix="/api/import", tags=["import"])

MAX_BYTES = 25 * 1024 * 1024  # 25 MB per upload


def _parse_mapping(mapping_json: str | None) -> dict | None:
    if not mapping_json:
        return None
    try:
        data = json.loads(mapping_json)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="mapping must be valid JSON")


async def _read(file: UploadFile) -> bytes:
    raw = await file.read()
    if len(raw) > MAX_BYTES:
        raise HTTPException(status_code=413, detail="File exceeds 25 MB limit")
    if not raw:
        raise HTTPException(status_code=400, detail="Empty file")
    return raw


@router.post("/preview")
async def preview(file: UploadFile = File(...), mapping: str | None = Form(None)):
    """Auto-detect columns and return a sample so the user can confirm the mapping
    before committing. Nothing is written to the database here."""
    raw = await _read(file)
    try:
        result = ingest.build_preview(raw, _parse_mapping(mapping))
    except UnicodeError:
        raise HTTPException(status_code=400, detail="Could not decode file as text/CSV")
    result["filename"] = file.filename
    return result


@router.post("/commit")
async def commit(
    file: UploadFile = File(...),
    mapping: str | None = Form(None),
    drop_date: str | None = Form(None),
    db: Session = Depends(get_db),
):
    """Ingest the uploaded daily drop into the reconstructed lead journeys."""
    raw = await _read(file)
    parsed_date = ingest.coerce_date(drop_date) if drop_date else None
    try:
        summary = ingest.ingest_drop(
            db, raw, filename=file.filename or "", mapping=_parse_mapping(mapping), drop_date=parsed_date
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return summary


@router.get("/drops")
def list_drops(db: Session = Depends(get_db)):
    """History of imported daily drops."""
    drops = db.execute(select(DailyDrop).order_by(DailyDrop.drop_date.desc())).scalars()
    total_leads = db.execute(select(func.count(Lead.id))).scalar_one()
    return {
        "total_leads": total_leads,
        "drops": [
            {
                "drop_date": d.drop_date.isoformat(),
                "filename": d.filename,
                "row_count": d.row_count,
                "error_rows": d.error_rows,
                "status": d.status,
                "imported_at": d.imported_at.isoformat() if d.imported_at else None,
            }
            for d in drops
        ],
    }


@router.delete("/reset")
def reset(db: Session = Depends(get_db)):
    """Wipe all imported data (leads, events, drops). Classifications/settings kept."""
    db.execute(delete(StageEvent))
    db.execute(delete(Lead))
    db.execute(delete(DailyDrop))
    db.commit()
    return {"ok": True}
