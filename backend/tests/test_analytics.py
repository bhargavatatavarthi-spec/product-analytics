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


def test_cohort_maturity(db):
    today = date.today()
    old = today - timedelta(days=10)
    # Lead entered 10 days ago, reached disbursal same day.
    ingest.ingest_drop(db, _journey_csv([
        ("A", "Interested", old.isoformat(), "DISBURSEMENT_COMPLETED", "100000"),
    ]), filename="j.csv", drop_date=today)

    co = analytics.cohort(db, "Disbursement Completed")
    assert len(co["rows"]) == 14
    # The oldest cohort row (10 days old) should have some mature cells.
    row = next(r for r in co["rows"] if r["size"] > 0)
    assert any(c["mature"] for c in row["cells"])


def test_cohort_from_milestone_dates(db):
    from app import ingest
    today = date.today()
    entry = today - timedelta(days=10)
    e = entry.isoformat()
    # Journey feed with an explicit Disbursement Date column. Three leads in one
    # cohort reach disbursal at day 0, 3 and 6 after entry; one never reaches.
    csv = (
        "INTERNAL_ID,DIY Sub-Stage,Created Date,Disbursement Date\n"
        f"A,DISBURSEMENT_COMPLETED,{e},{entry.isoformat()}\n"
        f"B,DISBURSEMENT_COMPLETED,{e},{(entry + timedelta(days=3)).isoformat()}\n"
        f"C,DISBURSEMENT_COMPLETED,{e},{(entry + timedelta(days=6)).isoformat()}\n"
        f"D,OFFER_GENERATED,{e},#N/A\n"
    ).encode()
    r = ingest.ingest_drop(db, csv, filename="j.csv", drop_date=today)
    assert "disbursement_on" in r["milestone_dates_detected"]

    co = analytics.cohort(db, "Disbursement Completed")
    assert co["milestone_dated"] is True
    row = next(r for r in co["rows"] if r["size"] == 4)  # the 4-lead cohort
    cells = row["cells"]
    # Curve climbs: 25% by D0, 50% by D3, 75% by D6 (D never reaches).
    assert cells[0]["value"] == 25.0
    assert cells[3]["value"] == 50.0
    assert cells[6]["value"] == 75.0
    assert cells[9]["value"] == 75.0  # plateaus


def test_cohort_reports_missing_dates(db):
    from app import ingest
    today = date.today()
    ingest.ingest_drop(db, (
        "INTERNAL_ID,DIY Sub-Stage,Created Date\n"
        f"A,OFFER_SELECTED,{(today - timedelta(days=5)).isoformat()}\n"
    ).encode(), filename="j.csv", drop_date=today)
    # No offer-selected date column -> that cohort flags as undated.
    assert analytics.cohort(db, "Offer Selected")["milestone_dated"] is False


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
