# Kotak PAL Journey Analyzer — single-image deploy (API + static frontend).
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    KPAL_DATA_DIR=/data \
    KPAL_FRONTEND_DIR=/app/frontend \
    # Cap glibc malloc arenas: without this, a 350k-row import fragments across
    # many per-thread arenas and peaks ~775 MB; capped it peaks ~226 MB (fits a
    # 512 MB free tier).
    MALLOC_ARENA_MAX=2

WORKDIR /app

# Install dependencies first for layer caching.
COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

# App code.
COPY backend ./backend
COPY frontend ./frontend

# Persist the SQLite DB on a volume.
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000
WORKDIR /app/backend

# Honour the platform's $PORT (Render/Railway/Fly set it); default 8000.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
