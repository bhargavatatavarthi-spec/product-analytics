"""Pre-filter the daily feeds down to *engaged* leads for a fast import.

Keeps a lead if it was connected at least once OR is on the DIY journey (has a
real sub-stage). Drops only leads that are both never-connected AND not on the
journey — the top-of-funnel dial noise (~75% of rows). This preserves organic
(never-connected) disbursals, which the attribution split needs.

Also strips PII (name / mobile) from the offer feed on the way out.

Usage:
    python scripts/filter_engaged.py JOURNEY.csv OFFER.csv OUT_DIR
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "backend"))
from app.ingest import is_na, coerce_bool, _norm  # noqa: E402

PII = {"name", "mobile", "phone", "email", "customer_name"}


def _col(fieldnames, *aliases):
    norm = {_norm(f): f for f in fieldnames}
    for a in aliases:
        if _norm(a) in norm:
            return norm[_norm(a)]
    return None


def filter_journey(path: Path, out: Path) -> set[str]:
    """Write engaged journey rows; return the set of engaged lead ids."""
    engaged: set[str] = set()
    with open(path, newline="", encoding="utf-8-sig") as f, open(out, "w", newline="") as o:
        reader = csv.DictReader(f)
        fn = reader.fieldnames or []
        id_c = _col(fn, "internal_id", "offer_id", "lead_id")
        conn_c = _col(fn, "connected_at_least_once", "connected")
        stage_c = _col(fn, "diy_sub_stage", "sub_stage", "stage")
        writer = csv.DictWriter(o, fieldnames=fn)
        writer.writeheader()
        total = kept = 0
        for row in reader:
            total += 1
            connected = coerce_bool(row.get(conn_c)) if conn_c else False
            on_journey = stage_c is not None and not is_na(row.get(stage_c))
            if connected or on_journey:
                writer.writerow(row)
                engaged.add((row.get(id_c) or "").strip())
                kept += 1
        print(f"journey: kept {kept:,} / {total:,} ({100*kept/max(total,1):.1f}%)")
    return engaged


def filter_offer(path: Path, out: Path, engaged: set[str]) -> None:
    """Write offer rows for engaged leads only, with PII columns removed."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fn = reader.fieldnames or []
        id_c = _col(fn, "internal_id", "offer_id", "lead_id")
        keep_cols = [c for c in fn if _norm(c) not in PII]
        with open(out, "w", newline="") as o:
            writer = csv.DictWriter(o, fieldnames=keep_cols, extrasaction="ignore")
            writer.writeheader()
            total = kept = 0
            for row in reader:
                total += 1
                if (row.get(id_c) or "").strip() in engaged:
                    writer.writerow(row)
                    kept += 1
        print(f"offer:   kept {kept:,} / {total:,} ({100*kept/max(total,1):.1f}%)  [PII columns dropped]")


def main() -> None:
    journey, offer, out_dir = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    out_dir.mkdir(parents=True, exist_ok=True)
    j_out = out_dir / "journey_engaged.csv"
    o_out = out_dir / "offer_engaged.csv"
    engaged = filter_journey(journey, j_out)
    filter_offer(offer, o_out, engaged)
    for p in (j_out, o_out):
        print(f"wrote {p}  ({p.stat().st_size/1_048_576:.1f} MB)")


if __name__ == "__main__":
    main()
