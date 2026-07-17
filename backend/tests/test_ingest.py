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


def test_real_kotak_journey_headers():
    # Exact headers from the real 17TH_JULY_SS.csv export.
    headers = ["INTERNAL_ID", "LAST_CALL_OUTCOME", "CONNECTED_AT_LEAST_ONCE",
               "DIY Sub-Stage", "Disbursement Amount", "Created Date"]
    m = ingest.suggest_mapping(headers)
    assert m["lead_id"] == "INTERNAL_ID"
    assert m["voice_connected"] == "CONNECTED_AT_LEAST_ONCE"
    assert m["stage"] == "DIY Sub-Stage"
    assert m["disbursed_amount"] == "Disbursement Amount"
    assert m["entry_date"] == "Created Date"
    assert m["last_disposition"] == "LAST_CALL_OUTCOME"


def test_na_substage_becomes_sentinel(db):
    from app import catalog
    csv = (
        "INTERNAL_ID,LAST_CALL_OUTCOME,CONNECTED_AT_LEAST_ONCE,DIY Sub-Stage,Disbursement Amount,Created Date\n"
        "u1,Not Interested,Yes,#N/A,#N/A,#N/A\n"
        "u2,Phone Busy,No,DISBURSEMENT_COMPLETED,25000,14-07-2026\n"
    )
    ingest.ingest_drop(db, csv.encode(), filename="17TH_JULY_SS.csv", drop_date=date(2026, 7, 17))
    leads = {l.lead_id: l for l in db.execute(select(Lead)).scalars()}
    assert leads["u1"].current_stage == catalog.NOT_IN_JOURNEY  # dialed, no journey
    assert leads["u1"].voice_connected is True                   # CONNECTED = Yes
    assert leads["u2"].current_stage == "Disbursement Completed"
    assert leads["u2"].voice_connected is False                  # CONNECTED = No
    assert leads["u2"].entry_date == date(2026, 7, 14)           # DD-MM-YYYY
    # #N/A is a null token, not a flagged cell error.
    assert leads["u1"].na_cells == 0


def test_real_offer_headers_and_roi_fraction():
    headers = ["name", "max_loan_amount", "max_tenure_months", "processing_fee",
               "scheme_id", "internal_id", "roi"]
    m = ingest.suggest_mapping(headers)
    assert m["lead_id"] == "internal_id"
    assert m["schemecode"] == "scheme_id"
    assert m["stage"] is None  # offer feed has no stage
    # ROI stored as a fraction in the offer feed -> normalized to percent.
    assert ingest.coerce_roi("0.115") == 11.5
    assert ingest.coerce_roi("11.5") == 11.5


def test_offer_feed_joins_onto_journey_lead(db):
    from sqlalchemy import select
    j = ("INTERNAL_ID,LAST_CALL_OUTCOME,CONNECTED_AT_LEAST_ONCE,DIY Sub-Stage,Disbursement Amount,Created Date\n"
         "X1,Interested,Yes,DISBURSEMENT_COMPLETED,250000,14-07-2026\n")
    o = ("name,max_loan_amount,max_tenure_months,processing_fee,scheme_id,internal_id,roi\n"
         "REDACTED,250000,24,0.02,11888,X1,0.125\n")
    ingest.ingest_drop(db, j.encode(), filename="j.csv", drop_date=date(2026, 7, 17))
    r = ingest.ingest_drop(db, o.encode(), filename="o.csv", drop_date=date(2026, 7, 17))
    assert r["new_leads"] == 0 and r["updated_leads"] == 1  # joined, not duplicated
    lead = db.execute(select(Lead).where(Lead.lead_id == "X1")).scalar_one()
    assert lead.current_stage == "Disbursement Completed"  # from journey feed
    assert lead.max_loan_amount == 250000                  # from offer feed
    assert lead.roi == 12.5                                # fraction -> percent
    assert lead.schemecode == "11888"


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


def test_error_token_flagged_not_blank_or_na(db):
    csv = (
        "offer_id,DIY Sub-stage,schemecode\n"
        "A,OFFER_SELECTED,#VALUE!\n"    # genuine cell error -> flagged
        "B,OFFER_SELECTED,#N/A\n"       # client null token -> not flagged
        "C,OFFER_SELECTED,\n"           # blank -> not flagged
    )
    ingest.ingest_drop(db, csv.encode(), filename="x.csv")
    leads = {l.lead_id: l for l in db.execute(select(Lead)).scalars()}
    assert leads["A"].na_cells == 1
    assert leads["B"].na_cells == 0
    assert leads["C"].na_cells == 0
