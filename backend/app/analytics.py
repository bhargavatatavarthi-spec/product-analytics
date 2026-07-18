"""Analytics computations.

Every screen is derived here from the reconstructed data (`Lead` / `StageEvent`
/ `DailyDrop`) plus the analyst's stage-classification overrides and settings.

Aggregation happens in SQL (`GROUP BY`), not by pulling rows into Python: a
screen's counts come back as ~20 grouped rows regardless of whether the table
holds 4k or 400k leads, so screens stay fast on modest infra. Time is anchored
to `as_of` — the most recent drop date — so imported historical data analyses
correctly regardless of the wall clock.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

from sqlalchemy import Integer, and_, case, cast, func, or_, select
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


def _cr(amount: float) -> str:
    return f"₹{amount / 1e7:.1f} Cr"


def get_as_of(db: Session) -> date:
    latest = db.execute(select(func.max(Lead.last_seen_on))).scalar()
    if latest:
        return latest
    return db.execute(select(func.max(DailyDrop.drop_date))).scalar() or date.today()


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


def has_data(db: Session) -> bool:
    return db.execute(select(func.count(Lead.id))).scalar_one() > 0


def _apply_window(stmt, start: date | None, end: date):
    if start is not None:
        return stmt.where(Lead.entry_date >= start, Lead.entry_date <= end)
    return stmt.where(Lead.entry_date <= end)


def _stage_counts(db: Session, start: date | None, end: date) -> dict[str, int]:
    """{current_stage: lead_count} for the entry-date window — one GROUP BY."""
    stmt = _apply_window(select(Lead.current_stage, func.count()), start, end).group_by(Lead.current_stage)
    return {stage: count for stage, count in db.execute(stmt).all()}


def _bucketize(stage_counts: dict[str, int], overrides: dict[str, str]) -> dict[str, int]:
    counts = {b: 0 for b in catalog.BUCKETS}
    for stage, c in stage_counts.items():
        counts[effective_bucket(stage, overrides)] += c
    return counts


def _days_in_stage(as_of: date):
    """SQL expression: whole days a lead has sat in its current stage."""
    as_of_iso = as_of.isoformat()
    return cast(
        func.julianday(as_of_iso) - func.julianday(func.coalesce(Lead.stage_entered_on, as_of_iso)),
        Integer,
    )


def _won_stages(overrides: dict[str, str]) -> list[str]:
    return [s for s, b in {**catalog.DEFAULT_BUCKET, **overrides}.items() if b == "won"]


# ─────────────────────────── range summaries ───────────────────────────
def range_summaries(db: Session) -> dict:
    as_of = get_as_of(db)
    overrides = get_overrides(db)
    out = {}
    for r in catalog.RANGES:
        start, end = range_window(as_of, r["key"])
        counts = _bucketize(_stage_counts(db, start, end), overrides)
        entered = sum(counts.values())
        delta = None
        if r["days"] is not None and start is not None:
            prev = _bucketize(
                _stage_counts(db, start - timedelta(days=r["days"]), start - timedelta(days=1)), overrides
            )
            delta = {
                b: (None if not prev[b] else round((counts[b] - prev[b]) / prev[b] * 100))
                for b in ("won", "inflight", "lost")
            }
        out[r["key"]] = {"label": r["label"], "full": r["full"], "entered": entered, "delta": delta}
    return {"as_of": as_of.isoformat(), "ranges": out}


# ─────────────────────────── overview ───────────────────────────
def overview(db: Session, range_key: str) -> dict:
    as_of = get_as_of(db)
    overrides = get_overrides(db)
    settings = get_settings(db)
    aging_threshold = int(settings["aging_threshold"])
    start, end = range_window(as_of, range_key)

    stage_counts = _stage_counts(db, start, end)
    counts = _bucketize(stage_counts, overrides)
    entered = sum(counts.values())

    days = catalog.RANGE_DAYS.get(range_key)
    delta = None
    if days is not None and start is not None:
        prev = _bucketize(
            _stage_counts(db, start - timedelta(days=days), start - timedelta(days=1)), overrides
        )
        delta = {
            b: (None if not prev[b] else round((counts[b] - prev[b]) / prev[b] * 100))
            for b in ("won", "inflight", "lost")
        }

    def bucket_block(b: str) -> dict:
        pct = round(counts[b] / entered * 100, 1) if entered else 0
        return {
            "count": counts[b],
            "count_label": indian_format(counts[b]),
            "pct": pct,
            "delta": (delta or {}).get(b) if delta else None,
        }

    buckets = {b: bucket_block(b) for b in ("won", "inflight", "lost", "unclassified")}

    # Aging of in-flight leads — bucket days-in-stage in SQL, group by stage+bucket.
    dis = _days_in_stage(as_of)
    whens = []
    for i, a in enumerate(catalog.AGING_BUCKETS):
        cond = dis >= a["min"] if a["max"] is None else and_(dis >= a["min"], dis <= a["max"])
        whens.append((cond, i))
    age_case = case(*whens, else_=len(catalog.AGING_BUCKETS) - 1)
    age_stmt = _apply_window(
        select(Lead.current_stage, age_case.label("b"), func.count()), start, end
    ).group_by(Lead.current_stage, age_case)

    bar_counts = [0] * len(catalog.AGING_BUCKETS)
    inflight_count = stalled = 0
    for stage, b, c in db.execute(age_stmt).all():
        if effective_bucket(stage, overrides) != "inflight":
            continue
        bar_counts[b] += c
        inflight_count += c
        if catalog.AGING_BUCKETS[b]["min"] >= aging_threshold:
            stalled += c

    first_stalled_idx = next(
        (i for i, a in enumerate(catalog.AGING_BUCKETS) if a["min"] >= aging_threshold), None
    )
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
    date_field = catalog.MILESTONE_DATE_FIELD.get(
        milestone_label, catalog.MILESTONE_DATE_FIELD[catalog.DEFAULT_MILESTONE]
    )
    date_col = getattr(Lead, date_field)
    cohort_dates = [as_of - timedelta(days=13 - i) for i in range(14)]
    earliest = cohort_dates[0]

    # Cohort sizes: one GROUP BY over entry_date (the journey's Created Date).
    sizes = dict(
        db.execute(
            select(Lead.entry_date, func.count())
            .where(Lead.entry_date >= earliest, Lead.entry_date <= as_of)
            .group_by(Lead.entry_date)
        ).all()
    )

    # True reach: reach_day = milestone_date − entry_date, from the feed's own
    # milestone-date column. Only leads that have that date count as reached.
    reach_by_cohort: dict[date, list[int]] = defaultdict(list)
    dated_leads = 0
    for entry, mdate in db.execute(
        select(Lead.entry_date, date_col).where(
            Lead.entry_date >= earliest, Lead.entry_date <= as_of, date_col.isnot(None)
        )
    ).all():
        if entry is not None and mdate is not None:
            reach_by_cohort[entry].append(max(0, (mdate - entry).days))
            dated_leads += 1

    cols = [{"label": f"D{d}", "full": f"Day {d}"} for d in range(14)]
    rows = []
    all_reach: list[int] = []
    plateau_days: list[int] = []
    mature_count = 0
    for c_date in cohort_dates:
        size = sizes.get(c_date, 0)
        age = (as_of - c_date).days
        reach = reach_by_cohort.get(c_date, [])
        all_reach.extend(reach)
        cells = []
        final_frac = 0.0
        for day in range(14):
            if day > age or size == 0:
                cells.append({"mature": False, "value": None, "text": ""})
                continue
            frac = sum(1 for rd in reach if rd <= day) / size * 100
            cells.append({"mature": True, "value": round(frac, 1), "text": f"{frac:.1f}%"})
            final_frac = frac
        if final_frac:
            for day in range(min(age, 13) + 1):
                if sum(1 for rd in reach if rd <= day) / size * 100 >= 0.98 * final_frac:
                    plateau_days.append(day)
                    break
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

    import statistics

    avg_days = round(statistics.mean(all_reach), 1) if all_reach else 0
    plateau_day = round(statistics.median(plateau_days)) if plateau_days else 0
    return {
        "as_of": as_of.isoformat(),
        "milestone": milestone_label,
        "milestone_short": next(
            (m["short"] for m in catalog.MILESTONES if m["label"] == milestone_label), milestone_label
        ),
        "cols": cols,
        "rows": rows,
        "milestone_dated": dated_leads > 0,
        "date_field": date_field,
        "summary": {
            "avg_days": avg_days,
            "plateau_day": plateau_day,
            "mature_cohorts": mature_count,
            "total_cohorts": 14,
        },
    }


# ─────────────────────────── stage explorer ───────────────────────────
def _median_from_hist(pairs: list[tuple[int, int]], n: int) -> float:
    if n == 0:
        return 0.0
    pairs = sorted(pairs)
    lo, hi = (n - 1) // 2, n // 2
    cum = 0
    v_lo = v_hi = pairs[-1][0]
    got_lo = False
    for d, c in pairs:
        cum += c
        if not got_lo and cum > lo:
            v_lo, got_lo = d, True
        if cum > hi:
            v_hi = d
            break
    return (v_lo + v_hi) / 2


def stages(db: Session, range_key: str, stage_filter: str = "all") -> dict:
    as_of = get_as_of(db)
    overrides = get_overrides(db)
    start, end = range_window(as_of, range_key)

    dis = _days_in_stage(as_of)
    stmt = _apply_window(
        select(Lead.current_stage, dis.label("d"), func.count()), start, end
    ).group_by(Lead.current_stage, dis)

    hist: dict[str, list[tuple[int, int]]] = defaultdict(list)
    totals: dict[str, int] = defaultdict(int)
    for stage, d, c in db.execute(stmt).all():
        hist[stage].append((int(d or 0), c))
        totals[stage] += c

    rows = []
    bucket_counts = {"all": 0, "unclassified": 0, "won": 0, "inflight": 0, "lost": 0}
    for stage_name, n in totals.items():
        bucket = effective_bucket(stage_name, overrides)
        median_days = _median_from_hist(hist[stage_name], n)
        rows.append(
            {
                "name": stage_name,
                "bucket": bucket,
                "is_unclassified": bucket == "unclassified",
                "count": n,
                "count_label": indian_format(n),
                "median": f"{median_days:.1f} d",
                "known": stage_name in catalog.STAGE_ORDER,
            }
        )
        bucket_counts["all"] += 1
        bucket_counts[bucket] = bucket_counts.get(bucket, 0) + 1

    rows.sort(key=lambda r: (0 if r["is_unclassified"] else 1, -r["count"]))
    filtered = rows if stage_filter == "all" else [r for r in rows if r["bucket"] == stage_filter]
    return {
        "as_of": as_of.isoformat(),
        "range": range_key,
        "filter": stage_filter,
        "rows": filtered,
        "bucket_counts": bucket_counts,
        "unclassified_count": bucket_counts["unclassified"],
        "empty": len(filtered) == 0,
    }


# ─────────────────────────── attribution ───────────────────────────
def attribution(db: Session, range_key: str, dim_key: str = "amount") -> dict:
    as_of = get_as_of(db)
    overrides = get_overrides(db)
    start, end = range_window(as_of, range_key)
    won_stages = _won_stages(overrides)

    # Voice vs organic split over disbursals — grouped in SQL.
    voice_won = organic_won = 0
    voice_amt = organic_amt = 0.0
    won_rows: list = []
    if won_stages:
        amt_col = func.coalesce(Lead.disbursed_amount, Lead.max_loan_amount, 0)
        split = _apply_window(
            select(Lead.voice_connected, func.count(), func.sum(amt_col)).where(
                Lead.current_stage.in_(won_stages)
            ),
            start,
            end,
        ).group_by(Lead.voice_connected)
        for connected, cnt, amt in db.execute(split).all():
            if connected:
                voice_won, voice_amt = cnt, float(amt or 0)
            else:
                organic_won, organic_amt = cnt, float(amt or 0)
        # Won leads are few — fetch their offer terms directly for the dim cuts.
        won_rows = db.execute(
            _apply_window(
                select(
                    Lead.voice_connected, Lead.disbursed_amount, Lead.max_loan_amount,
                    Lead.max_tenure_months, Lead.roi, Lead.emi, Lead.schemecode,
                ).where(Lead.current_stage.in_(won_stages)),
                start,
                end,
            )
        ).mappings().all()

    won_count = voice_won + organic_won
    ratio_pct = round(voice_won / won_count * 100) if won_count else 0
    org_share = 100 - ratio_pct if won_count else 0
    attr = {
        "ratio_pct": ratio_pct,
        "voice": {"count": voice_won, "count_label": indian_format(voice_won), "amount": _cr(voice_amt), "share": ratio_pct},
        "organic": {"count": organic_won, "count_label": indian_format(organic_won), "amount": _cr(organic_amt), "share": org_share},
    }

    # Call outcomes — grouped in SQL.
    disp_stmt = _apply_window(
        select(Lead.last_disposition, func.count()).where(Lead.last_disposition.isnot(None)), start, end
    ).group_by(Lead.last_disposition)
    disp_counts = {d: c for d, c in db.execute(disp_stmt).all()}
    dialed = db.execute(
        _apply_window(
            select(func.count()).where(or_(Lead.call_count > 0, Lead.last_disposition.isnot(None))), start, end
        )
    ).scalar_one()

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
        {"label": "On DNC / opt-out list", "value": f"{disp_share('DNC Client : Don' + chr(39) + 't Call Further')}%"},
    ]

    dim = catalog.ATTR_DIMENSION_BY_KEY.get(dim_key, catalog.ATTR_DIMENSIONS[0])
    dim_rows = _dimension_rows(won_rows, dim)
    stage_attr = _stage_attribution(db, won_stages, start, end)

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


def _dimension_rows(won_rows: list, dim: dict) -> list[dict]:
    field = dim["field"]
    buckets: dict[str, list] = {}
    order: list[str] = []
    if dim["kind"] == "numeric":
        for b in dim["bins"]:
            buckets[b["name"]] = []
            order.append(b["name"])
        for row in won_rows:
            v = row.get(field)
            if v is None:
                continue
            for b in dim["bins"]:
                if v >= b["lo"] and (b["hi"] is None or v < b["hi"]):
                    buckets[b["name"]].append(row)
                    break
    else:
        for row in won_rows:
            v = row.get(field) or "Unknown"
            buckets.setdefault(v, [])
            if v not in order:
                order.append(v)
            buckets[v].append(row)
        order.sort(key=lambda k: -len(buckets[k]))
        order = order[:8]

    rows = []
    for name in order:
        group = buckets[name]
        if not group:
            rows.append({"name": name, "disb": 0, "disb_label": "0", "voice_pct": 0, "amount": _cr(0)})
            continue
        voice = sum(1 for r in group if r["voice_connected"])
        amount = sum((r["disbursed_amount"] or r["max_loan_amount"] or 0) for r in group)
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


def _stage_attribution(db: Session, won_stages: list[str], start: date | None, end: date) -> list[dict]:
    """Which in-flight stages the voice-won leads passed through, median calls."""
    if not won_stages:
        return []
    vw = db.execute(
        _apply_window(
            select(Lead.id, Lead.call_count).where(
                Lead.current_stage.in_(won_stages), Lead.voice_connected.is_(True)
            ),
            start,
            end,
        )
    ).all()
    if not vw:
        return []
    calls_by_pk = {pk: cc for pk, cc in vw}
    pks = list(calls_by_pk)
    stage_pks: dict[str, set[int]] = defaultdict(set)
    # Chunk the IN() to stay within SQLite's parameter cap.
    for i in range(0, len(pks), 800):
        batch = pks[i : i + 800]
        for pk, stage in db.execute(
            select(StageEvent.lead_pk, StageEvent.stage).where(StageEvent.lead_pk.in_(batch))
        ).all():
            order = catalog.STAGE_ORDER.get(stage)
            if order is None or order >= 100:
                continue
            stage_pks[stage].add(pk)

    import statistics

    rows = []
    for stage_name, pk_set in stage_pks.items():
        calls = [calls_by_pk[pk] for pk in pk_set if calls_by_pk.get(pk)]
        rows.append(
            {
                "stage": stage_name,
                "count": len(pk_set),
                "count_label": indian_format(len(pk_set)),
                "calls": round(statistics.median(calls), 1) if calls else 0,
            }
        )
    rows.sort(key=lambda r: -r["count"])
    return rows[:6]


# ─────────────────────────── data health ───────────────────────────
def health(db: Session) -> dict:
    as_of = get_as_of(db)
    drops = {d.drop_date: d for d in db.execute(select(DailyDrop)).scalars()}
    days = []
    present = 0
    for i in range(30):
        d = as_of - timedelta(days=29 - i)
        drop = drops.get(d)
        if drop is None:
            status = "missing"
        else:
            status = "partial" if drop.status == "partial" else "received"
            present += 1
        days.append({"date": d.strftime("%d %b"), "status": status})
    completeness = round(present / 30 * 100) if drops else 0

    na_rows = db.execute(select(func.count(Lead.id)).where(Lead.na_cells > 0)).scalar_one()
    overrides = get_overrides(db)
    won_stages = _won_stages(overrides)
    zero_disb = (
        db.execute(
            select(func.count(Lead.id)).where(
                Lead.current_stage.in_(won_stages),
                or_(Lead.disbursed_amount.is_(None), Lead.disbursed_amount == 0),
            )
        ).scalar_one()
        if won_stages
        else 0
    )
    backward = db.execute(select(func.count(Lead.id)).where(Lead.had_backward_move.is_(True))).scalar_one()

    flags = [
        {"count": indian_format(na_rows), "label": "Rows with cell errors", "note": "leads with #VALUE!/#REF! source cells"},
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
