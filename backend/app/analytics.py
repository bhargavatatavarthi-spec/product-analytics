"""Analytics computations.

Every screen is derived here from the reconstructed data (`Lead` / `StageEvent`
/ `DailyDrop`) plus the global settings.

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
from .models import DailyDrop, Lead, StageEvent, Setting

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


def get_settings(db: Session) -> dict[str, str]:
    values = dict(DEFAULT_SETTINGS)
    for s in db.execute(select(Setting)).scalars():
        values[s.key] = s.value
    return values


def effective_bucket(stage: str) -> str:
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


def _bucketize(stage_counts: dict[str, int]) -> dict[str, int]:
    counts = {b: 0 for b in catalog.BUCKETS}
    for stage, c in stage_counts.items():
        counts[effective_bucket(stage)] += c
    return counts


def _days_in_stage(as_of: date):
    """SQL expression: whole days a lead has sat in its current stage."""
    as_of_iso = as_of.isoformat()
    return cast(
        func.julianday(as_of_iso) - func.julianday(func.coalesce(Lead.stage_entered_on, as_of_iso)),
        Integer,
    )


def _won_stages() -> list[str]:
    return [s for s, b in catalog.DEFAULT_BUCKET.items() if b == "won"]


# ─────────────────────────── range summaries ───────────────────────────
def range_summaries(db: Session) -> dict:
    as_of = get_as_of(db)
    out = {}
    for r in catalog.RANGES:
        start, end = range_window(as_of, r["key"])
        counts = _bucketize(_stage_counts(db, start, end))
        entered = sum(counts.values())
        delta = None
        if r["days"] is not None and start is not None:
            prev = _bucketize(
                _stage_counts(db, start - timedelta(days=r["days"]), start - timedelta(days=1))
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
    settings = get_settings(db)
    aging_threshold = int(settings["aging_threshold"])
    start, end = range_window(as_of, range_key)

    stage_counts = _stage_counts(db, start, end)
    counts = _bucketize(stage_counts)
    entered = sum(counts.values())

    days = catalog.RANGE_DAYS.get(range_key)
    delta = None
    if days is not None and start is not None:
        prev = _bucketize(
            _stage_counts(db, start - timedelta(days=days), start - timedelta(days=1))
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
        if effective_bucket(stage) != "inflight":
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
NUM_WEEKS = 3
WEEK_WINDOW_DAYS = NUM_WEEKS * 7  # 21 days of daily cohorts, bucketed into 3 week columns


def _week_bucket(age_days: int) -> int:
    """Which week-of-age column (0-indexed) an age in days falls into."""
    return min(age_days // 7, NUM_WEEKS - 1)


def cohort(db: Session, milestone_label: str) -> dict:
    """Cumulative cohort funnel: reach of a milestone by week 1 / 2 / 3.

    A cohort = the fixed set of leads with the same Created Date. Each week
    column is the **cumulative** share of that cohort observed to have reached
    (at or past) the selected milestone *by the end of* that week of age:
    W1 = by day 6, W2 = by day 13, W3 = by day 20. Because it is cumulative,
    the columns never decrease left-to-right (a lead that reached a stage stays
    reached), and the denominator is always the full cohort size.

    Reach timing comes from the stored ``StageEvent`` history — the drop date on
    which a lead was first observed at/past the milestone — so a cohort's row
    fills across the weeks as successive (weekly) snapshots are imported. With a
    single snapshot a cohort is only observed once, so just the week matching its
    current age is populated; the triangle fills in as more drops arrive.
    """
    as_of = get_as_of(db)
    milestone_order = catalog.MILESTONE_ORDER.get(
        milestone_label, catalog.MILESTONE_ORDER[catalog.DEFAULT_MILESTONE]
    )
    # Stages that count as "at or past" this milestone.
    reached_stages = {s for s, o in catalog.STAGE_ORDER.items() if o is not None and o >= milestone_order}

    cohort_dates = [as_of - timedelta(days=WEEK_WINDOW_DAYS - 1 - i) for i in range(WEEK_WINDOW_DAYS)]
    earliest = cohort_dates[0]
    cutoffs = [w * 7 + 6 for w in range(NUM_WEEKS)]  # week-end ages: 6, 13, 20

    # Cohort size + the earliest age at which we observed the cohort at all, in
    # one GROUP BY. Cohorts use the real Created Date (created_on) only — leads
    # with no Created Date are excluded, not lumped into a fake "today" cohort.
    size: dict[date, int] = {}
    first_age: dict[date, int] = {}
    for cdate, cnt, first_seen in db.execute(
        select(Lead.created_on, func.count(), func.min(Lead.first_seen_on))
        .where(Lead.created_on >= earliest, Lead.created_on <= as_of)
        .group_by(Lead.created_on)
    ).all():
        if cdate is None:
            continue
        size[cdate] = cnt
        first_age[cdate] = (first_seen - cdate).days if first_seen else 0

    # Per-lead earliest reach: the first drop date a lead was seen at/past the
    # milestone (from StageEvent). reach_age = that date − Created Date. Bucket
    # each reached lead cumulatively into every week whose cutoff it meets.
    cum: dict[date, list[int]] = defaultdict(lambda: [0] * NUM_WEEKS)
    if reached_stages:
        for cdate, reach_on in db.execute(
            select(Lead.created_on, func.min(StageEvent.observed_on))
            .join(StageEvent, StageEvent.lead_pk == Lead.id)
            .where(
                Lead.created_on >= earliest,
                Lead.created_on <= as_of,
                StageEvent.stage.in_(reached_stages),
            )
            .group_by(Lead.id)
        ).all():
            if cdate is None or reach_on is None:
                continue
            reach_age = max(0, (reach_on - cdate).days)
            for w, cut in enumerate(cutoffs):
                if reach_age <= cut:
                    cum[cdate][w] += 1

    cols = [
        {"label": f"W{w + 1}", "full": f"Week {w + 1} (reached by day {w * 7 + 6})"}
        for w in range(NUM_WEEKS)
    ]
    rows = []
    total_size = total_reached = 0
    nonempty = []  # (age, reached_now_pct) for cohorts that have leads
    for c_date in cohort_dates:
        sz = size.get(c_date, 0)
        age = (as_of - c_date).days
        fa = first_age.get(c_date, age)
        counts = cum.get(c_date, [0] * NUM_WEEKS)
        total_size += sz
        total_reached += counts[NUM_WEEKS - 1]  # reached by current age (cumulative)
        if sz:
            nonempty.append((age, round(counts[NUM_WEEKS - 1] / sz * 100, 1)))
        cells = []
        for w in range(NUM_WEEKS):
            week_start, week_end = w * 7, w * 7 + 6
            # Observed only if the cohort has entered this week (age past its
            # start) AND we began watching it by the week's end (so the week is
            # actually covered by a snapshot, not silently read as zero).
            observed = sz > 0 and age >= week_start and fa <= week_end
            if observed:
                pct = round(counts[w] / sz * 100, 1)
                cells.append({
                    "mature": True, "value": pct, "text": f"{pct:.1f}%",
                    "partial": age < week_end,  # week still in progress for this cohort
                })
            else:
                cells.append({"mature": False, "value": None, "text": "", "partial": False})
        rows.append(
            {
                "date": c_date.strftime("%d %b"),
                "size": sz,
                "size_label": indian_format(sz),
                "age": age,
                "week": _week_bucket(age) + 1,
                "cells": cells,
            }
        )

    overall_pct = round(total_reached / total_size * 100, 1) if total_size else 0.0
    # Newest / oldest reach = cumulative reach-so-far of the youngest / oldest
    # cohort that actually has leads (skip empty date rows at the window edges).
    newest_pct = min(nonempty, key=lambda x: x[0])[1] if nonempty else 0.0
    oldest_pct = max(nonempty, key=lambda x: x[0])[1] if nonempty else 0.0
    return {
        "as_of": as_of.isoformat(),
        "milestone": milestone_label,
        "milestone_short": next(
            (m["short"] for m in catalog.MILESTONES if m["label"] == milestone_label), milestone_label
        ),
        "cols": cols,
        "rows": rows,
        "summary": {
            "overall_pct": overall_pct,
            "newest_pct": newest_pct,
            "oldest_pct": oldest_pct,
            "cohorts": sum(1 for c in cohort_dates if size.get(c, 0) > 0),
            "total_cohorts": WEEK_WINDOW_DAYS,
        },
    }


# ─────────────────────────── attribution ───────────────────────────
def attribution(db: Session, range_key: str, dim_key: str = "amount") -> dict:
    as_of = get_as_of(db)
    start, end = range_window(as_of, range_key)
    won_stages = _won_stages()

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


