"""Tests for the analytics engine."""
from __future__ import annotations

from datetime import date, timedelta

from app import analytics, ingest


def _journey_csv(rows: list[tuple[str, str, str, str]]) -> bytes:
    # (offer_id, disposition, created_date_iso, stage)
    head = "offer_id,last_call_outcome,Created Date,DIY Sub-stage,DIS VALUE\n"
    body = "".join(f"{r[0]},{r[1]},{r[2]},{r[3]},{r[4] if len(r) > 4 else ''}\n" for r in rows)
    return (head + body).encode()


def test_empty_state(db):
    ov = analytics.overview(db, "30d")
    assert ov["entered"] == 0
    assert "Import a daily drop" in ov["takeaway"]
    assert analytics.has_data(db) is False


def test_buckets(db):
    today = date.today()
    d = today.isoformat()
    ingest.ingest_drop(db, _journey_csv([
        ("A", "Interested", d, "DISBURSEMENT_COMPLETED", "500000"),
        ("B", "Interested", d, "OFFER_ACCEPTED", ""),
        ("C", "Not Eligible", d, "APPLICATION_REJECTED", ""),
        ("D", "Interested", d, "OFFER_SELECTED", ""),
    ]), filename=f"j_{d}.csv", drop_date=today)

    ov = analytics.overview(db, "all")
    assert ov["entered"] == 4
    assert ov["buckets"]["won"]["count"] == 1       # Disbursement Completed
    assert ov["buckets"]["lost"]["count"] == 1       # Application Rejected
    assert ov["buckets"]["inflight"]["count"] == 2   # Offer Accepted + Offer Selected


def test_unclassified_bucket_default(db):
    today = date.today()
    ingest.ingest_drop(db, _journey_csv([
        ("A", "Interested", today.isoformat(), "APPLICATION_ON_HOLD", ""),
    ]), filename="j.csv", drop_date=today)

    ov = analytics.overview(db, "all")
    assert ov["buckets"]["unclassified"]["count"] == 1  # On Hold defaults unclassified


def test_cohort_single_snapshot_fills_only_current_week(db):
    today = date.today()
    old = today - timedelta(days=8)  # observed once, at age 8 -> week 2
    ingest.ingest_drop(db, _journey_csv([
        ("A", "Interested", old.isoformat(), "OFFER_REVIEW", ""),
        ("B", "Interested", old.isoformat(), "DISBURSEMENT_COMPLETED", "100000"),
        ("C", "Interested", old.isoformat(), "OFFER_GENERATED", ""),
        ("D", "Interested", old.isoformat(), "APPLICATION_REJECTED", ""),  # never reached
    ]), filename="j.csv", drop_date=today)

    co = analytics.cohort(db, "Offer Generated")
    assert len(co["cols"]) == 3
    assert len(co["rows"]) == 21
    row = next(r for r in co["rows"] if r["size"] == 4)
    assert row["age"] == 8
    # First observed at age 8, so W1 (by day 6) was never watched -> only W2 shows.
    observed = [(i, c["value"]) for i, c in enumerate(row["cells"]) if c["mature"]]
    assert len(observed) == 1
    col, value = observed[0]
    assert col == 1  # W2
    # 3 of 4 (Offer Review, Disbursement, Offer Generated) are at/past Offer Generated.
    assert value == 75.0


def test_cohort_reach_is_at_or_past(db):
    today = date.today()
    old = today - timedelta(days=5)  # 5 days old -> observed in W1 (0-6d)
    # A lead at "AA Initiated" counts as having reached the earlier "Offer Selected".
    ingest.ingest_drop(db, _journey_csv([
        ("A", "Interested", old.isoformat(), "AA_INITIATED", ""),
    ]), filename="j.csv", drop_date=today)
    row = next(r for r in analytics.cohort(db, "Offer Selected")["rows"] if r["size"] == 1)
    assert row["week"] == 1
    assert row["cells"][0]["value"] == 100.0  # measured at W1, reached


def test_cohort_cumulative_reach_across_snapshots(db):
    """Two weekly drops -> a cohort's row fills cumulatively across W1 and W2."""
    today = date.today()
    created = today - timedelta(days=10)          # cohort created 10 days ago
    drop1 = today - timedelta(days=7)             # age 3 (week 1): only offer generated
    drop2 = today                                 # age 10 (week 2): now disbursed
    ingest.ingest_drop(db, _journey_csv([
        ("A", "Interested", created.isoformat(), "OFFER_GENERATED", ""),
    ]), filename="d1.csv", drop_date=drop1)
    ingest.ingest_drop(db, _journey_csv([
        ("A", "Interested", created.isoformat(), "DISBURSEMENT_COMPLETED", "100000"),
    ]), filename="d2.csv", drop_date=drop2)

    # Offer Generated was reached in week 1 -> cumulative 100% from W1 onward.
    og = next(r for r in analytics.cohort(db, "Offer Generated")["rows"] if r["size"] == 1)
    assert og["cells"][0]["value"] == 100.0  # W1
    assert og["cells"][1]["value"] == 100.0  # W2 (still reached)

    # Disbursement only happened in week 2 -> 0% by W1, 100% by W2 (cumulative).
    dc = next(r for r in analytics.cohort(db, "Disbursement Completed")["rows"] if r["size"] == 1)
    assert dc["cells"][0]["mature"] is True and dc["cells"][0]["value"] == 0.0   # W1: not yet
    assert dc["cells"][1]["mature"] is True and dc["cells"][1]["value"] == 100.0  # W2: reached


def test_cohort_week_boundaries(db):
    today = date.today()
    # Ages 6, 7, 13, 14, 20 probe every W1/W2/W3 boundary.
    ages_and_stages = [(6, "A"), (7, "B"), (13, "C"), (14, "D"), (20, "E")]
    rows = [
        (lid, "Interested", (today - timedelta(days=age)).isoformat(), "OFFER_GENERATED", "")
        for age, lid in ages_and_stages
    ]
    ingest.ingest_drop(db, _journey_csv(rows), filename="j.csv", drop_date=today)

    co = analytics.cohort(db, "Offer Generated")
    weeks_by_age = {r["age"]: r["week"] for r in co["rows"] if r["size"]}
    assert weeks_by_age == {6: 1, 7: 2, 13: 2, 14: 3, 20: 3}
    # A lead older than the 21-day window (e.g. 21d) falls outside the grid entirely.
    assert all(r["age"] <= 20 for r in co["rows"])


def test_dia_date_alias_maps_to_aa_initiated(db):
    from app import ingest
    from sqlalchemy import select
    from app.models import Lead
    ingest.ingest_drop(db, (
        "INTERNAL_ID,DIY Sub-Stage,Created Date,DIA Date\n"
        "A,AA_INITIATED,05-07-2026,08-07-2026\n"
    ).encode(), filename="j.csv", drop_date=date(2026, 7, 18))
    lead = db.execute(select(Lead).where(Lead.lead_id == "A")).scalar_one()
    assert lead.aa_initiated_on == date(2026, 7, 8)


def test_indian_format():
    assert analytics.indian_format(1234567) == "12,34,567"
    assert analytics.indian_format(100) == "100"
    assert analytics.indian_format(4200) == "4,200"
