"""Runtime configuration for the Kotak PAL Journey Analyzer backend.

Everything is environment-driven so the same image runs locally, in Docker,
or on a managed host. Sensible defaults keep `uvicorn app.main:app` working
with zero configuration.
"""
from __future__ import annotations

import os
from pathlib import Path

# Repo root is two levels up from this file (backend/app/config.py).
BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BACKEND_DIR.parent

# Where the SQLite file (and any uploaded originals) live. Overridable so a
# container can point it at a mounted volume.
DATA_DIR = Path(os.environ.get("KPAL_DATA_DIR", REPO_ROOT / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DATABASE_URL = os.environ.get(
    "KPAL_DATABASE_URL", f"sqlite:///{DATA_DIR / 'kpal.db'}"
)

# Static frontend directory served by FastAPI in production.
FRONTEND_DIR = Path(os.environ.get("KPAL_FRONTEND_DIR", REPO_ROOT / "frontend"))

# When true (default), the app seeds a synthetic demo dataset on first boot so
# a fresh deploy shows a populated dashboard. Set KPAL_SEED_DEMO=0 to start empty.
SEED_DEMO = os.environ.get("KPAL_SEED_DEMO", "1") not in ("0", "false", "False", "")

# CORS origins for running the frontend on a separate dev server.
CORS_ORIGINS = [
    o.strip()
    for o in os.environ.get("KPAL_CORS_ORIGINS", "*").split(",")
    if o.strip()
]
