# Production image for the API. Build context is the repository root (see docker-compose.prod.yml).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# /srv/backend is the working directory, so the app's "../sample_data/small" is /srv/sample_data/small.
WORKDIR /srv/backend

COPY backend/requirements.txt .
RUN pip install -r requirements.txt

RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /data/uploads && chown appuser /data/uploads

COPY --chown=appuser:appuser backend/app app
COPY --chown=appuser:appuser backend/migrations migrations
COPY --chown=appuser:appuser sample_data/small /srv/sample_data/small
USER appuser

EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=4s --start-period=20s --retries=5 \
    CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/api/health', timeout=3)"

# ONE worker on purpose: imports and reconciliation runs take an in-process lock, so two workers
# could run them at the same time. Migrations run first, as the database owner.
CMD ["sh", "-c", "python -m app.migrate && exec uvicorn app.main:app --host 0.0.0.0 --port 8000"]
