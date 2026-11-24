FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    ARCHIVE_DB=/data/reconcile.sqlite3

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY archive_reconcile ./archive_reconcile

RUN useradd --create-home --uid 10001 archive \
    && mkdir -p /data \
    && chown -R archive:archive /data /app
USER archive

EXPOSE 8000
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"

CMD ["sh", "-c", "exec gunicorn --bind 0.0.0.0:8000 --workers ${GUNICORN_WORKERS:-1} --threads 4 --timeout ${GUNICORN_TIMEOUT:-120} --access-logfile - --error-logfile - 'archive_reconcile.app:create_app()'"]
