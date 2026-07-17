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
# Kotak PAL delivers two joined daily feeds, both keyed on offer_id:
#   • offer feed   : offerid, name, mobile, max_loan_amount, max_tenure_months,
#                    roi, EMI, processing_fee, schemecode
#   • journey feed : offer_id, last_call_outcome, Created Date (entry), DIY
#                    Sub-stage (stage), DIS VALUE (disbursed amount)
# Both map onto the same canonical fields below, so either file imports cleanly
# and rows merge on lead_id (= offer_id).
FIELD_ALIASES: dict[str, list[str]] = {
    "lead_id": ["internal_id", "offer_id", "offerid", "lead_id", "leadid", "lead", "id", "customer_id", "applicant_id", "loan_id"],
    "drop_date": ["drop_date", "snapshot_date", "as_of_date", "file_date", "report_date", "date"],
    "entry_date": ["created_date", "created date", "entry_date", "created_at", "created", "lead_date", "onboarded_on", "start_date"],
    "stage": ["diy_sub_stage", "diy_substage", "sub_stage", "substage", "stage", "current_stage", "journey_stage", "status", "state"],
    "disbursed_amount": ["disbursement_amount", "dis_value", "disvalue", "disbursement_value", "disbursed_amount", "disbursal_amount", "disbursed", "loan_disbursed", "amount_disbursed"],
    "max_loan_amount": ["max_loan_amount", "max_loan_amt", "max_loan", "loan_amount", "sanctioned_amount", "offer_amount"],
    "max_tenure_months": ["max_tenure_months", "max_tenure_month", "max_tenure", "tenure_months", "tenure"],
    "roi": ["roi", "rate_of_interest", "interest_rate", "rate"],
    "emi": ["emi", "emi_amount", "monthly_emi", "installment"],
    "processing_fee": ["processing_fee", "processing_fees", "proc_fee", "pf"],
    "schemecode": ["schemecode", "scheme_code", "scheme", "product_code", "product"],
    "voice_connected": ["connected_at_least_once", "voice_connected", "connected", "ai_connected", "is_connected"],
    "call_count": ["call_count", "calls", "num_calls", "attempts", "dial_count"],
    "last_disposition": ["last_call_outcome", "last_disposition", "disposition", "call_disposition", "outcome", "call_outcome"],
}

# Only lead_id is strictly required: the offer feed has no stage column, so a
# row with no stage still registers/updates the offer (default stage applied to
# brand-new leads).
REQUIRED_FIELDS = ("lead_id",)
DEFAULT_STAGE = "Offer Generated"

# Normalize incoming stage strings (e.g. "DISBURSEMENT_COMPLETED") to catalog names.
_STAGE_BY_NORM = {re.sub(r"[^a-z0-9]", "", s["name"].lower()): s["name"] for s in catalog.STAGE_CATALOG}


def normalize_stage(value: str) -> str:
    """Map a raw stage string to its catalog name; unknowns become Title Case."""
    key = re.sub(r"[^a-z0-9]", "", value.lower())
    if key in _STAGE_BY_NORM:
        return _STAGE_BY_NORM[key]
    return re.sub(r"[_\s]+", " ", value).strip().title()
# Tokens treated as "no value" during coercion. Clients (incl. Kotak) use
# "#N/A" pervasively as their null token, so it is a normal absence, not an error.
NA_TOKENS = {"", "#n/a", "n/a", "na", "null", "none", "-", "nan",
             "#value!", "#ref!", "#name?", "#div/0!", "#num!", "#null!"}
# Genuine spreadsheet cell errors worth flagging (blank / #N/A are NOT errors).
ERROR_TOKENS = {"#value!", "#ref!", "#name?", "#div/0!", "#num!", "#null!"}


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


_DMY_RE = re.compile(r"^\s*(\d{1,2})[-/](\d{1,2})[-/](\d{2,4})")


def coerce_date(value: str | None, dayfirst: bool = True) -> date | None:
    if is_na(value):
        return None
    v = value.strip()
    # Prefer unambiguous ISO 8601 (YYYY-MM-DD[...]) — using dayfirst here would
    # wrongly swap month/day (e.g. 2026-07-12 -> 2026-12-07).
    try:
        return date.fromisoformat(v[:10])
    except ValueError:
        pass
    # Slash/dash dates: honour the detected format (Indian DD/MM vs US M/D).
    try:
        return dateparser.parse(v, dayfirst=dayfirst).date()
    except (ValueError, OverflowError, TypeError):
        return None


def detect_dayfirst(values: list[str | None]) -> bool:
    """Infer whether a date column is day-first (DD/MM) or month-first (M/D).

    A first component > 12 proves day-first; a second component > 12 proves
    month-first. Defaults to day-first (Indian convention) when ambiguous.
    """
    day_first_votes = month_first_votes = 0
    for v in values:
        if not v:
            continue
        m = _DMY_RE.match(v)
        if not m:
            continue
        a, b = int(m.group(1)), int(m.group(2))
        if a > 12:
            day_first_votes += 1
        elif b > 12:
            month_first_votes += 1
    if month_first_votes > day_first_votes:
        return False
    return True


