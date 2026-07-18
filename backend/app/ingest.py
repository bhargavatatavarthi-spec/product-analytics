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
from itertools import chain as _chain, islice as _islice

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
    "schemecode": ["schemecode", "scheme_code", "scheme_id", "schemeid", "scheme", "product_code", "product"],
    "voice_connected": ["connected_at_least_once", "voice_connected", "connected", "ai_connected", "is_connected"],
    "call_count": ["call_count", "calls", "num_calls", "attempts", "dial_count"],
    "last_disposition": ["last_call_outcome", "last_disposition", "disposition", "call_disposition", "outcome", "call_outcome"],
    # Explicit milestone dates for cohort analysis (optional; auto-detected).
    "offer_generated_on": ["offer_generated_date", "offer_generated_on", "offer_generated", "offer_gen_date", "og_date"],
    "offer_selected_on": ["offer_selected_date", "offer_selected_on", "offer_selected", "offer_selection_date", "os_date"],
    "aa_initiated_on": ["aa_initiated_date", "aa_initiated_on", "aa_initiation_date", "aa_date", "dia_date", "dia_initiated_date", "dia_initiation_date", "account_aggregator_date"],
    "disbursement_on": ["disbursement_date", "disbursed_date", "disbursal_date", "disbursement_completed_date", "disbursal_completed_date", "disbursement_on", "disbursed_on"],
}

# Canonical milestone-date fields (subset of FIELD_ALIASES) for reporting/mapping.
MILESTONE_DATE_FIELDS = ("offer_generated_on", "offer_selected_on", "aa_initiated_on", "disbursement_on")

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


def _dialect(text: str):
    try:
        return csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        return csv.excel


def open_reader(raw: bytes):
    """Return (headers, row-iterator) without materializing all rows — lets the
    preview and import stream a 350k-row file in constant memory."""
    text = raw.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text), dialect=_dialect(text))
    headers = [h for h in (reader.fieldnames or []) if h is not None]
    return headers, reader


def parse_csv(raw: bytes) -> tuple[list[str], list[dict[str, str]]]:
    """Decode + parse a CSV into (headers, list-of-row-dicts). Convenience for
    small inputs / tests; the import path streams via open_reader instead."""
    headers, reader = open_reader(raw)
    return headers, [dict(r) for r in reader]


def _chunks(iterator, size: int):
    """Yield lists of up to `size` items from an iterator."""
    chunk: list = []
    for item in iterator:
        chunk.append(item)
        if len(chunk) >= size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


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


def coerce_roi(value: str | None) -> float | None:
    """ROI to a percentage. The offer feed stores it as a fraction (0.115),
    other exports as a percent (11.5) — normalize any 0<roi<=1 to percent."""
    f = coerce_float(value)
    if f is None:
        return None
    return round(f * 100, 2) if 0 < f <= 1 else f


