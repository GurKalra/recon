# One image for hosts that run a single web service (Render): the API serves the website too.
# Build context is the repository root.
FROM node:22-alpine AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ .
RUN npm run build

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    WEB_DIST=/srv/web \
    UPLOAD_DIR=/data/uploads

WORKDIR /srv/backend

COPY backend/requirements.txt .
RUN pip install -r requirements.txt

RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /data/uploads && chown appuser /data/uploads

COPY --chown=appuser:appuser backend/app app
COPY --chown=appuser:appuser backend/migrations migrations
COPY --chown=appuser:appuser sample_data/small /srv/sample_data/small
COPY --from=web /web/dist /srv/web
USER appuser

# ONE worker on purpose (imports and reconciliation take an in-process lock).
# Migrations run first, as the database owner (MIGRATE_DATABASE_URL). Render sets PORT.
CMD ["sh", "-c", "python -m app.migrate && exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-10000}"]