@dataclass
class RowResult:
    ok: bool
    error: str | None = None
    values: dict = field(default_factory=dict)


def extract_row(
    row: dict[str, str],
    mapping: dict[str, str | None],
    dayfirst: bool = True,
    default_stage: str | None = None,
) -> RowResult:
    """Pull canonical values out of one raw row using the column mapping."""

    def raw(field_name: str) -> str | None:
        col = mapping.get(field_name)
        return row.get(col) if col else None

    lead_id = raw("lead_id")
    if is_na(lead_id):
        return RowResult(ok=False, error="missing lead_id")

    # Stage handling depends on whether the feed *has* a stage column:
    #   • no stage column (offer feed)        -> stage=None (new leads get the default)
    #   • stage column present but value #N/A  -> the sentinel "Not in DIY Journey"
    #     (a dialed lead that never entered the offer journey)
    #   • real value                           -> normalized to the catalog name
    if mapping.get("stage"):
        raw_stage = raw("stage")
        stage = catalog.NOT_IN_JOURNEY if is_na(raw_stage) else normalize_stage(raw_stage.strip())
    else:
        stage = None

    # Voice attribution: explicit column wins; otherwise derive from the call
    # outcome (a connected disposition implies the Voice AI reached the lead).
    disposition = None if is_na(raw("last_disposition")) else raw("last_disposition").strip()
    voice_col = raw("voice_connected")
    voice = coerce_bool(voice_col) if not is_na(voice_col) else catalog.is_connected(disposition)
    call_count = coerce_int(raw("call_count"))
    if call_count is None:
        call_count = 1 if disposition else 0

    na_cells = sum(1 for f in FIELD_ALIASES if mapping.get(f) and is_error_token(raw(f)))
    values = {
        "lead_id": lead_id.strip(),
        "stage": stage,
        "entry_date": coerce_date(raw("entry_date"), dayfirst),
        "drop_date": coerce_date(raw("drop_date"), dayfirst),
        "disbursed_amount": coerce_float(raw("disbursed_amount")),
        "max_loan_amount": coerce_float(raw("max_loan_amount")),
        "max_tenure_months": coerce_float(raw("max_tenure_months")),
        "roi": coerce_float(raw("roi")),
        "emi": coerce_float(raw("emi")),
        "processing_fee": coerce_float(raw("processing_fee")),
        "schemecode": None if is_na(raw("schemecode")) else raw("schemecode").strip(),
        "voice_connected": voice,
        "call_count": call_count,
        "last_disposition": disposition,
        "na_cells": na_cells,
    }
    return RowResult(ok=True, values=values)


def _resolve_mapping(headers: list[str], mapping: dict[str, str | None] | None) -> dict[str, str | None]:
    resolved = suggest_mapping(headers)
    if mapping:
        resolved.update({k: v for k, v in mapping.items() if v})
    return resolved


def _dayfirst_for(rows: list[dict[str, str]], mapping: dict[str, str | None]) -> bool:
    """Detect date orientation from the mapped entry_date / drop_date columns."""
    samples: list[str | None] = []
    for fld in ("entry_date", "drop_date"):
        col = mapping.get(fld)
        if col:
            samples.extend(r.get(col) for r in rows[:200])
    return detect_dayfirst(samples)


