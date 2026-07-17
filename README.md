# Kotak PAL — Journey Analyzer

A deployable, full-stack analytics app for the Kotak PAL lending journey. It
turns daily client data drops into six live dashboards — Overview, Cohort
Triangle, Stage Explorer, Attribution, Data Health and Settings — and ships
with a **manual data-import** flow so an analyst can upload a drop and see it
folded into the analytics immediately.

It started as a static design prototype (`Kotak PAL.dc.html`). This repo turns
that prototype into a real product: a **Python (FastAPI) backend** that
computes every number from imported data, and a faithful rebuild of the UI that
reads from the API.

<p align="center"><em>Backend: FastAPI + SQLAlchemy · Frontend: dependency-free SPA · Storage: SQLite (swappable)</em></p>

---

## How the data works

Kotak delivers **two daily files ("drops")**, joined on `offer_id`:

| Feed | Columns | Purpose |
|------|---------|---------|
| **Offer feed** | `offerid, name, mobile, max_loan_amount, max_tenure_months, roi, EMI, processing_fee, schemecode` | Offer terms per lead |
| **Journey feed** | `offer_id, last_call_outcome, Created Date, DIY Sub-stage, DIS VALUE` | Stage, entry date, call outcome, disbursed amount |

Key ideas:

- **Each file is one dated snapshot.** The drop date is the date *of the data*
  — read from the filename (`..._2026-07-17.csv`) or set at upload. Sequential
  drops let the backend **reconstruct each lead's journey**: stage transitions,
  time-in-stage, and milestone timing.
- **Scalable + idempotent.** Re-sending July 2nd's file updates July 2nd — it
  never duplicates. New days just extend the timeline.
- **Either feed imports on its own.** The offer feed (no stage) creates/enriches
  leads at a default stage; the journey feed drives the real stage. They merge
  on `offer_id`.
- **No PII stored.** `name` and `mobile` are ignored — they power no insight.
- **Forgiving parser.** Fuzzy column auto-mapping, `₹`/`%`/comma stripping,
  `#N/A` handling, and date-format auto-detection (`Created Date` is `M/D/YYYY`,
  not `D/M`).

### What each field powers

`lead_id`(offer_id) + `stage` + `drop_date` + `entry_date` is the minimum for a
populated dashboard. Everything else enriches specific panels and **degrades
gracefully** (honest empty states, never fabricated numbers):

- **stage** → Overview buckets, Stage Explorer, Cohort milestones
- **entry_date / drop_date** → cohorts, range filters, time-in-stage, Data Health
- **DIS VALUE** → Attribution ₹ amounts
- **max_loan_amount / tenure / roi / schemecode** → Attribution metadata cuts
- **last_call_outcome** → call-outcome breakdown, connect rate, Voice-AI split

Won / In-flight / Lost buckets, aging, cohort maturity, and data-quality flags
are all **derived** by the backend — not imported.

---

## Quickstart (local)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r backend/requirements.txt
cd backend
uvicorn app.main:app --reload --port 8000
```

Open <http://localhost:8000>. On first boot it seeds a synthetic demo dataset
(through the real import pipeline) so the dashboard is populated. Set
`KPAL_SEED_DEMO=0` to start empty.

## Run with Docker

```bash
docker compose up --build
# → http://localhost:8000  (SQLite persisted in the kpal-data volume)
```

## Deploy anywhere

The single image serves both API and frontend and honours `$PORT`, so it drops
straight onto Render, Railway, Fly.io, Cloud Run, or any container host. For a
production database, point `KPAL_DATABASE_URL` at Postgres/MySQL — SQLAlchemy
handles the rest, no code change:

```bash
KPAL_DATABASE_URL="postgresql+psycopg://user:pass@host:5432/kpal"
```

---

## Configuration

| Env var | Default | Description |
|---------|---------|-------------|
| `KPAL_DATABASE_URL` | `sqlite:///<repo>/data/kpal.db` | Any SQLAlchemy URL |
| `KPAL_DATA_DIR` | `<repo>/data` | Where the SQLite file lives |
| `KPAL_SEED_DEMO` | `1` | Seed demo data on first boot |
| `KPAL_FRONTEND_DIR` | `<repo>/frontend` | Static SPA directory |
| `KPAL_CORS_ORIGINS` | `*` | Comma-separated allowed origins |
| `PORT` | `8000` | Bind port (set by most PaaS) |

---

## API

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/meta` | Ranges, milestones, dimensions, settings, per-range summaries |
| `GET` | `/api/overview?range=` | Buckets, deltas, aging |
| `GET` | `/api/cohort?milestone=` | Cohort-triangle grid + summary |
| `GET` | `/api/stages?range=&filter=` | Sub-stages, counts, medians |
| `POST` | `/api/stages/classify` | Override a stage's bucket |
| `GET` | `/api/attribution?range=&dim=` | Voice/organic split, call outcomes, metadata cuts |
| `GET` | `/api/health-report` | Drop ledger, completeness, quality flags |
| `GET`/`POST` | `/api/settings` | Aging threshold, default milestone |
| `POST` | `/api/import/preview` | Upload → auto-mapping + parsed sample (no write) |
| `POST` | `/api/import/commit` | Ingest a drop |
| `GET` | `/api/import/drops` | Import history |
| `DELETE` | `/api/import/reset` | Wipe imported data |

Interactive docs at `/docs`.

---

## Manual import flow

1. **Data Import** screen → drop a CSV (offer or journey feed).
2. The app auto-detects columns, date format, and shows a parsed preview with
   valid/skip counts. Adjust any mapping; override the drop date if needed.
3. **Import** → the drop is reconstructed into the analytics; a history row and
   toast confirm the result. Every other screen updates on the next visit.

Example files: [`sample_data/`](./sample_data).

---

## Project layout

```
backend/app/
  main.py        FastAPI app + static serving + first-boot seed
  catalog.py     Journey stage order, default buckets, milestones, dimensions
  models.py      SQLAlchemy models (DailyDrop, Lead, StageEvent, ...)
  ingest.py      CSV parsing, column mapping, journey reconstruction
  analytics.py   All six screens computed from the data
  seed.py        Deterministic demo generator (uses the real import path)
  routers/       api.py (analytics/settings) + imports.py (upload)
frontend/        index.html + assets/ (dependency-free SPA, SquadStack design)
sample_data/     Example offer + journey drops
```

## Tests

```bash
pip install -r backend/requirements-dev.txt
cd backend && pytest -q
```