def compute_emi(principal: float | None, annual_roi_pct: float | None, months: float | None) -> float | None:
    """Reducing-balance EMI from loan terms: P·r·(1+r)^n / ((1+r)^n − 1).

    Used when the offer feed doesn't carry an EMI column but has amount, ROI and
    tenure. `annual_roi_pct` is a percentage (11.5), not a fraction.
    """
    if not principal or not months or annual_roi_pct is None:
        return None
    n = int(months)
    if n <= 0:
        return None
    r = annual_roi_pct / 1200.0  # monthly rate as a fraction
    if r == 0:
        return round(principal / n, 2)
    factor = (1 + r) ** n
    return round(principal * r * factor / (factor - 1), 2)


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
        "roi": coerce_roi(raw("roi")),
        "emi": coerce_float(raw("emi")),
        "processing_fee": coerce_float(raw("processing_fee")),
        "schemecode": None if is_na(raw("schemecode")) else raw("schemecode").strip(),
        "voice_connected": voice,
        "call_count": call_count,
        "last_disposition": disposition,
        "offer_generated_on": coerce_date(raw("offer_generated_on"), dayfirst),
        "offer_selected_on": coerce_date(raw("offer_selected_on"), dayfirst),
        "aa_initiated_on": coerce_date(raw("aa_initiated_on"), dayfirst),
        "disbursement_on": coerce_date(raw("disbursement_on"), dayfirst),
        "na_cells": na_cells,
    }
    # Derive EMI from terms when the feed doesn't supply it (reducing balance).
    if values["emi"] is None:
        values["emi"] = compute_emi(values["max_loan_amount"], values["roi"], values["max_tenure_months"])
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
    """Stream the file to show a mapping + sample before committing.

    Constant-memory: samples the first chunk for date-format detection and only
    keeps `limit` example rows, counting the rest without materializing them.
    """
    headers, reader = open_reader(raw)
    mapping = _resolve_mapping(headers, mapping)

    first = list(_islice(reader, 500))
    dayfirst = _dayfirst_for(first, mapping)

    missing_required = [f for f in REQUIRED_FIELDS if not mapping.get(f)]
    has_stage = bool(mapping.get("stage"))
    sample: list[dict] = []
    ok_count = total = 0
    for r in _chain(first, reader):
        total += 1
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
        "total_rows": total,
        "valid_rows": ok_count,
        "invalid_rows": total - ok_count,
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
    progress_cb=None,
) -> dict:
    """Parse a CSV and fold it into the reconstructed lead journeys.

    Handles both Kotak feeds (offer + journey), joined on lead_id (= offer_id):
    the offer feed has no stage, so its rows only enrich metadata on existing
    leads (or create a lead at ``default_stage``); the journey feed drives stage
    transitions, entry dates, disbursals and call outcomes.

    Idempotent per drop_date: re-importing the same date updates existing leads
    rather than duplicating them.
    """
    headers, reader = open_reader(raw)
    resolved_mapping = _resolve_mapping(headers, mapping)

    missing_required = [f for f in REQUIRED_FIELDS if not resolved_mapping.get(f)]
    if missing_required:
        raise ValueError(f"Cannot import: missing required column(s): {', '.join(missing_required)}")

    fallback_stage = default_stage or DEFAULT_STAGE
    META_FIELDS = ("max_loan_amount", "max_tenure_months", "roi", "emi",
                   "processing_fee", "schemecode", "disbursed_amount", "last_disposition",
                   "offer_generated_on", "offer_selected_on", "aa_initiated_on", "disbursement_on")
    detected_dates = [f for f in MILESTONE_DATE_FIELDS if resolved_mapping.get(f)]
    cols = (Lead.id, Lead.lead_id, Lead.current_stage, Lead.stage_entered_on,
            Lead.last_seen_on, Lead.entry_date, Lead.first_seen_on, Lead.na_cells,
            Lead.voice_connected, Lead.call_count, Lead.had_backward_move,
            *[getattr(Lead, f) for f in META_FIELDS])

    # Chunk size stays within SQLite's 999 bound-parameter cap for the IN(...)
    # look-ups, and keeps peak memory flat regardless of file size.
    CHUNK = 900
    # Peek the first chunk to detect date orientation without reading it all.
    first = list(_islice(reader, CHUNK))
    dayfirst = _dayfirst_for(first, resolved_mapping)

    totals = {"total": 0, "imported": 0, "error": 0, "new": 0, "updated": 0}
    new_stages: set[str] = set()
    state: dict = {"date": drop_date, "drop": None, "since_commit": 0}

    def process_chunk(raw_rows: list[dict]) -> None:
        parsed: list[dict] = []
        for r in raw_rows:
            totals["total"] += 1
            res = extract_row(r, resolved_mapping, dayfirst)
            if res.ok:
                parsed.append(res.values)
            else:
                totals["error"] += 1
        if not parsed:
            return

        # Resolve the drop date + DailyDrop record once, on the first data chunk.
        if state["drop"] is None:
            the_date = _resolve_drop_date(parsed, filename, drop_date)
            state["date"] = the_date
            # One ledger row per (date, file): re-importing the same file updates
            # it; a different file for the same date is a separate row, so both
            # the journey and offer feeds stay visible in the history.
            drop = db.execute(
                select(DailyDrop).where(DailyDrop.drop_date == the_date, DailyDrop.filename == filename)
            ).scalar_one_or_none()
            if drop is None:
                drop = DailyDrop(drop_date=the_date, filename=filename)
                db.add(drop)
            db.flush()
            state["drop"] = drop
        the_date = state["date"]

        # De-dup within the chunk; cross-chunk dedup happens naturally because a
        # repeated lead is already persisted (and thus "existing") next time.
        by_id: dict[str, dict] = {}
        for v in parsed:
            by_id[v["lead_id"]] = v
        for v in by_id.values():
            if v["stage"] and v["stage"] not in catalog.STAGE_ORDER:
                new_stages.add(v["stage"])

        ids = list(by_id)
        existing = {
            row["lead_id"]: row
            for row in db.execute(select(*cols).where(Lead.lead_id.in_(ids))).mappings()
        }

        new_lead_maps: list[dict] = []
        update_maps: list[dict] = []
        new_lead_stage: list[tuple[str, str]] = []
        event_maps: list[dict] = []

        for lid, v in by_id.items():
            stage = v["stage"]
            real_stage = stage if (stage and stage != catalog.NOT_IN_JOURNEY) else None
            has_offer = v["max_loan_amount"] is not None
            cur = existing.get(lid)

            if cur is None:
                if real_stage:
                    initial_stage = real_stage
                elif has_offer:
                    initial_stage = "Offer Generated"
                elif stage == catalog.NOT_IN_JOURNEY:
                    initial_stage = catalog.NOT_IN_JOURNEY
                else:
                    initial_stage = fallback_stage
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

            is_latest = the_date >= (cur["last_seen_on"] or the_date)
            cur_stage = cur["current_stage"]
            applied_stage = None
            if real_stage and real_stage != cur_stage:
                applied_stage = real_stage
            elif has_offer and cur_stage == catalog.NOT_IN_JOURNEY:
                applied_stage = "Offer Generated"

            backward = bool(cur["had_backward_move"])
            if applied_stage:
                event_maps.append({"lead_pk": cur["id"], "stage": applied_stage, "observed_on": the_date})
                prev_order = catalog.STAGE_ORDER.get(cur_stage)
                new_order = catalog.STAGE_ORDER.get(applied_stage)
                if prev_order is not None and new_order is not None and new_order < prev_order:
                    backward = True
            stage_changes = applied_stage is not None and is_latest

            # Uniform key set on every update dict: unchanged columns are written
            # back with their current value. This keeps bulk_update_mappings to a
            # single cached statement (varying key-sets otherwise explode the
            # compiled-statement cache to hundreds of MB on a 350k-row update).
            upd: dict = {
                "id": cur["id"],
                "first_seen_on": min(cur["first_seen_on"] or the_date, the_date),
                "last_seen_on": the_date if is_latest else cur["last_seen_on"],
                "entry_date": (min(cur["entry_date"] or v["entry_date"], v["entry_date"])
                               if v["entry_date"] else cur["entry_date"]),
                "na_cells": (cur["na_cells"] or 0) + v["na_cells"],
                "voice_connected": bool(cur["voice_connected"]) or v["voice_connected"],
                "call_count": max(cur["call_count"] or 0, v["call_count"]),
                "current_stage": applied_stage if stage_changes else cur_stage,
                "stage_entered_on": the_date if stage_changes else cur["stage_entered_on"],
                "had_backward_move": backward,
            }
            for fld in META_FIELDS:
                upd[fld] = v[fld] if (v[fld] is not None and (is_latest or cur[fld] is None)) else cur[fld]
            update_maps.append(upd)

        totals["new"] += len(new_lead_maps)
        totals["updated"] += len(update_maps)
        totals["imported"] += len(by_id)

        if new_lead_maps:
            db.bulk_insert_mappings(Lead, new_lead_maps)
            db.flush()
            new_ids = [m["lead_id"] for m in new_lead_maps]
            idmap = {
                r["lead_id"]: r["id"]
                for r in db.execute(select(Lead.id, Lead.lead_id).where(Lead.lead_id.in_(new_ids))).mappings()
            }
            event_maps.extend(
                {"lead_pk": idmap[lid], "stage": st, "observed_on": the_date}
                for lid, st in new_lead_stage
            )
        if update_maps:
            db.bulk_update_mappings(Lead, update_maps)
        if event_maps:
            db.bulk_insert_mappings(StageEvent, event_maps)
        # Commit periodically so SQLite flushes dirty pages to disk rather than
        # holding the whole file's transaction in memory. Batching (vs per-chunk)
        # keeps fsync overhead low while peak RSS stays flat.
        state["since_commit"] += 1
        if state["since_commit"] >= 25:
            db.commit()
            state["since_commit"] = 0
        else:
            db.flush()

        if progress_cb:
            progress_cb(totals["total"])

    process_chunk(first)
    for chunk in _chunks(reader, CHUNK):
        process_chunk(chunk)

    # Empty / all-invalid file: still record the drop so the ledger shows it.
    if state["drop"] is None:
        the_date = _resolve_drop_date([], filename, drop_date)
        state["date"] = the_date
        drop = db.execute(
            select(DailyDrop).where(DailyDrop.drop_date == the_date, DailyDrop.filename == filename)
        ).scalar_one_or_none()
        if drop is None:
            drop = DailyDrop(drop_date=the_date, filename=filename)
            db.add(drop)
        state["drop"] = drop

    drop = state["drop"]
    drop.row_count = totals["imported"]
    drop.error_rows = totals["error"]
    drop.status = "partial" if totals["error"] else "received"
    db.commit()

    return {
        "drop_date": state["date"].isoformat(),
        "filename": filename,
        "status": drop.status,
        "total_rows": totals["total"],
        "imported_rows": totals["imported"],
        "error_rows": totals["error"],
        "new_leads": totals["new"],
        "updated_leads": totals["updated"],
        "new_unmapped_stages": sorted(new_stages),
        "milestone_dates_detected": detected_dates,
        "mapping": resolved_mapping,
    }
