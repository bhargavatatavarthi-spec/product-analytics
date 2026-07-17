"""Tests for the manual data-import pipeline."""
from __future__ import annotations

from datetime import date

from sqlalchemy import select

from app import ingest
from app.models import Lead

OFFER_CSV = (
    "offerid,name,mobile,max_loan_amount,max_tenure_months,roi,EMI,processing_fee,schemecode\n"
    "OFF1,Asha,98x,500000,24,12.5,23500,2500,KPAL-PL-STD\n"
    "OFF2,Vik,98y,300000,36,13.0,10100,1800,KPAL-PL-PREM\n"
)
JOURNEY_CSV = (
    "offer_id,last_call_outcome,Created Date,DIY Sub-stage,DIS VALUE\n"
    "OFF1,Abruptly disconnected call,6/18/2026,DISBURSEMENT_COMPLETED,500000\n"
    "OFF2,Phone Not Answered,6/15/2026,OFFER_SELECTED,\n"
)


def test_suggest_mapping_matches_real_headers():
    headers = ["offer_id", "last_call_outcome", "Created Date", "DIY Sub-stage", "DIS VALUE"]
    m = ingest.suggest_mapping(headers)
    assert m["lead_id"] == "offer_id"
    assert m["stage"] == "DIY Sub-stage"
    assert m["entry_date"] == "Created Date"
    assert m["disbursed_amount"] == "DIS VALUE"
    assert m["last_disposition"] == "last_call_outcome"


def test_detect_dayfirst():
    assert ingest.detect_dayfirst(["6/18/2026", "6/15/2026"]) is False  # month-first
    assert ingest.detect_dayfirst(["18/06/2026", "15/06/2026"]) is True  # day-first
    assert ingest.detect_dayfirst(["05/06/2026"]) is True  # ambiguous -> default day-first


def test_iso_date_not_swapped():
    # dayfirst must not corrupt ISO YYYY-MM-DD.
    assert ingest.coerce_date("2026-07-12") == date(2026, 7, 12)


def test_stage_normalization():
    assert ingest.normalize_stage("DISBURSEMENT_COMPLETED") == "Disbursement Completed"
    assert ingest.normalize_stage("offer selected") == "Offer Selected"
    assert ingest.normalize_stage("Totally New Stage") == "Totally New Stage"


def test_two_feed_join(db):
    # Offer feed first: creates leads at the default stage.
    r1 = ingest.ingest_drop(db, OFFER_CSV.encode(), filename="offer_2026-06-18.csv")
    assert r1["new_leads"] == 2
    leads = {l.lead_id: l for l in db.execute(select(Lead)).scalars()}
    assert leads["OFF1"].current_stage == ingest.DEFAULT_STAGE
    assert leads["OFF1"].max_loan_amount == 500000
    assert leads["OFF1"].emi == 23500

    # Journey feed: joins on offer_id, sets real stage + entry + disbursal.
    r2 = ingest.ingest_drop(db, JOURNEY_CSV.encode(), filename="journey_2026-06-18.csv")
    assert r2["new_leads"] == 0 and r2["updated_leads"] == 2
    db.expire_all()
    leads = {l.lead_id: l for l in db.execute(select(Lead)).scalars()}
    assert leads["OFF1"].current_stage == "Disbursement Completed"
    assert leads["OFF1"].entry_date == date(2026, 6, 18)
    assert leads["OFF1"].disbursed_amount == 500000
    assert leads["OFF1"].voice_connected is True  # connected disposition
    assert leads["OFF2"].current_stage == "Offer Selected"
    assert leads["OFF2"].voice_connected is False  # Phone Not Answered


def test_no_pii_stored(db):
    ingest.ingest_drop(db, OFFER_CSV.encode(), filename="offer.csv")
    # name / mobile are not columns on the model at all.
    assert not hasattr(Lead, "name")
    assert not hasattr(Lead, "mobile")


def test_missing_lead_id_raises(db):
    bad = "foo,bar\n1,2\n"
    try:
        ingest.ingest_drop(db, bad.encode(), filename="bad.csv")
        assert False, "should have raised"
    except ValueError as e:
        assert "lead_id" in str(e)


def test_idempotent_reimport(db):
    ingest.ingest_drop(db, JOURNEY_CSV.encode(), filename="j.csv", drop_date=date(2026, 6, 18))
    n1 = db.execute(select(Lead)).scalars().all()
    ingest.ingest_drop(db, JOURNEY_CSV.encode(), filename="j.csv", drop_date=date(2026, 6, 18))
    n2 = db.execute(select(Lead)).scalars().all()
    assert len(n1) == len(n2) == 2  # no duplicates


def test_error_token_flagged_not_blank(db):
    csv = (
        "offer_id,DIY Sub-stage,schemecode\n"
        "A,OFFER_SELECTED,#N/A\n"      # #N/A -> flagged
        "B,OFFER_SELECTED,\n"          # blank -> not flagged
    )
    ingest.ingest_drop(db, csv.encode(), filename="x.csv")
    leads = {l.lead_id: l for l in db.execute(select(Lead)).scalars()}
    assert leads["A"].na_cells == 1
    assert leads["B"].na_cells == 0
