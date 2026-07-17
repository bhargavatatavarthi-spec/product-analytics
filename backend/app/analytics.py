"""Analytics computations.

Every screen in the dashboard is derived here from the reconstructed data
(`Lead` / `StageEvent` / `DailyDrop`) plus the analyst's stage-classification
overrides and global settings. Nothing is hard-coded: an empty database yields
zeroed, honest empty states.

Time is anchored to `as_of` — the most recent drop date in the data — so that
imported historical datasets analyse correctly regardless of the wall clock.
"""
from __future__ import annotations

import statistics
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import catalog
from .models import DailyDrop, Lead, StageClassification, StageEvent, Setting

DEFAULT_SETTINGS = {"aging_threshold": "21", "default_milestone": catalog.DEFAULT_MILESTONE}


# ─────────────────────────── helpers ───────────────────────────
def indian_format(n: float) -> str:
    """Format an integer with Indian digit grouping (e.g. 12,34,567)."""
    n = int(round(n))
    sign = "-" if n < 0 else ""
    s = str(abs(n))
    if len(s) <= 3:
        return sign + s
    head, tail = s[:-3], s[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    parts.insert(0, head)
    return sign + ",".join(parts) + "," + tail


def get_as_of(db: Session) -> date:
    latest = db.execute(select(func.max(Lead.last_seen_on))).scalar()
    if latest:
        return latest
    latest_drop = db.execute(select(func.max(DailyDrop.drop_date))).scalar()
    return latest_drop or date.today()


def get_overrides(db: Session) -> dict[str, str]:
    return {c.stage: c.bucket for c in db.execute(select(StageClassification)).scalars()}


def get_settings(db: Session) -> dict[str, str]:
    values = dict(DEFAULT_SETTINGS)
    for s in db.execute(select(Setting)).scalars():
        values[s.key] = s.value
    return values


def effective_bucket(stage: str, overrides: dict[str, str]) -> str:
    if stage in overrides:
        return overrides[stage]
    return catalog.default_bucket_for(stage)


def range_window(as_of: date, range_key: str) -> tuple[date | None, date]:
    days = catalog.RANGE_DAYS.get(range_key, 30)
    if days is None:
        return None, as_of
    return as_of - timedelta(days=days - 1), as_of


def leads_in_window(db: Session, start: date | None, end: date) -> list[Lead]:
    stmt = select(Lead)
    if start is not None:
        stmt = stmt.where(Lead.entry_date >= start, Lead.entry_date <= end)
    else:
        stmt = stmt.where(Lead.entry_date <= end)
    return list(db.execute(stmt).scalars())


def counts_by_bucket(leads: list[Lead], overrides: dict[str, str]) -> dict[str, int]:
    counts = {b: 0 for b in catalog.BUCKETS}
    for lead in leads:
        counts[effective_bucket(lead.current_stage, overrides)] += 1
    return counts


def has_data(db: Session) -> bool:
    return db.execute(select(func.count(Lead.id))).scalar_one() > 0


# ─────────────────────────── range summaries ───────────────────────────
def range_summaries(db: Session) -> dict:
    """entered count + bucket deltas for every range preset (for the picker/anchor)."""
    as_of = get_as_of(db)
    overrides = get_overrides(db)
    out = {}
    for r in catalog.RANGES:
        start, end = range_window(as_of, r["key"])
        leads = leads_in_window(db, start, end)
        counts = counts_by_bucket(leads, overrides)
        delta = None
        if r["days"] is not None:
            prev_start = start - timedelta(days=r["days"])
            prev_end = start - timedelta(days=1)
            prev = counts_by_bucket(leads_in_window(db, prev_start, prev_end), overrides)
            delta = {}
            for b in ("won", "inflight", "lost"):
                if prev[b]:
                    delta[b] = round((counts[b] - prev[b]) / prev[b] * 100)
                else:
                    delta[b] = None
        out[r["key"]] = {
            "label": r["label"],
            "full": r["full"],
            "entered": len(leads),
            "delta": delta,
        }
    return {"as_of": as_of.isoformat(), "ranges": out}


# ─────────────────────────── overview ───────────────────────────
def overview(db: Session, range_key: str) -> dict:
    as_of = get_as_of(db)
    overrides = get_overrides(db)
    settings = get_settings(db)
    aging_threshold = int(settings["aging_threshold"])
    start, end = range_window(as_of, range_key)
    leads = leads_in_window(db, start, end)
    entered = len(leads)
    counts = counts_by_bucket(leads, overrides)

    # deltas vs previous equal window.
    days = catalog.RANGE_DAYS.get(range_key)
    delta = None
    if days is not None and start is not None:
        prev = counts_by_bucket(
            leads_in_window(db, start - timedelta(days=days), start - timedelta(days=1)), overrides
        )
        delta = {}
        for b in ("won", "inflight", "lost"):
            delta[b] = None if not prev[b] else round((counts[b] - prev[b]) / prev[b] * 100)

    def bucket_block(b: str) -> dict:
        pct = round(counts[b] / entered * 100, 1) if entered else 0
        return {
            "count": counts[b],
            "count_label": indian_format(counts[b]),
            "pct": pct,
            "delta": (delta or {}).get(b) if delta else None,
        }

    buckets = {b: bucket_block(b) for b in ("won", "inflight", "lost", "unclassified")}

    # Aging of in-flight leads.
    inflight_leads = [l for l in leads if effective_bucket(l.current_stage, overrides) == "inflight"]
    inflight_count = len(inflight_leads)
    bar_counts = [0] * len(catalog.AGING_BUCKETS)
    stalled = 0
    first_stalled_idx = next(
        (i for i, a in enumerate(catalog.AGING_BUCKETS) if a["min"] >= aging_threshold), None
    )
    for lead in inflight_leads:
        dis = (as_of - (lead.stage_entered_on or as_of)).days
        for i, a in enumerate(catalog.AGING_BUCKETS):
            if dis >= a["min"] and (a["max"] is None or dis <= a["max"]):
                bar_counts[i] += 1
                break
        if dis >= aging_threshold:
            stalled += 1
    aging_bars = [
        {
            "label": a["label"],
            "count": bar_counts[i],
            "count_label": indian_format(bar_counts[i]),
            "stalled": a["min"] >= aging_threshold,
            "first_stalled": i == first_stalled_idx,
        }
        for i, a in enumerate(catalog.AGING_BUCKETS)
    ]
    stalled_pct = round(stalled / inflight_count * 100) if inflight_count else 0

    takeaway = (
        f"Of {indian_format(entered)} leads entered, {buckets['won']['pct']}% Won, "
        f"{buckets['inflight']['pct']}% In-flight, {buckets['lost']['pct']}% Lost. "
        f"{stalled_pct}% of In-flight leads have stalled beyond {aging_threshold} days."
        if entered
        else "No leads in this window yet. Import a daily drop to populate the dashboard."
    )

    return {
        "as_of": as_of.isoformat(),
        "range": range_key,
        "entered": entered,
        "entered_label": indian_format(entered),
        "buckets": buckets,
        "aging": {
            "threshold": aging_threshold,
            "bars": aging_bars,
            "stalled_count": stalled,
            "stalled_count_label": indian_format(stalled),
            "stalled_pct": stalled_pct,
            "inflight_count": inflight_count,
            "inflight_count_label": indian_format(inflight_count),
        },
        "takeaway": takeaway,
    }


# ─────────────────────────── cohort triangle ───────────────────────────
def cohort(db: Session, milestone_label: str) -> dict:
    as_of = get_as_of(db)
    milestone_order = catalog.MILESTONE_ORDER.get(milestone_label, catalog.MILESTONE_ORDER[catalog.DEFAULT_MILESTONE])
    cohort_dates = [as_of - timedelta(days=13 - i) for i in range(14)]
    earliest = cohort_dates[0]

    leads = list(
        db.execute(
            select(Lead).where(Lead.entry_date >= earliest, Lead.entry_date <= as_of)
        ).scalars()
    )
    lead_by_pk = {l.id: l for l in leads}
    # days-to-reach the milestone for each lead (min over qualifying events).
    reach_day: dict[int, int] = {}
    if lead_by_pk:
        events = db.execute(
            select(StageEvent).where(StageEvent.lead_pk.in_(lead_by_pk.keys()))
        ).scalars()
        for ev in events:
            order = catalog.STAGE_ORDER.get(ev.stage)
            if order is None or order < milestone_order:
                continue
            lead = lead_by_pk[ev.lead_pk]
            if not lead.entry_date:
                continue
            d = (ev.observed_on - lead.entry_date).days
            if d < 0:
                d = 0
            reach_day[ev.lead_pk] = min(reach_day.get(ev.lead_pk, d), d)

    cols = [{"label": f"D{d}", "full": f"Day {d}"} for d in range(14)]
    rows = []
    reach_days_all: list[int] = []
    mature_count = 0
    plateau_days: list[int] = []
    for c_idx, c_date in enumerate(cohort_dates):
        cohort_leads = [l for l in leads if l.entry_date == c_date]
        size = len(cohort_leads)
        age = (as_of - c_date).days
        cohort_reach = [reach_day[l.id] for l in cohort_leads if l.id in reach_day]
        reach_days_all.extend(cohort_reach)
        cells = []
        final_frac = None
        plateau_day = None
        for day in range(14):
            if day > age or size == 0:
                cells.append({"mature": False, "value": None, "text": ""})
                continue
            reached = sum(1 for rd in cohort_reach if rd <= day)
            frac = reached / size * 100
            cells.append({"mature": True, "value": round(frac, 1), "text": f"{frac:.1f}%"})
            final_frac = frac
            if plateau_day is None and final_frac and frac >= 0.98 * (max(cohort_reach, default=0) and final_frac or final_frac):
                pass
        # plateau day for this cohort: first day reaching >=98% of its own final value.
        if final_frac:
            for day in range(min(age, 13) + 1):
                reached = sum(1 for rd in cohort_reach if rd <= day)
                if reached / size * 100 >= 0.98 * final_frac:
                    plateau_day = day
                    break
        if plateau_day is not None:
            plateau_days.append(plateau_day)
        if age >= 13:
            mature_count += 1
        rows.append(
            {
                "date": c_date.strftime("%d %b"),
                "size": size,
                "size_label": indian_format(size),
                "age": age,
                "cells": cells,
            }
        )

    avg_days = round(statistics.mean(reach_days_all), 1) if reach_days_all else 0
    plateau_day = round(statistics.median(plateau_days)) if plateau_days else 0
    return {
        "as_of": as_of.isoformat(),
        "milestone": milestone_label,
        "milestone_short": next(
            (m["short"] for m in catalog.MILESTONES if m["label"] == milestone_label), milestone_label
        ),
        "cols": cols,
        "rows": rows,
        "summary": {
            "avg_days": avg_days,
            "plateau_day": plateau_day,
            "mature_cohorts": mature_count,
            "total_cohorts": 14,
        },
    }


# ─────────────────────────── stage explorer ───────────────────────────
def stages(db: Session, range_key: str, stage_filter: str = "all") -> dict:
    as_of = get_as_of(db)
    overrides = get_overrides(db)
    start, end = range_window(as_of, range_key)
    leads = leads_in_window(db, start, end)

    grouped: dict[str, list[Lead]] = {}
    for lead in leads:
        grouped.setdefault(lead.current_stage, []).append(lead)

    rows = []
    bucket_counts = {"all": 0, "unclassified": 0, "won": 0, "inflight": 0, "lost": 0}
    for stage_name, stage_leads in grouped.items():
        bucket = effective_bucket(stage_name, overrides)
        median_days = statistics.median(
            [(as_of - (l.stage_entered_on or as_of)).days for l in stage_leads]
        ) if stage_leads else 0
        rows.append(
            {
                "name": stage_name,
                "bucket": bucket,
                "is_unclassified": bucket == "unclassified",
                "count": len(stage_leads),
                "count_label": indian_format(len(stage_leads)),
                "median": f"{median_days:.1f} d",
                "known": stage_name in catalog.STAGE_ORDER,
            }
        )
        bucket_counts["all"] += 1
        bucket_counts[bucket] = bucket_counts.get(bucket, 0) + 1

    rows.sort(key=lambda r: (0 if r["is_unclassified"] else 1, -r["count"]))
    unclassified_count = bucket_counts["unclassified"]
    filtered = rows if stage_filter == "all" else [r for r in rows if r["bucket"] == stage_filter]

    return {
        "as_of": as_of.isoformat(),
        "range": range_key,
        "filter": stage_filter,
        "rows": filtered,
        "bucket_counts": bucket_counts,
        "unclassified_count": unclassified_count,
        "empty": len(filtered) == 0,
    }


# ─────────────────────────── attribution ───────────────────────────
def _cr(amount: float) -> str:
    return f"₹{amount / 1e7:.1f} Cr"


def attribution(db: Session, range_key: str, dim_key: str = "amount") -> dict:
    as_of = get_as_of(db)
    overrides = get_overrides(db)
    start, end = range_window(as_of, range_key)
    leads = leads_in_window(db, start, end)

    won = [l for l in leads if effective_bucket(l.current_stage, overrides) == "won"]
    won_count = len(won)
    voice_won = [l for l in won if l.voice_connected]
    organic_won = [l for l in won if not l.voice_connected]

    def amt(rows: list[Lead]) -> float:
        return sum((l.disbursed_amount or l.max_loan_amount or 0) for l in rows)

    voice_amt, organic_amt = amt(voice_won), amt(organic_won)
    ratio_pct = round(len(voice_won) / won_count * 100) if won_count else 0
    org_share = 100 - ratio_pct if won_count else 0

    attr = {
        "ratio_pct": ratio_pct,
        "voice": {
            "count": len(voice_won),
            "count_label": indian_format(len(voice_won)),
            "amount": _cr(voice_amt),
            "share": ratio_pct,
        },
        "organic": {
            "count": len(organic_won),
            "count_label": indian_format(len(organic_won)),
            "amount": _cr(organic_amt),
            "share": org_share,
        },
    }

    # Call-outcome breakdown across leads with any dial activity.
    dialed_leads = [l for l in leads if l.call_count > 0 or l.last_disposition]
    dialed = len(dialed_leads)
    disp_counts: dict[str, int] = {}
    for l in dialed_leads:
        if l.last_disposition:
            disp_counts[l.last_disposition] = disp_counts.get(l.last_disposition, 0) + 1
    total_disp = sum(disp_counts.values()) or 1
    connected_total = sum(c for d, c in disp_counts.items() if catalog.is_connected(d))
    connect_rate = round(connected_total / total_disp * 100) if disp_counts else 0
    max_share = max((c / total_disp * 100 for c in disp_counts.values()), default=1)
    call_outcomes = [
        {
            "label": d,
            "connected": catalog.is_connected(d),
            "pct": round(c / total_disp * 100, 1),
            "count": indian_format(c),
            "rel": round(c / total_disp * 100 / max_share * 100) if max_share else 0,
        }
        for d, c in sorted(disp_counts.items(), key=lambda kv: -kv[1])
    ]

    def disp_share(name: str) -> float:
        return round(disp_counts.get(name, 0) / total_disp * 100, 1) if disp_counts else 0

    post_connect = [
        {"label": "Connected — reached a human", "value": f"{connect_rate}%"},
        {"label": "Reschedule / callback booked", "value": f"{disp_share('Call Rescheduled')}%"},
        {"label": "On DNC / opt-out list", "value": f"{disp_share(chr(8220) if False else 'DNC Client : Don' + chr(39) + 't Call Further')}%"},
    ]

    # Offer-metadata attribution by the selected dimension.
    dim = catalog.ATTR_DIMENSION_BY_KEY.get(dim_key, catalog.ATTR_DIMENSIONS[0])
    dim_rows = _dimension_rows(won, dim)

    # Journey-stage attribution: where the Voice AI advanced voice-won leads.
    stage_attr = _stage_attribution(db, voice_won)

    return {
        "as_of": as_of.isoformat(),
        "range": range_key,
        "attr": attr,
        "dialed": dialed,
        "dialed_label": indian_format(dialed),
        "connect_rate": connect_rate,
        "call_outcomes": call_outcomes,
        "post_connect": post_connect,
        "dim": {"key": dim["key"], "label": dim["label"], "field": dim["field"], "rows": dim_rows},
        "stage_attr": stage_attr,
    }


def _dimension_rows(won: list[Lead], dim: dict) -> list[dict]:
    field = dim["field"]
    buckets: dict[str, list[Lead]] = {}
    order: list[str] = []
    if dim["kind"] == "numeric":
        for b in dim["bins"]:
            buckets[b["name"]] = []
            order.append(b["name"])
        for lead in won:
            v = getattr(lead, field, None)
            if v is None:
                continue
            for b in dim["bins"]:
                if v >= b["lo"] and (b["hi"] is None or v < b["hi"]):
                    buckets[b["name"]].append(lead)
                    break
    else:  # categorical
        for lead in won:
            v = getattr(lead, field, None) or "Unknown"
            if v not in buckets:
                buckets[v] = []
                order.append(v)
            buckets[v].append(lead)
        order.sort(key=lambda k: -len(buckets[k]))
        order = order[:8]

    rows = []
    for name in order:
        group = buckets[name]
        if not group:
            rows.append({"name": name, "disb": 0, "disb_label": "0", "voice_pct": 0, "amount": _cr(0)})
            continue
        voice = sum(1 for l in group if l.voice_connected)
        amount = sum((l.disbursed_amount or l.max_loan_amount or 0) for l in group)
        rows.append(
            {
                "name": name,
                "disb": len(group),
                "disb_label": indian_format(len(group)),
                "voice_pct": round(voice / len(group) * 100),
                "amount": _cr(amount),
            }
        )
    return rows


def _stage_attribution(db: Session, voice_won: list[Lead]) -> list[dict]:
    """Count voice-won leads whose journey passed each in-flight stage,
    with the median calls it took — 'where the Voice AI advanced the disbursal'."""
    if not voice_won:
        return []
    pk_to_calls = {l.id: l.call_count for l in voice_won}
    pks = list(pk_to_calls.keys())
    events = db.execute(select(StageEvent).where(StageEvent.lead_pk.in_(pks))).scalars()
    stage_pks: dict[str, set[int]] = {}
    for ev in events:
        order = catalog.STAGE_ORDER.get(ev.stage)
        if order is None or order >= 100:  # skip terminal Won and off-ladder branches
            continue
        stage_pks.setdefault(ev.stage, set()).add(ev.lead_pk)
    rows = []
    for stage_name, pks_set in stage_pks.items():
        calls = [pk_to_calls[pk] for pk in pks_set if pk_to_calls.get(pk)]
        rows.append(
            {
                "stage": stage_name,
                "count": len(pks_set),
                "count_label": indian_format(len(pks_set)),
                "calls": round(statistics.median(calls), 1) if calls else 0,
            }
        )
    rows.sort(key=lambda r: -r["count"])
    return rows[:6]


# ─────────────────────────── data health ───────────────────────────
def health(db: Session) -> dict:
    as_of = get_as_of(db)
    drops = {
        d.drop_date: d
        for d in db.execute(select(DailyDrop)).scalars()
    }
    days = []
    present = 0
    for i in range(30):
        d = as_of - timedelta(days=29 - i)
        drop = drops.get(d)
        if drop is None:
            status = "missing"
        elif drop.status == "partial":
            status = "partial"
            present += 1
        else:
            status = "received"
            present += 1
        days.append({"date": d.strftime("%d %b"), "status": status})
    completeness = round(present / 30 * 100) if drops else 0

    na_rows = db.execute(select(func.count(Lead.id)).where(Lead.na_cells > 0)).scalar_one()
    overrides = get_overrides(db)
    won_stages = [s for s, b in {**catalog.DEFAULT_BUCKET, **overrides}.items() if b == "won"]
    zero_disb = db.execute(
        select(func.count(Lead.id)).where(
            Lead.current_stage.in_(won_stages),
            (Lead.disbursed_amount.is_(None)) | (Lead.disbursed_amount == 0),
        )
    ).scalar_one() if won_stages else 0
    backward = db.execute(select(func.count(Lead.id)).where(Lead.had_backward_move.is_(True))).scalar_one()

    flags = [
        {"count": indian_format(na_rows), "label": "Rows with #N/A", "note": "leads with missing source cells"},
        {"count": indian_format(zero_disb), "label": "Zero-value disbursals", "note": "flagged for review"},
        {"count": indian_format(backward), "label": "Backward stage moves", "note": "correction vs. real regression"},
    ]
    return {
        "as_of": as_of.isoformat(),
        "completeness": completeness,
        "days": days,
        "flags": flags,
        "total_drops": len(drops),
    }
