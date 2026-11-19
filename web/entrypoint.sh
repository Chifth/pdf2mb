#!/bin/sh
set -eu

: "${HOST:=0.0.0.0}"
: "${PORT:=8000}"
: "${LOG_LEVEL:=info}"
: "${ROOT_PATH:=}"
: "${STORAGE_DIR:=/tmp/pdf2mb}"
: "${COMPRESS_WORKERS:=2}"
: "${MAX_UPLOAD_MB:=100}"
: "${DEFAULT_TARGET_BYTES:=1995000}"
: "${FILE_TTL_SECONDS:=3600}"
: "${CLEANUP_INTERVAL_SECONDS:=300}"
: "${CORS_ORIGINS:=*}"
: "${PROXY_HEADERS:=true}"
: "${FORWARDED_ALLOW_IPS:=*}"

mkdir -p "${STORAGE_DIR}"

echo "[entrypoint] Starting PDF 2MB Smart Optimizer"
echo "[entrypoint] HOST=${HOST} PORT=${PORT} COMPRESS_WORKERS=${COMPRESS_WORKERS} STORAGE_DIR=${STORAGE_DIR} DEFAULT_TARGET_BYTES=${DEFAULT_TARGET_BYTES} FILE_TTL_SECONDS=${FILE_TTL_SECONDS}"

set -- app.main:app --host "${HOST}" --port "${PORT}" --log-level "${LOG_LEVEL}" --workers 1

if [ -n "${ROOT_PATH}" ]; then
  set -- "$@" --root-path "${ROOT_PATH}"
fi

if [ "${PROXY_HEADERS}" = "true" ]; then
  set -- "$@" --proxy-headers --forwarded-allow-ips "${FORWARDED_ALLOW_IPS}"
fi

exec uvicorn "$@"
