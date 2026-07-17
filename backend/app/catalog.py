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

# order: progression rank along the happy path. Higher == further along.
# Side branches (callbacks, reuploads, holds) and terminal Lost states carry
# order=None: they are not points on the linear milestone ladder.
STAGE_CATALOG: list[dict] = [
    {"name": "Offer Generated", "bucket": "inflight", "order": 10},
    {"name": "Offer Review", "bucket": "inflight", "order": 15},
    {"name": "Offer Selected", "bucket": "inflight", "order": 20},
    {"name": "Income Assessment", "bucket": "inflight", "order": 25},
    {"name": "Application Initiated", "bucket": "inflight", "order": 30},
    {"name": "Employment Verification", "bucket": "inflight", "order": 35},
    {"name": "Bank Statement Upload", "bucket": "inflight", "order": 40},
    {"name": "Address Verification", "bucket": "inflight", "order": 45},
    {"name": "FI Consent Collection", "bucket": "inflight", "order": 50},
    {"name": "Reference Check", "bucket": "inflight", "order": 55},
    {"name": "E-Mandate Setup", "bucket": "inflight", "order": 60},
    {"name": "E-Sign Pending", "bucket": "inflight", "order": 65},
    {"name": "Disbursal Initiated", "bucket": "inflight", "order": 70},
    {"name": "Disbursement Completed", "bucket": "won", "order": 100},
    # Side branches — genuinely in-flight but off the linear ladder.
    {"name": "Callback Scheduled", "bucket": "inflight", "order": None},
    {"name": "Document Reupload", "bucket": "inflight", "order": None},
    {"name": "Offer Upgrade Review", "bucket": "inflight", "order": None},
    # Ambiguous states left Unclassified on purpose — an analyst must decide
    # whether these hide dead leads as pipeline.
    {"name": "Application On Hold", "bucket": "unclassified", "order": None},
    {"name": "FI Consent Collection Failed", "bucket": "unclassified", "order": None},
    # Terminal Lost states.
    {"name": "Rejected", "bucket": "lost", "order": None},
    {"name": "Not Eligible", "bucket": "lost", "order": None},
    {"name": "Offer Declined", "bucket": "lost", "order": None},
    {"name": "Dropped", "bucket": "lost", "order": None},
    {"name": "KYC Failed", "bucket": "lost", "order": None},
    {"name": "Upgrade Offer Not Eligible", "bucket": "lost", "order": None},
]

STAGE_ORDER: dict[str, int | None] = {s["name"]: s["order"] for s in STAGE_CATALOG}
DEFAULT_BUCKET: dict[str, str] = {s["name"]: s["bucket"] for s in STAGE_CATALOG}

# Cohort-Triangle milestones, ordered from earliest to latest along the ladder.
MILESTONES: list[dict] = [
    {"key": "offer_generated", "label": "Offer Generated", "short": "Offer Gen.", "order": 10},
    {"key": "offer_selected", "label": "Offer Selected", "short": "Offer Sel.", "order": 20},
    {"key": "application_initiated", "label": "Application Initiated", "short": "App. Init.", "order": 30},
    {"key": "disbursement_completed", "label": "Disbursement Completed", "short": "Disbursal", "order": 100},
]
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
