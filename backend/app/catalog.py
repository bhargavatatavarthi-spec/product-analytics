"""Domain catalog for the Kotak PAL lending journey.

This encodes what the product *knows* about the journey independent of any
imported data: the canonical ordering of sub-stages, their default bucket
classification (Won / In-flight / Lost / Unclassified), the milestone
definitions used by the Cohort Triangle, and the connected/not-connected map
for call dispositions.

Stage classifications are only *defaults* — the Stage Explorer lets an analyst
override any stage, and those overrides are persisted in the DB.
"""
from __future__ import annotations

BUCKETS = ("won", "inflight", "lost", "unclassified")

# Sentinel stage for leads present in the journey feed but with no DIY sub-stage
# (Sub-Stage = #N/A) — dialed leads that never entered the offer journey.
NOT_IN_JOURNEY = "Not in DIY Journey"

# order: progression rank along the happy path. Higher == further along.
# Side branches (upgrade track, holds) and terminal Lost states carry order=None:
# they are not points on the linear milestone ladder.
#
# Stage names mirror the real Kotak "DIY Sub-Stage" vocabulary
# (OFFER_GENERATED, AA_INITIATED, DISBURSEMENT_COMPLETED, ...) after
# normalization to Title Case.
STAGE_CATALOG: list[dict] = [
    {"name": "Offer Generated", "bucket": "inflight", "order": 10},
    {"name": "Offer Review", "bucket": "inflight", "order": 15},
    {"name": "Offer Selected", "bucket": "inflight", "order": 20},
    {"name": "Offer Accepted", "bucket": "inflight", "order": 25},
    {"name": "AA Initiated", "bucket": "inflight", "order": 30},
    {"name": "Employment Details", "bucket": "inflight", "order": 35},
    {"name": "Repayment Setup Completed", "bucket": "inflight", "order": 60},
    {"name": "Disbursement Initiated", "bucket": "inflight", "order": 70},
    {"name": "Disbursement Completed", "bucket": "won", "order": 100},
    # Terminal Lost states.
    {"name": "Application Rejected", "bucket": "lost", "order": None},
    {"name": "Application Dropped", "bucket": "lost", "order": None},
    # Upgrade / top-up track (existing borrowers offered a further loan).
    {"name": "Upgrade Offer Generated", "bucket": "inflight", "order": None},
    {"name": "Upgrade Offer Review", "bucket": "inflight", "order": None},
    {"name": "Upgrade Offer Selected", "bucket": "inflight", "order": None},
    {"name": "Upgrade Offer Progress", "bucket": "inflight", "order": None},
    {"name": "Upgrade Offer Declined", "bucket": "lost", "order": None},
    {"name": "Upgrade Offer Not Eligible", "bucket": "lost", "order": None},
    # Ambiguous states left Unclassified on purpose — an analyst must decide
    # whether these hide dead leads as pipeline.
    {"name": "Application On Hold", "bucket": "unclassified", "order": None},
    {"name": "FI Consent Collection Failed", "bucket": "unclassified", "order": None},
    {"name": NOT_IN_JOURNEY, "bucket": "unclassified", "order": None},
]

STAGE_ORDER: dict[str, int | None] = {s["name"]: s["order"] for s in STAGE_CATALOG}
DEFAULT_BUCKET: dict[str, str] = {s["name"]: s["bucket"] for s in STAGE_CATALOG}

# Cohort-Triangle milestones, ordered from earliest to latest along the ladder.
# `date_field` is the Lead column holding that milestone's real date (from the
# feed), used to compute cohort reach as milestone_date − entry_date.
MILESTONES: list[dict] = [
    {"key": "offer_generated", "label": "Offer Generated", "short": "Offer Gen.", "order": 10, "date_field": "offer_generated_on"},
    {"key": "offer_selected", "label": "Offer Selected", "short": "Offer Sel.", "order": 20, "date_field": "offer_selected_on"},
    {"key": "aa_initiated", "label": "AA Initiated", "short": "AA Init.", "order": 30, "date_field": "aa_initiated_on"},
    {"key": "disbursement_completed", "label": "Disbursement Completed", "short": "Disbursal", "order": 100, "date_field": "disbursement_on"},
]
MILESTONE_DATE_FIELD = {m["label"]: m["date_field"] for m in MILESTONES}
MILESTONE_ORDER: dict[str, int] = {m["label"]: m["order"] for m in MILESTONES}
DEFAULT_MILESTONE = "Disbursement Completed"

