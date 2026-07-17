"""Manual data-import pipeline.

Turns a client *daily drop* CSV into reconstructed lead journeys. The flow the
UI exposes is: upload -> preview (auto-detected column mapping + sample) ->
confirm -> ingest. This module is intentionally forgiving about header names,
number formatting (₹, commas, %), and blank/#N/A cells, because real client
files are messy.
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime

from dateutil import parser as dateparser
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import catalog
from .models import DailyDrop, Lead, StageEvent

# Canonical field -> accepted header aliases (matched case/space/punct-insensitively).
FIELD_ALIASES: dict[str, list[str]] = {
    "lead_id": ["lead_id", "leadid", "lead", "id", "customer_id", "applicant_id", "loan_id"],
    "drop_date": ["drop_date", "snapshot_date", "as_of_date", "file_date", "report_date", "date"],
    "entry_date": ["entry_date", "created_at", "created_date", "lead_date", "onboarded_on", "start_date"],
    "stage": ["stage", "current_stage", "sub_stage", "substage", "journey_stage", "status", "state"],
    "disbursed_amount": ["disbursed_amount", "disbursal_amount", "disbursed", "loan_disbursed", "amount_disbursed"],
    "max_loan_amount": ["max_loan_amount", "max_loan", "loan_amount", "sanctioned_amount", "offer_amount"],
    "max_tenure_months": ["max_tenure_months", "max_tenure", "tenure_months", "tenure"],
    "roi": ["roi", "rate_of_interest", "interest_rate", "rate"],
    "schemecode": ["schemecode", "scheme_code", "scheme", "product_code", "product"],
    "voice_connected": ["voice_connected", "connected", "ai_connected", "is_connected"],
    "call_count": ["call_count", "calls", "num_calls", "attempts", "dial_count"],
    "last_disposition": ["last_disposition", "disposition", "call_disposition", "outcome", "call_outcome"],
}

REQUIRED_FIELDS = ("lead_id", "stage")
# Tokens treated as "no value" during coercion (a blank optional field is fine).
NA_TOKENS = {"", "#n/a", "n/a", "na", "null", "none", "-", "nan", "#value!", "#ref!"}
# Stricter set: genuine data-quality errors worth flagging (blank is NOT an error).
ERROR_TOKENS = {"#n/a", "n/a", "na", "null", "none", "nan", "#value!", "#ref!"}


def _norm(header: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (header or "").lower())


def suggest_mapping(headers: list[str]) -> dict[str, str | None]:
    """Map each canonical field to the best-matching source header (or None)."""
    norm_to_original = {_norm(h): h for h in headers}
    mapping: dict[str, str | None] = {}
    for field_name, aliases in FIELD_ALIASES.items():
        match = None
        for alias in aliases:
            if _norm(alias) in norm_to_original:
                match = norm_to_original[_norm(alias)]
                break
        mapping[field_name] = match
    return mapping


def parse_csv(raw: bytes) -> tuple[list[str], list[dict[str, str]]]:
    """Decode + parse a CSV into (headers, list-of-row-dicts)."""
    text = raw.decode("utf-8-sig", errors="replace")
    # Sniff the delimiter; fall back to comma.
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    headers = [h for h in (reader.fieldnames or []) if h is not None]
    rows = [dict(r) for r in reader]
    return headers, rows


def is_na(value: str | None) -> bool:
    return value is None or value.strip().lower() in NA_TOKENS


def is_error_token(value: str | None) -> bool:
    """A literal data-quality error (#N/A, #VALUE!, ...) — not merely blank."""
    return value is not None and value.strip().lower() in ERROR_TOKENS


def coerce_float(value: str | None) -> float | None:
    if is_na(value):
        return None
    cleaned = re.sub(r"[^0-9.\-]", "", value)  # strip ₹, commas, %, spaces
    if cleaned in ("", "-", "."):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def coerce_int(value: str | None) -> int | None:
    f = coerce_float(value)
    return int(f) if f is not None else None


def coerce_bool(value: str | None) -> bool:
    if is_na(value):
        return False
    return value.strip().lower() in ("1", "true", "yes", "y", "connected", "t")


def coerce_date(value: str | None) -> date | None:
    if is_na(value):
        return None
    v = value.strip()
    # Prefer unambiguous ISO 8601 (YYYY-MM-DD[...]) — using dayfirst here would
    # wrongly swap month/day (e.g. 2026-07-12 -> 2026-12-07).
    try:
        return date.fromisoformat(v[:10])
    except ValueError:
        pass
    # Fall back to day-first parsing for Indian-style DD-MM-YYYY / DD/MM/YYYY.
    try:
        return dateparser.parse(v, dayfirst=True).date()
    except (ValueError, OverflowError, TypeError):
        return None


@dataclass
class RowResult:
    ok: bool
    error: str | None = None
    values: dict = field(default_factory=dict)


def extract_row(row: dict[str, str], mapping: dict[str, str | None]) -> RowResult:
    """Pull canonical values out of one raw row using the column mapping."""

    def raw(field_name: str) -> str | None:
        col = mapping.get(field_name)
        return row.get(col) if col else None

    lead_id = raw("lead_id")
    if is_na(lead_id):
        return RowResult(ok=False, error="missing lead_id")
    stage = raw("stage")
    if is_na(stage):
        return RowResult(ok=False, error="missing stage")

    na_cells = sum(1 for f in FIELD_ALIASES if mapping.get(f) and is_error_token(raw(f)))
    values = {
        "lead_id": lead_id.strip(),
        "stage": stage.strip(),
        "entry_date": coerce_date(raw("entry_date")),
        "drop_date": coerce_date(raw("drop_date")),
        "disbursed_amount": coerce_float(raw("disbursed_amount")),
        "max_loan_amount": coerce_float(raw("max_loan_amount")),
        "max_tenure_months": coerce_float(raw("max_tenure_months")),
        "roi": coerce_float(raw("roi")),
        "schemecode": None if is_na(raw("schemecode")) else raw("schemecode").strip(),
        "voice_connected": coerce_bool(raw("voice_connected")),
        "call_count": coerce_int(raw("call_count")) or 0,
        "last_disposition": None if is_na(raw("last_disposition")) else raw("last_disposition").strip(),
        "na_cells": na_cells,
    }
    return RowResult(ok=True, values=values)


def build_preview(raw: bytes, mapping: dict[str, str | None] | None = None, limit: int = 8) -> dict:
    """Parse just enough to show the user a mapping + sample before committing."""
    headers, rows = parse_csv(raw)
    suggested = suggest_mapping(headers)
    if mapping:
        # Caller-supplied overrides win, but keep suggestions for unset fields.
        merged = dict(suggested)
        merged.update({k: v for k, v in mapping.items() if v})
        mapping = merged
    else:
        mapping = suggested

    missing_required = [f for f in REQUIRED_FIELDS if not mapping.get(f)]
    sample: list[dict] = []
    ok_count = 0
    for r in rows:
        res = extract_row(r, mapping)
        if res.ok:
            ok_count += 1
            if len(sample) < limit:
                sample.append(res.values)
    return {
        "headers": headers,
        "mapping": mapping,
        "fields": list(FIELD_ALIASES.keys()),
        "required": list(REQUIRED_FIELDS),
        "missing_required": missing_required,
        "total_rows": len(rows),
        "valid_rows": ok_count,
        "invalid_rows": len(rows) - ok_count,
        "sample": sample,
    }


def _resolve_drop_date(rows_values: list[dict], filename: str, override: date | None) -> date:
    """Determine the drop's calendar date: explicit override > per-row drop_date
    > a YYYY-MM-DD / DD-MM-YYYY found in the filename > today."""
    if override:
        return override
    for v in rows_values:
        if v.get("drop_date"):
            return v["drop_date"]
    m = re.search(r"(\d{4}[-_]\d{2}[-_]\d{2})", filename or "")
    if m:
        d = coerce_date(m.group(1).replace("_", "-"))
        if d:
            return d
    m = re.search(r"(\d{2}[-_]\d{2}[-_]\d{4})", filename or "")
    if m:
        d = coerce_date(m.group(1).replace("_", "-"))
        if d:
            return d
    return datetime.now().date()


def ingest_drop(
    db: Session,
    raw: bytes,
    filename: str = "",
    mapping: dict[str, str | None] | None = None,
    drop_date: date | None = None,
) -> dict:
    """Parse a CSV and fold it into the reconstructed lead journeys.

    Idempotent per drop_date: re-importing the same date replaces that drop's
    row-count metadata and re-applies its observations (existing leads are
    updated, not duplicated).
    """
    headers, rows = parse_csv(raw)
    resolved_mapping = suggest_mapping(headers)
    if mapping:
        resolved_mapping.update({k: v for k, v in mapping.items() if v})

    missing_required = [f for f in REQUIRED_FIELDS if not resolved_mapping.get(f)]
    if missing_required:
        raise ValueError(f"Cannot import: missing required column(s): {', '.join(missing_required)}")

    parsed: list[dict] = []
    error_rows = 0
    for r in rows:
        res = extract_row(r, resolved_mapping)
        if res.ok:
            parsed.append(res.values)
        else:
            error_rows += 1

    the_date = _resolve_drop_date(parsed, filename, drop_date)

    # Upsert the DailyDrop record.
    drop = db.execute(select(DailyDrop).where(DailyDrop.drop_date == the_date)).scalar_one_or_none()
    if drop is None:
        drop = DailyDrop(drop_date=the_date)
        db.add(drop)
    drop.filename = filename or drop.filename
    drop.row_count = len(parsed)
    drop.error_rows = error_rows
    drop.status = "partial" if error_rows else "received"

    # Cache existing leads referenced in this drop for fast upsert.
    lead_ids = {v["lead_id"] for v in parsed}
    existing = {
        lead.lead_id: lead
        for lead in db.execute(select(Lead).where(Lead.lead_id.in_(lead_ids))).scalars()
    } if lead_ids else {}

    new_leads = 0
    updated_leads = 0
    new_stages: set[str] = set()

    for v in parsed:
        lead = existing.get(v["lead_id"])
        stage = v["stage"]
        if stage not in catalog.STAGE_ORDER:
            new_stages.add(stage)

        if lead is None:
            lead = Lead(
                lead_id=v["lead_id"],
                current_stage=stage,
                entry_date=v["entry_date"] or the_date,
                stage_entered_on=the_date,
                first_seen_on=the_date,
                last_seen_on=the_date,
                max_loan_amount=v["max_loan_amount"],
                max_tenure_months=v["max_tenure_months"],
                roi=v["roi"],
                schemecode=v["schemecode"],
                disbursed_amount=v["disbursed_amount"],
                voice_connected=v["voice_connected"],
                call_count=v["call_count"],
                last_disposition=v["last_disposition"],
                na_cells=v["na_cells"],
            )
            db.add(lead)
            db.flush()
            db.add(StageEvent(lead_pk=lead.id, stage=stage, observed_on=the_date))
            existing[v["lead_id"]] = lead
            new_leads += 1
            continue

        updated_leads += 1
        # Timeline bookkeeping.
        lead.first_seen_on = min(lead.first_seen_on or the_date, the_date)
        if v["entry_date"]:
            lead.entry_date = min(lead.entry_date or v["entry_date"], v["entry_date"])
        lead.na_cells += v["na_cells"]

        # Latest non-null metadata wins (only when this drop is the newest we've seen).
        is_latest = the_date >= (lead.last_seen_on or the_date)
        for fld in ("max_loan_amount", "max_tenure_months", "roi", "schemecode",
                    "disbursed_amount", "last_disposition"):
            if v[fld] is not None and (is_latest or getattr(lead, fld) is None):
                setattr(lead, fld, v[fld])
        lead.voice_connected = lead.voice_connected or v["voice_connected"]
        lead.call_count = max(lead.call_count, v["call_count"])

        # Stage transition handling.
        if stage != lead.current_stage:
            db.add(StageEvent(lead_pk=lead.id, stage=stage, observed_on=the_date))
            prev_order = catalog.STAGE_ORDER.get(lead.current_stage)
            new_order = catalog.STAGE_ORDER.get(stage)
            if prev_order is not None and new_order is not None and new_order < prev_order:
                lead.had_backward_move = True
            if is_latest:
                lead.current_stage = stage
                lead.stage_entered_on = the_date

        if is_latest:
            lead.last_seen_on = the_date

    db.commit()

    return {
        "drop_date": the_date.isoformat(),
        "filename": filename,
        "status": drop.status,
        "total_rows": len(rows),
        "imported_rows": len(parsed),
        "error_rows": error_rows,
        "new_leads": new_leads,
        "updated_leads": updated_leads,
        "new_unmapped_stages": sorted(new_stages),
        "mapping": resolved_mapping,
    }
