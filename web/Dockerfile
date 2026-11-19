FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOST=0.0.0.0 \
    PORT=8000 \
    STORAGE_DIR=/tmp/pdf2mb \
    COMPRESS_WORKERS=2 \
    MAX_UPLOAD_MB=100 \
    DEFAULT_TARGET_BYTES=1995000 \
    FILE_TTL_SECONDS=3600 \
    LOG_LEVEL=info

WORKDIR /app

# Runtime libs for Pillow / pikepdf (libqpdf) / PyMuPDF
RUN apt-get update && apt-get install -y --no-install-recommends \
        libjpeg62-turbo \
        libtiff6 \
        libopenjp2-7 \
        liblcms2-2 \
        zlib1g \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --upgrade pip && pip install -r /app/requirements.txt

COPY app /app/app
COPY static /app/static
COPY entrypoint.sh /app/entrypoint.sh

# Normalize line endings (Windows CRLF -> Unix LF) and set executable bit
RUN sed -i 's/\r$//' /app/entrypoint.sh \
    && chmod +x /app/entrypoint.sh \
    && useradd -m -u 10001 appuser \
    && mkdir -p /tmp/pdf2mb \
    && chown -R appuser:appuser /app /tmp/pdf2mb

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.getenv(\"PORT\",\"8000\")}/healthz', timeout=3)" || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]