# Cohort-window presets. `days=None` means "all time".
RANGES: list[dict] = [
    {"key": "7d", "label": "7d", "full": "Last 7 days", "days": 7},
    {"key": "30d", "label": "30d", "full": "Last 30 days", "days": 30},
    {"key": "90d", "label": "90d", "full": "Last 90 days", "days": 90},
    {"key": "all", "label": "All", "full": "All time", "days": None},
]
RANGE_DAYS: dict[str, int | None] = {r["key"]: r["days"] for r in RANGES}

# Aging-bucket definition for In-flight leads (min days-in-stage, inclusive).
AGING_BUCKETS: list[dict] = [
    {"label": "0–2 d", "min": 0, "max": 2},
    {"label": "3–6 d", "min": 3, "max": 6},
    {"label": "7–13 d", "min": 7, "max": 13},
    {"label": "14–20 d", "min": 14, "max": 20},
    {"label": "21–27 d", "min": 21, "max": 27},
    {"label": "28+ d", "min": 28, "max": None},
]

# Call dispositions and whether they mean the lead reached a human.
# Anything not listed defaults to connected=True (a real conversation outcome).
DISPOSITION_CONNECTED: dict[str, bool] = {
    "Phone Not Answered": False,
    "Number Not Reachable": False,
    "Phone Busy": False,
    "Voicemail": False,
    "Wrong Number": False,
}

# Offer-metadata attribution dimensions. Each maps a numeric/string lead field
# to labelled buckets. `field` is the canonical column name.
ATTR_DIMENSIONS: list[dict] = [
    {
        "key": "amount",
        "label": "Max loan amount",
        "field": "max_loan_amount",
        "kind": "numeric",
        "bins": [
            {"name": "Under ₹1L", "lo": 0, "hi": 100_000},
            {"name": "₹1L – ₹3L", "lo": 100_000, "hi": 300_000},
            {"name": "₹3L – ₹5L", "lo": 300_000, "hi": 500_000},
            {"name": "₹5L – ₹10L", "lo": 500_000, "hi": 1_000_000},
            {"name": "₹10L and above", "lo": 1_000_000, "hi": None},
        ],
    },
    {
        "key": "tenure",
        "label": "Tenure",
        "field": "max_tenure_months",
        "kind": "numeric",
        "bins": [
            {"name": "12 months", "lo": 0, "hi": 12},
            {"name": "24 months", "lo": 13, "hi": 24},
            {"name": "36 months", "lo": 25, "hi": 36},
            {"name": "48 months", "lo": 37, "hi": 48},
            {"name": "60 months", "lo": 49, "hi": None},
        ],
    },
    {
        "key": "roi",
        "label": "ROI",
        "field": "roi",
        "kind": "numeric",
        "bins": [
            {"name": "Under 11%", "lo": 0, "hi": 11},
            {"name": "11% – 13%", "lo": 11, "hi": 13},
            {"name": "13% – 15%", "lo": 13, "hi": 15},
            {"name": "15% and above", "lo": 15, "hi": None},
        ],
    },
    {
        "key": "emi",
        "label": "EMI",
        "field": "emi",
        "kind": "numeric",
        "bins": [
            {"name": "Under ₹5k", "lo": 0, "hi": 5000},
            {"name": "₹5k – ₹10k", "lo": 5000, "hi": 10000},
            {"name": "₹10k – ₹20k", "lo": 10000, "hi": 20000},
            {"name": "₹20k – ₹40k", "lo": 20000, "hi": 40000},
            {"name": "₹40k and above", "lo": 40000, "hi": None},
        ],
    },
    {
        "key": "scheme",
        "label": "Scheme code",
        "field": "schemecode",
        "kind": "categorical",
    },
]
ATTR_DIMENSION_BY_KEY = {d["key"]: d for d in ATTR_DIMENSIONS}


def default_bucket_for(stage_name: str) -> str:
    """Best-guess bucket for a stage the catalog has never seen -> unclassified."""
    return DEFAULT_BUCKET.get(stage_name, "unclassified")


def is_connected(disposition: str | None) -> bool:
    if not disposition:
        return False
    return DISPOSITION_CONNECTED.get(disposition.strip(), True)
