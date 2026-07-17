"""Synthetic demo-data generator.

Produces a realistic multi-day sequence of daily drops and feeds them through
the *real* import pipeline (`ingest.ingest_drop`). This doubles as an
end-to-end test of import + reconstruction, and it makes a fresh deploy show a
populated dashboard that mirrors the original Kotak PAL prototype.

Deterministic: uses a fixed RNG seed so the demo numbers are stable.
"""
from __future__ import annotations

import csv
import io
import random
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import catalog, ingest
from .models import Lead

# Terminal outcome distribution for a lead's *final* stage (won/lost/inflight mix
# roughly matching the original 8% / 61% / 31% prototype split).
_LOST_STAGES = ["Application Rejected", "Application Dropped", "Upgrade Offer Declined", "Upgrade Offer Not Eligible"]
_INFLIGHT_STAGES = [s["name"] for s in catalog.STAGE_CATALOG if s["bucket"] == "inflight"]
_DISPOSITIONS = [
    ("Phone Not Answered", 19.0), ("Not Interested", 14.5), ("Number Not Reachable", 8.0),
    ("Already Applied", 7.5), ("Interested", 7.0), ("Not Eligible", 6.0), ("Will do it later", 5.0),
    ("Phone Busy", 4.5), ("Already Spoken", 4.4), ("Call Rescheduled", 4.0),
    ("Already took loan from other lender", 3.3), ("Rejected", 3.0), ("Voicemail", 2.8),
    ("Language Issue", 2.4), ("Wrong Number", 2.2), ("DNC Client : Don't Call Further", 2.0),
    ("Other Cases", 1.9), ("Abruptly disconnected call", 1.7), ("App Issue", 0.8),
]
_SCHEMES = ["KPAL-PL-STD", "KPAL-PL-PREM", "KPAL-BL-STD", "KPAL-TOPUP", "KPAL-TW"]
# Happy-path progression the majority of leads walk before terminating.
# Ordered to match catalog.STAGE_ORDER so it never produces false backward moves.
_PROGRESSION = [
    "Offer Generated", "Offer Selected", "Offer Accepted", "AA Initiated",
    "Employment Details", "Repayment Setup Completed", "Disbursement Initiated",
    "Disbursement Completed",
]


def _weighted(rng: random.Random, pairs: list[tuple[str, float]]) -> str:
    total = sum(w for _, w in pairs)
    x = rng.uniform(0, total)
    acc = 0.0
    for name, w in pairs:
        acc += w
        if x <= acc:
            return name
    return pairs[-1][0]


def _build_leads(rng: random.Random, num_leads: int, first_entry: date, span_days: int) -> list[dict]:
    """Create lead life-stories: entry date, a target outcome, and a per-day timeline."""
    leads = []
    for i in range(num_leads):
        entry = first_entry + timedelta(days=rng.randint(0, span_days - 1))
        roll = rng.random()
        if roll < 0.085:
            outcome = "won"
        elif roll < 0.085 + 0.31:
            outcome = "lost"
        else:
            outcome = "inflight"

        loan = rng.choice([80_000, 150_000, 250_000, 400_000, 750_000, 1_200_000, 1_800_000])
        tenure = rng.choice([12, 24, 36, 48, 60])
        roi = round(rng.uniform(10.5, 16.5), 1)
        scheme = rng.choice(_SCHEMES)
        voice = rng.random() < 0.63  # ~voice-touched share
        calls = rng.randint(1, 6) if voice else rng.randint(0, 2)
        disposition = _weighted(rng, _DISPOSITIONS)

        # Timeline of (day_offset_from_entry, stage).
        timeline: list[tuple[int, str]] = []
        day = 0
        if outcome == "won":
            path = _PROGRESSION
        elif outcome == "lost":
            depth = rng.randint(1, 4)
            path = _PROGRESSION[:depth] + [rng.choice(_LOST_STAGES)]
        else:  # inflight: stop partway, sometimes on a side branch
            depth = rng.randint(1, len(_PROGRESSION) - 1)
            path = _PROGRESSION[:depth]
            if rng.random() < 0.25:
                path = path + [rng.choice(["Application On Hold", "Upgrade Offer Progress"])]
        for stage in path:
            timeline.append((day, stage))
            day += rng.choice([0, 1, 1, 2, 2, 3, 5])
        # A small, genuine minority regress to an earlier stage (real backward move).
        if outcome == "inflight" and len(path) >= 3 and rng.random() < 0.05:
            timeline.append((day + rng.choice([1, 2]), path[rng.randint(0, len(path) - 2)]))
        # A few won leads land with no disbursed amount recorded (data-quality flag).
        zero_disbursed = outcome == "won" and rng.random() < 0.10
        leads.append(
            {
                "lead_id": f"KPAL{100000 + i}",
                "entry": entry,
                "loan": loan,
                "tenure": tenure,
                "roi": roi,
                "scheme": scheme,
                "voice": voice,
                "calls": calls,
                "disposition": disposition,
                "timeline": timeline,
                "zero_disbursed": zero_disbursed,
            }
        )
    return leads


def _stage_on(lead: dict, d: date) -> str | None:
    """The stage a lead is in on calendar date d (None if not entered yet)."""
    if d < lead["entry"]:
        return None
    offset = (d - lead["entry"]).days
    current = None
    for day_off, stage in lead["timeline"]:
        if day_off <= offset:
            current = stage
        else:
            break
    return current


def _drop_csv(leads: list[dict], d: date) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        "lead_id", "drop_date", "entry_date", "stage", "max_loan_amount",
        "max_tenure_months", "roi", "schemecode", "disbursed_amount",
        "voice_connected", "call_count", "last_disposition",
    ])
    for lead in leads:
        stage = _stage_on(lead, d)
        if stage is None:
            continue
        disbursed = ""
        if stage == "Disbursement Completed" and not lead["zero_disbursed"]:
            disbursed = lead["loan"]
        w.writerow([
            lead["lead_id"], d.isoformat(), lead["entry"].isoformat(), stage,
            lead["loan"], lead["tenure"], lead["roi"], lead["scheme"], disbursed,
            "yes" if lead["voice"] else "no", lead["calls"], lead["disposition"],
        ])
    return buf.getvalue().encode()


def seed_demo(db: Session, num_leads: int = 4200, drops: int = 30, missing: tuple[int, ...] = (8, 21)) -> dict:
    """Generate `drops` daily drops ending today and ingest them. `missing` are
    indices (0=oldest) deliberately skipped to exercise Data-Health gaps."""
    if db.execute(select(func.count(Lead.id))).scalar_one() > 0:
        return {"skipped": True, "reason": "data already present"}

    rng = random.Random(42)
    today = date.today()
    first_drop = today - timedelta(days=drops - 1)
    # Leads may enter a little before the window so early cohorts have history.
    all_leads = _build_leads(rng, num_leads, first_drop - timedelta(days=6), drops + 6)

    imported = 0
    for i in range(drops):
        if i in missing:
            continue
        d = first_drop + timedelta(days=i)
        csv_bytes = _drop_csv(all_leads, d)
        # Inject a few genuine cell errors on one partial day to exercise the
        # data-quality flag (blank / #N/A are normal nulls and not flagged).
        if i == 17:
            csv_bytes = csv_bytes.replace(_SCHEMES[0].encode(), b"#VALUE!", 3)
        ingest.ingest_drop(db, csv_bytes, filename=f"kotak_pal_drop_{d.isoformat()}.csv")
        imported += 1

    total = db.execute(select(func.count(Lead.id))).scalar_one()
    return {"skipped": False, "drops_imported": imported, "total_leads": total}
