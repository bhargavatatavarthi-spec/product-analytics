# Kotak PAL — Journey Analyzer

A deployable, full-stack analytics app for the Kotak PAL lending journey. It
turns daily client data drops into four live dashboards — Overview, Cohort
Triangle, Attribution and Settings — and ships with a **manual data-import**
flow so an analyst can upload a drop and see it folded into the analytics
immediately.

It started as a static design prototype (`Kotak PAL.dc.html`). This repo turns
that prototype into a real product: a **Python (FastAPI) backend** that
computes every number from imported data, and a faithful rebuild of the UI that
reads from the API.

<p align="center"><em>Backend: FastAPI + SQLAlchemy · Frontend: dependency-free SPA · Storage: SQLite (swappable)</em></p>

---

## How the data works

Kotak delivers **two daily files ("drops")**, joined on the offer/lead id:

| Feed | Columns (real export headers) | Purpose |
|------|---------|---------|
| **Journey feed** | `INTERNAL_ID, LAST_CALL_OUTCOME, CONNECTED_AT_LEAST_ONCE, DIY Sub-Stage, Disbursement Amount, Created Date` | Stage, entry date, call outcome, voice-touched flag, disbursed amount |
| **Offer feed** | `internal_id, name, max_loan_amount, max_tenure_months, processing_fee, scheme_id, roi` | Offer terms per lead |

The importer's fuzzy column matching handles both these headers and common
variants automatically, joining the feeds on `internal_id`. Real drops are
~350k rows / ~30 MB each; the file is **streamed and committed in chunks**
(with `MALLOC_ARENA_MAX=2` to keep glibc from fragmenting), so a full drop
imports in ~30–60s within ~230 MB of RAM (fits a 512 MB free tier).
ROI stored as a fraction (`0.115`) is normalized to percent (`11.5`).

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
- **`#N/A` is a null, not an error.** Clients use `#N/A` pervasively as their
  empty token, so it is treated as absence.
- **Leads with no journey stage** (`DIY Sub-Stage = #N/A`) — the ~98% of dialed
  leads not (yet) in the offer journey — are recorded as **"Not in DIY Journey"**
  (Unclassified). Their call outcomes still feed Attribution; they're excluded
  from the Won/In-flight/Lost buckets.
- **Forgiving parser.** Fuzzy column auto-mapping, `₹`/`%`/comma stripping, and
  date-format **auto-detection** (handles both `DD-MM-YYYY` and `M/D/YYYY`).

### What each field powers

`lead_id`(offer_id) + `stage` + `drop_date` + `entry_date` is the minimum for a
populated dashboard. Everything else enriches specific panels and **degrades
gracefully** (honest empty states, never fabricated numbers):

- **stage** → Overview buckets
- **entry_date (Created Date)** → cohort rows, range filters, time-in-stage
- **milestone dates** (optional: offer-generated / offer-selected / DIA-AA-initiated
  / disbursement date) → the **Cohort Triangle**. Reach = milestone_date −
  Created Date, so real cohort curves come from a single daily drop. Auto-detected
  by column name (e.g. `DIA Date` → AA Initiated); a milestone with no date column
  shows a "add this column" note instead of a misleading grid.
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
straight onto Render, Railway, Fly.io, Cloud Run, or any container host.

**One-click on Render:** this repo ships a [`render.yaml`](./render.yaml)
Blueprint. In Render: **New → Blueprint → pick this repo**. It provisions a
Docker web service with a 1 GB persistent disk for the SQLite DB and a
`/api/ping` health check — no further config needed.

For a production database, point `KPAL_DATABASE_URL` at Postgres/MySQL —
SQLAlchemy handles the rest, no code change:

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
| `GET` | `/api/attribution?range=&dim=` | Voice/organic split, call outcomes, metadata cuts |
| `GET`/`POST` | `/api/settings` | Aging threshold, default milestone |
| `POST` | `/api/import/preview` | Upload → auto-mapping + parsed sample (no write) |
| `POST` | `/api/import/commit` | Ingest a drop |
| `GET` | `/api/import/drops` | Import history |
| `DELETE` | `/api/import/reset` | Wipe imported data |

Interactive docs at `/docs`.

---

## Manual import flow

1. **Data Import** screen → drop your daily CSV file(s) (journey and/or offer
   feed — select both together). Columns and date format are auto-detected.
2. Each file uploads to a **background job** and shows a live **0–100 %
   progress bar** ("Processing… file 1 of 2"); the UI never freezes and there's
   no HTTP-timeout risk. Multiple files import in sequence.
3. When it finishes, a toast confirms and every dashboard reflects the new data.

The import is non-blocking (`POST /api/import/start` → poll
`GET /api/import/status/{job_id}`); writes are serialised so queued files never
conflict.

Example files: [`sample_data/`](./sample_data).

---

## Project layout

```
backend/app/
  main.py        FastAPI app + static serving + first-boot seed
  catalog.py     Journey stage order, default buckets, milestones, dimensions
  models.py      SQLAlchemy models (DailyDrop, Lead, StageEvent, ...)
  ingest.py      CSV parsing, column mapping, journey reconstruction
  analytics.py   All dashboard screens computed from the data
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