def build_preview(raw: bytes, mapping: dict[str, str | None] | None = None, limit: int = 8) -> dict:
    """Parse just enough to show the user a mapping + sample before committing."""
    headers, rows = parse_csv(raw)
    mapping = _resolve_mapping(headers, mapping)
    dayfirst = _dayfirst_for(rows, mapping)

    missing_required = [f for f in REQUIRED_FIELDS if not mapping.get(f)]
    has_stage = bool(mapping.get("stage"))
    sample: list[dict] = []
    ok_count = 0
    for r in rows:
        res = extract_row(r, mapping, dayfirst)
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
        "dayfirst": dayfirst,
        "has_stage": has_stage,
        "default_stage": DEFAULT_STAGE,
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
    default_stage: str | None = None,
) -> dict:
    """Parse a CSV and fold it into the reconstructed lead journeys.

    Handles both Kotak feeds (offer + journey), joined on lead_id (= offer_id):
    the offer feed has no stage, so its rows only enrich metadata on existing
    leads (or create a lead at ``default_stage``); the journey feed drives stage
    transitions, entry dates, disbursals and call outcomes.

    Idempotent per drop_date: re-importing the same date updates existing leads
    rather than duplicating them.
    """
    headers, rows = parse_csv(raw)
    resolved_mapping = _resolve_mapping(headers, mapping)

    missing_required = [f for f in REQUIRED_FIELDS if not resolved_mapping.get(f)]
    if missing_required:
        raise ValueError(f"Cannot import: missing required column(s): {', '.join(missing_required)}")

    dayfirst = _dayfirst_for(rows, resolved_mapping)
    fallback_stage = default_stage or DEFAULT_STAGE

    parsed: list[dict] = []
    error_rows = 0
    for r in rows:
        res = extract_row(r, resolved_mapping, dayfirst)
        if res.ok:
            parsed.append(res.values)
        else:
            error_rows += 1

    the_date = _resolve_drop_date(parsed, filename, drop_date)

    # Upsert the DailyDrop record (a date may receive both feeds).
    drop = db.execute(select(DailyDrop).where(DailyDrop.drop_date == the_date)).scalar_one_or_none()
    if drop is None:
        drop = DailyDrop(drop_date=the_date)
        db.add(drop)
    drop.filename = filename or drop.filename
    drop.row_count = len(parsed)
    drop.error_rows = error_rows
    drop.status = "partial" if error_rows else "received"

    db.flush()  # persist the DailyDrop before the bulk ops below

    # De-duplicate within the file (a snapshot should be unique per lead; last wins).
    by_id: dict[str, dict] = {}
    for v in parsed:
        by_id[v["lead_id"]] = v

    new_stages = sorted({
        v["stage"] for v in by_id.values()
        if v["stage"] and v["stage"] not in catalog.STAGE_ORDER
    })

    META_FIELDS = ("max_loan_amount", "max_tenure_months", "roi", "emi",
                   "processing_fee", "schemecode", "disbursed_amount", "last_disposition")

    # Load existing leads as lightweight rows (no ORM identity map / dirty tracking).
    cols = (Lead.id, Lead.lead_id, Lead.current_stage, Lead.stage_entered_on,
            Lead.last_seen_on, Lead.entry_date, Lead.first_seen_on, Lead.na_cells,
            Lead.voice_connected, Lead.call_count, *[getattr(Lead, f) for f in META_FIELDS])
    existing: dict[str, dict] = {}
    if by_id:
        for row in db.execute(select(*cols)).mappings():
            if row["lead_id"] in by_id:
                existing[row["lead_id"]] = row

    new_lead_maps: list[dict] = []
    update_maps: list[dict] = []
    new_lead_stage: list[tuple[str, str]] = []   # (lead_id, initial_stage)
    event_maps: list[dict] = []                  # transition events for existing leads

    for lid, v in by_id.items():
        stage = v["stage"]
        cur = existing.get(lid)

        if cur is None:
            initial_stage = stage or fallback_stage
            new_lead_maps.append({
                "lead_id": lid, "current_stage": initial_stage,
                "entry_date": v["entry_date"] or the_date,
                "stage_entered_on": the_date, "first_seen_on": the_date, "last_seen_on": the_date,
                "voice_connected": v["voice_connected"], "call_count": v["call_count"],
                "na_cells": v["na_cells"], "had_backward_move": False,
                **{f: v[f] for f in META_FIELDS},
            })
            new_lead_stage.append((lid, initial_stage))
            continue

        upd: dict = {"id": cur["id"], "first_seen_on": min(cur["first_seen_on"] or the_date, the_date)}
        if v["entry_date"]:
            upd["entry_date"] = min(cur["entry_date"] or v["entry_date"], v["entry_date"])
        if v["na_cells"]:
            upd["na_cells"] = (cur["na_cells"] or 0) + v["na_cells"]
        is_latest = the_date >= (cur["last_seen_on"] or the_date)
        for fld in META_FIELDS:
            if v[fld] is not None and (is_latest or cur[fld] is None):
                upd[fld] = v[fld]
        if v["voice_connected"] and not cur["voice_connected"]:
            upd["voice_connected"] = True
        if v["call_count"] > (cur["call_count"] or 0):
            upd["call_count"] = v["call_count"]
        if stage and stage != cur["current_stage"]:
            event_maps.append({"lead_pk": cur["id"], "stage": stage, "observed_on": the_date})
            prev_order = catalog.STAGE_ORDER.get(cur["current_stage"])
            new_order = catalog.STAGE_ORDER.get(stage)
            if prev_order is not None and new_order is not None and new_order < prev_order:
                upd["had_backward_move"] = True
            if is_latest:
                upd["current_stage"] = stage
                upd["stage_entered_on"] = the_date
        if is_latest:
            upd["last_seen_on"] = the_date
        update_maps.append(upd)

    new_leads = len(new_lead_maps)
    updated_leads = len(update_maps)

    # Bulk persist. bulk_* bypass the unit-of-work for speed on 350k-row drops.
    if new_lead_maps:
        db.bulk_insert_mappings(Lead, new_lead_maps)
        db.flush()
        idmap = {r["lead_id"]: r["id"] for r in db.execute(select(Lead.id, Lead.lead_id)).mappings()}
        event_maps.extend(
            {"lead_pk": idmap[lid], "stage": st, "observed_on": the_date}
            for lid, st in new_lead_stage
        )
    if update_maps:
        db.bulk_update_mappings(Lead, update_maps)
    if event_maps:
        db.bulk_insert_mappings(StageEvent, event_maps)

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
