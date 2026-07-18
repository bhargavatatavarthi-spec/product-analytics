"""In-process background import jobs with progress tracking.

Uploads return immediately with a job id; the file is ingested on a daemon
thread while the UI polls for a 0–100 percentage. A lock serialises the actual
DB writes (SQLite is single-writer), so several queued uploads process one after
another without conflicting.
"""
from __future__ import annotations

import threading
import uuid
from datetime import date

from . import ingest
from .db import SessionLocal

_JOBS: dict[str, dict] = {}
_STORE_LOCK = threading.Lock()
_WRITE_LOCK = threading.Lock()  # one import writes to SQLite at a time
_MAX_JOBS = 50


def _set(job_id: str, **kw) -> None:
    with _STORE_LOCK:
        if job_id in _JOBS:
            _JOBS[job_id].update(kw)


def start_import(
    raw: bytes,
    filename: str,
    mapping: dict | None,
    drop_date: date | None,
    default_stage: str | None = None,
) -> str:
    job_id = uuid.uuid4().hex[:12]
    total = max(0, raw.count(b"\n") - 1)  # rows ≈ newlines minus header
    with _STORE_LOCK:
        # Trim old jobs so the store doesn't grow unbounded.
        if len(_JOBS) >= _MAX_JOBS:
            for stale in list(_JOBS)[: len(_JOBS) - _MAX_JOBS + 1]:
                _JOBS.pop(stale, None)
        _JOBS[job_id] = {
            "status": "queued", "processed": 0, "total": total,
            "filename": filename, "result": None, "error": None,
        }
    threading.Thread(
        target=_run, args=(job_id, raw, filename, mapping, drop_date, default_stage), daemon=True
    ).start()
    return job_id


def _run(job_id, raw, filename, mapping, drop_date, default_stage) -> None:
    def cb(processed: int) -> None:
        _set(job_id, processed=processed)

    with _WRITE_LOCK:
        _set(job_id, status="running")
        db = SessionLocal()
        try:
            result = ingest.ingest_drop(
                db, raw, filename=filename, mapping=mapping, drop_date=drop_date,
                default_stage=default_stage, progress_cb=cb,
            )
            _set(job_id, status="done", result=result, processed=result.get("total_rows", 0))
        except Exception as exc:  # noqa: BLE001 - surface any failure to the client
            db.rollback()
            _set(job_id, status="error", error=str(exc))
        finally:
            db.close()


def get_status(job_id: str) -> dict | None:
    with _STORE_LOCK:
        j = _JOBS.get(job_id)
        if not j:
            return None
        total = j["total"] or 1
        if j["status"] == "done":
            percent = 100
        elif j["status"] == "running":
            percent = min(99, round(j["processed"] / total * 100))
        else:
            percent = 0
        return {
            "status": j["status"],
            "percent": percent,
            "filename": j["filename"],
            "result": j["result"],
            "error": j["error"],
        }
