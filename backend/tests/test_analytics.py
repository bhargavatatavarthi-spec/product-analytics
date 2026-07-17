"""Tests for the analytics engine."""
from __future__ import annotations

from datetime import date, timedelta

from app import analytics, ingest
from app.models import StageClassification


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
        ("B", "Interested", d, "APPLICATION_INITIATED", ""),
        ("C", "Not Eligible", d, "REJECTED", ""),
        ("D", "Interested", d, "OFFER_SELECTED", ""),
    ]), filename=f"j_{d}.csv", drop_date=today)

    ov = analytics.overview(db, "all")
    assert ov["entered"] == 4
    assert ov["buckets"]["won"]["count"] == 1       # Disbursement Completed
    assert ov["buckets"]["lost"]["count"] == 1       # Rejected
    assert ov["buckets"]["inflight"]["count"] == 2   # App Init + Offer Selected


def test_classification_override_moves_bucket(db):
    today = date.today()
    ingest.ingest_drop(db, _journey_csv([
        ("A", "Interested", today.isoformat(), "APPLICATION_ON_HOLD", ""),
    ]), filename="j.csv", drop_date=today)

    ov = analytics.overview(db, "all")
    assert ov["buckets"]["unclassified"]["count"] == 1  # On Hold defaults unclassified

    db.add(StageClassification(stage="Application On Hold", bucket="lost"))
    db.commit()
    ov2 = analytics.overview(db, "all")
    assert ov2["buckets"]["unclassified"]["count"] == 0
    assert ov2["buckets"]["lost"]["count"] == 1


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


def test_health_flags_and_completeness(db):
    today = date.today()
    ingest.ingest_drop(db, _journey_csv([
        ("A", "Interested", today.isoformat(), "DISBURSEMENT_COMPLETED", ""),  # won, zero disbursal
    ]), filename="j.csv", drop_date=today)
    hr = analytics.health(db)
    labels = {f["label"]: f["count"] for f in hr["flags"]}
    assert labels["Zero-value disbursals"] == "1"
    assert hr["completeness"] > 0


def test_indian_format():
    assert analytics.indian_format(1234567) == "12,34,567"
    assert analytics.indian_format(100) == "100"
    assert analytics.indian_format(4200) == "4,200"
