"""FastAPI application entrypoint.

Serves the JSON API under /api and the static dashboard SPA from the frontend
directory. On first boot it creates tables and (unless disabled) seeds a
synthetic demo dataset so the deployed URL shows a populated dashboard.
"""
from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config
from .db import SessionLocal, init_db
from .routers import api, imports

log = logging.getLogger("kpal")

app = FastAPI(
    title="Kotak PAL Journey Analyzer",
    description="Backend + manual data import for the Kotak PAL lead-journey analytics dashboard.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api.router)
app.include_router(imports.router)


@app.on_event("startup")
def _startup() -> None:
    init_db()
    if config.SEED_DEMO:
        from .seed import seed_demo

        db = SessionLocal()
        try:
            result = seed_demo(db)
            if not result.get("skipped"):
                log.info("Seeded demo data: %s", result)
        except Exception:  # pragma: no cover - seeding must never block boot
            log.exception("Demo seeding failed; starting with empty dataset")
        finally:
            db.close()


@app.get("/api/ping")
def ping() -> dict:
    return {"ok": True, "service": "kotak-pal-journey-analyzer"}


# ─────────────────────────── static frontend ───────────────────────────
if config.FRONTEND_DIR.exists():
    app.mount("/assets", StaticFiles(directory=config.FRONTEND_DIR / "assets"), name="assets")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(config.FRONTEND_DIR / "index.html")

    @app.get("/{path:path}")
    def spa(path: str):
        # Serve real files if present; otherwise fall back to the SPA shell.
        candidate = config.FRONTEND_DIR / path
        if candidate.is_file():
            return FileResponse(candidate)
        index_file = config.FRONTEND_DIR / "index.html"
        if index_file.exists():
            return FileResponse(index_file)
        return JSONResponse({"detail": "Not found"}, status_code=404)
