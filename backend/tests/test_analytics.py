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


def test_cohort_single_snapshot_observes_only_that_day(db):
    today = date.today()
    old = today - timedelta(days=8)  # cohort is 8 days old
    ingest.ingest_drop(db, _journey_csv([
        ("A", "Interested", old.isoformat(), "OFFER_REVIEW", ""),
        ("B", "Interested", old.isoformat(), "DISBURSEMENT_COMPLETED", "100000"),
        ("C", "Interested", old.isoformat(), "OFFER_GENERATED", ""),
        ("D", "Interested", old.isoformat(), "APPLICATION_REJECTED", ""),  # never reached
    ]), filename="j.csv", drop_date=today)

    co = analytics.cohort(db, "Offer Generated")
    assert len(co["rows"]) == 14
    row = next(r for r in co["rows"] if r["size"] == 4)
    assert row["age"] == 8
    # Only one snapshot exists (today = D8), so only D8 is observed.
    observed = [(i, c["value"]) for i, c in enumerate(row["cells"]) if c["mature"]]
    assert len(observed) == 1
    day, value = observed[0]
    assert day == 8
    # 3 of 4 (Offer Review, Disbursement, Offer Generated) are at/past Offer Generated.
    assert value == 75.0


def test_cohort_fills_triangle_from_snapshot_history(db):
    """With drops on successive days the row fills in as a rising curve."""
    today = date.today()
    old = today - timedelta(days=3)  # cohort created 3 days ago
    d1 = old + timedelta(days=1)
    # Day 1 snapshot: A has reached Offer Generated, B has not yet.
    ingest.ingest_drop(db, _journey_csv([
        ("A", "Interested", old.isoformat(), "OFFER_GENERATED", ""),
        ("B", "Interested", old.isoformat(), "NOT_INTERESTED", ""),
    ]), filename="d1.csv", drop_date=d1)
    # Day 3 snapshot: B has now also reached Offer Generated.
    ingest.ingest_drop(db, _journey_csv([
        ("A", "Interested", old.isoformat(), "OFFER_GENERATED", ""),
        ("B", "Interested", old.isoformat(), "OFFER_GENERATED", ""),
    ]), filename="d3.csv", drop_date=today)

    row = next(r for r in analytics.cohort(db, "Offer Generated")["rows"] if r["size"] == 2)
    assert row["age"] == 3
    # D1 (first drop): 1 of 2 reached = 50%. D3 (second drop): 2 of 2 = 100%.
    assert row["cells"][1]["mature"] and row["cells"][1]["value"] == 50.0
    assert row["cells"][3]["mature"] and row["cells"][3]["value"] == 100.0
    # D0 and D2 had no snapshot -> un-observed.
    assert not row["cells"][0]["mature"]
    assert not row["cells"][2]["mature"]


def test_cohort_reach_is_at_or_past(db):
    today = date.today()
    old = today - timedelta(days=5)
    # A lead at "AA Initiated" counts as having reached the earlier "Offer Selected".
    ingest.ingest_drop(db, _journey_csv([
        ("A", "Interested", old.isoformat(), "AA_INITIATED", ""),
    ]), filename="j.csv", drop_date=today)
    row = next(r for r in analytics.cohort(db, "Offer Selected")["rows"] if r["size"] == 1)
    assert row["cells"][5]["value"] == 100.0  # observed at D5, reached


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
