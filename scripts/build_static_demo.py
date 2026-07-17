"""Build a self-contained, read-only HTML snapshot of the dashboard.

Imports the given daily feeds, precomputes every API response the frontend
requests, and inlines the CSS + app.js + data into a single HTML file that runs
with no backend (a `fetch` shim serves the embedded bundle). Handy for sharing a
live, clickable view of a specific dataset.

Usage:
    python scripts/build_static_demo.py OUT.html FEED1.csv [FEED2.csv ...]
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "backend"))

import os
os.environ["KPAL_SEED_DEMO"] = "0"
os.environ["KPAL_DATA_DIR"] = tempfile.mkdtemp(prefix="kpal_demo_")

from app.db import SessionLocal, init_db          # noqa: E402
from app import analytics, catalog, ingest         # noqa: E402
from app.models import DailyDrop, Lead             # noqa: E402
from sqlalchemy import func, select                # noqa: E402


def build_bundle(db) -> dict:
    ranges = [r["key"] for r in catalog.RANGES]
    dims = [d["key"] for d in catalog.ATTR_DIMENSIONS]
    filters = ["all", "unclassified", "won", "inflight", "lost"]
    milestones = [m["label"] for m in catalog.MILESTONES]

    bundle: dict[str, object] = {}
    bundle["GET /api/meta"] = {
        "ranges": catalog.RANGES,
        "milestones": catalog.MILESTONES,
        "dimensions": [{"key": d["key"], "label": d["label"], "field": d["field"]} for d in catalog.ATTR_DIMENSIONS],
        "buckets": [
            {"key": "won", "label": "Won", "color": "#6F39F5"},
            {"key": "inflight", "label": "In-flight", "color": "#191132"},
            {"key": "lost", "label": "Lost", "color": "#8A8595"},
            {"key": "unclassified", "label": "Unclassified", "color": "#6F39F5"},
        ],
        "settings": analytics.get_settings(db),
        "summaries": analytics.range_summaries(db),
        "has_data": analytics.has_data(db),
    }
    for r in ranges:
        bundle[f"GET /api/overview?range={r}"] = analytics.overview(db, r)
        for f in filters:
            bundle[f"GET /api/stages?range={r}&filter={f}"] = analytics.stages(db, r, f)
        for d in dims:
            bundle[f"GET /api/attribution?range={r}&dim={d}"] = analytics.attribution(db, r, d)
    for m in milestones:
        bundle[f"GET /api/cohort?milestone={quote(m, safe='')}"] = analytics.cohort(db, m)
    bundle["GET /api/health-report"] = analytics.health(db)

    drops = db.execute(select(DailyDrop).order_by(DailyDrop.drop_date.desc())).scalars()
    bundle["GET /api/import/drops"] = {
        "total_leads": db.execute(select(func.count(Lead.id))).scalar_one(),
        "drops": [
            {"drop_date": d.drop_date.isoformat(), "filename": d.filename, "row_count": d.row_count,
             "error_rows": d.error_rows, "status": d.status,
             "imported_at": d.imported_at.isoformat() if d.imported_at else None}
            for d in drops
        ],
    }
    return bundle


def assemble_html(bundle: dict) -> str:
    assets = REPO / "frontend" / "assets"
    colors = (assets / "colors_and_type.css").read_text()
    # Drop the external @import (blocked under the artifact CSP); system font is fine.
    colors = "\n".join(l for l in colors.splitlines() if not l.strip().startswith("@import"))
    styles = (assets / "styles.css").read_text()
    app_js = (assets / "app.js").read_text()
    index = (REPO / "frontend" / "index.html").read_text()
    body_inner = index.split("<body>", 1)[1].split("<script src", 1)[0]

    shim = (
        "const KPAL_BUNDLE = " + json.dumps(bundle, ensure_ascii=False) + ";\n"
        "const _RO = 'Read-only snapshot — deploy the backend for live import & edits.';\n"
        "window.fetch = async (path, opts) => {\n"
        "  const method = ((opts && opts.method) || 'GET').toUpperCase();\n"
        "  const key = method + ' ' + path;\n"
        "  if (key in KPAL_BUNDLE) return { ok: true, json: async () => KPAL_BUNDLE[key] };\n"
        "  return { ok: false, status: 403, statusText: _RO, json: async () => ({ detail: _RO }) };\n"
        "};\n"
    )
    return (
        f"<style>{colors}</style>\n<style>{styles}</style>\n"
        f'<div class="ss-demo-banner">Live snapshot · read-only · real imported data</div>\n'
        "<style>.ss-demo-banner{position:fixed;bottom:12px;left:50%;transform:translateX(-50%);"
        "z-index:200;background:var(--ss-darkmatter);color:#fff;font-family:var(--ss-font);"
        "font-size:11px;font-weight:700;letter-spacing:.03em;padding:6px 14px;border-radius:999px;"
        "opacity:.85;pointer-events:none;}</style>\n"
        f"{body_inner}\n<script>\n{shim}\n{app_js}\n</script>\n"
    )


def main() -> None:
    out = Path(sys.argv[1])
    feeds = sys.argv[2:]
    if not feeds:
        raise SystemExit("provide at least one feed CSV")
    init_db()
    db = SessionLocal()
    for f in feeds:
        raw = Path(f).read_bytes()
        r = ingest.ingest_drop(db, raw, filename=Path(f).name)
        print(f"imported {Path(f).name}: {r['imported_rows']} rows, {r['new_leads']} new")
    bundle = build_bundle(db)
    out.write_text(assemble_html(bundle))
    db.close()
    print(f"wrote {out} ({out.stat().st_size // 1024} KB, {len(bundle)} cached responses)")


if __name__ == "__main__":
    main()
