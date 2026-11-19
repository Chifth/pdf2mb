# `chifty/pdf2mb` — Smart PDF Optimizer (≤ 2 MB)

Легкий та швидкий self-hosted веб-сервіс для автоматичного стиснення PDF-документів під заданий ліміт (за замовчуванням **≤ 2000 КБ / 1,995,000 байт**) із розумним автовибором стратегії, збереженням OCR-шару, чергою обробки та миттєвим веб-інтерфейсом.

- **Docker Hub:** `chifty/pdf2mb:v1.0.0` (`chifty/pdf2mb:latest`)
- **Стек:** Python 3.12 + FastAPI + Uvicorn + `ProcessPoolExecutor` + PyMuPDF + pikepdf + Pillow + zero-build SPA (HTML/CSS/Vanilla JS, без зовнішніх CDN).

---

## Можливості

- **Автовибір оптимальної стратегії під кожен PDF:**
  - Файл уже `≤ ліміту` → повертається 1:1 без втрат (`PASS_THROUGH`).
  - **Векторний PDF із фото** → стискає лише вбудовані растрові зображення без растризації векторного тексту.
  - **1–3 стор. або блідий друк (анти-MRC правило)** → нативний **300 DPI RGB 4:4:4 JPEG** (`subsampling=0`) + збережений OCR (без розриву блідих літер).
  - **Контрастний скан 4–7 стор.** → **MRC 300 DPI CCITT Group 4** (бритвений текст) + **300 DPI RGB 4:4:4** кольорові печатки/підписи + збережений OCR.
  - **Контрастний скан 8–12 стор.** → **MRC 300 DPI CCITT G4 + 200 DPI RGB 4:2:0** + збережений OCR.
  - **13+ сторінок** → **MRC 300 DPI CCITT G4 + 150/120 DPI** із каскадним fallback (гарантія потрапляння в ліміт).
- **Черга та пакетна обробка:** завантаження 1 або декількох PDF одночасно, паралельні воркери, живі статуси через SSE (`У черзі → Оптимізація → Готово`).
- **Детальна статистика в UI:** кількість сторінок, початковий та кінцевий розмір, відсоток стиснення, обрана стратегія, динамічні посилання на скачування (з підтримкою UTF-8 / кириличних імен) та кнопка **«Завантажити всі (.zip)»**.
- **Готовий для Docker та Kubernetes:** non-root користувач (`uid 10001`), `/healthz` та `/readyz` проби, стартап-скрипт із підтримкою змінних оточення, автоматичне очищення тимчасових файлів за TTL.

---

## Швидкий старт

### 1. Docker CLI

```bash
docker run -d \
  --name pdf2mb \
  --restart unless-stopped \
  -p 8000:8000 \
  -e COMPRESS_WORKERS=2 \
  -e DEFAULT_TARGET_BYTES=1995000 \
  -e FILE_TTL_SECONDS=3600 \
  chifty/pdf2mb:v1.0.0
```

Відкрити у браузері: `http://localhost:8000`

### 2. Docker Compose

```yaml
services:
  pdf2mb:
    image: chifty/pdf2mb:v1.0.0
    container_name: pdf2mb
    restart: unless-stopped
    ports:
      - "8000:8000"
    environment:
      PORT: "8000"
      COMPRESS_WORKERS: "2"
      MAX_UPLOAD_MB: "100"
      DEFAULT_TARGET_BYTES: "1995000"
      FILE_TTL_SECONDS: "3600"
      STORAGE_DIR: "/tmp/pdf2mb"
      PROXY_HEADERS: "true"
    tmpfs:
      - /tmp/pdf2mb:size=1024m
```

```bash
docker compose up -d
```

### 3. Kubernetes

Готовий маніфест знаходиться в [web/k8s/deployment.yaml](./web/k8s/deployment.yaml):

```bash
kubectl apply -f web/k8s/deployment.yaml
```

> При використанні за Nginx / Traefik Ingress залиште `PROXY_HEADERS=true`. Якщо сервіс публікується на підшляху (наприклад `/pdf2mb`), задайте `ROOT_PATH=/pdf2mb`.

---

## Змінні оточення

| Змінна | За замовчуванням | Опис |
| :--- | :--- | :--- |
| `HOST` | `0.0.0.0` | Адреса прив'язки HTTP-сервера |
| `PORT` | `8000` | Порт HTTP-сервера |
| `ROOT_PATH` | `""` | Префікс шляху за reverse-proxy (напр. `/pdf2mb`) |
| `LOG_LEVEL` | `info` | Рівень логування (`debug`, `info`, `warning`, `error`) |
| `COMPRESS_WORKERS` | `2` | Кількість паралельних процесів стиснення PDF (`ProcessPoolExecutor`) |
| `MAX_UPLOAD_MB` | `100` | Максимальний розмір одного завантаженого PDF (у МБ) |
| `DEFAULT_TARGET_BYTES` | `1995000` | Цільовий ліміт розміру за замовчуванням у байтах (`1,995,000` ≈ `≤ 2000 КБ`) |
| `MIN_TARGET_BYTES` | `100000` | Мінімально дозволений ліміт у формі (`100 КБ`) |
| `MAX_TARGET_BYTES` | `25000000` | Максимально дозволений ліміт у формі (`25 МБ`) |
| `STORAGE_DIR` | `/tmp/pdf2mb` | Директорія тимчасового зберігання файлів (рекомендовано `tmpfs` / `emptyDir`) |
| `FILE_TTL_SECONDS` | `3600` | Час зберігання готових PDF перед автовидаленням (секунди) |
| `CLEANUP_INTERVAL_SECONDS` | `300` | Інтервал перевірки застарілих файлів (секунди) |
| `CORS_ORIGINS` | `*` | Дозволені CORS-джерела (через кому або `*`) |
| `PROXY_HEADERS` | `true` | Довіряти заголовкам `X-Forwarded-*` (`true` / `false`) |
| `FORWARDED_ALLOW_IPS` | `*` | IP-адреси довірених проксі для `--forwarded-allow-ips` |

---

## HTTP API

| Метод | Ендпоінт | Опис |
| :--- | :--- | :--- |
| `GET` | `/` | Веб-інтерфейс (SPA) |
| `GET` | `/healthz` | Liveness-проба (`{"status":"ok"}`) |
| `GET` | `/readyz` | Readiness-проба (статус пулу воркерів і розмір черги) |
| `GET` | `/api/config` | Поточні ліміти та параметри сервера для фронтенду |
| `POST` | `/api/jobs` | Додати 1+ PDF у чергу (`multipart/form-data`: `files`, опційно `target_kb`) |
| `GET` | `/api/jobs/{job_id}` | Поточний статус і метрики завдання |
| `GET` | `/api/jobs/{job_id}/events` | SSE-потік живих оновлень статусу завдання |
| `GET` | `/api/download/{job_id}` | Завантажити готовий оптимізований PDF |
| `POST` | `/api/download-zip` | Завантажити пачку готових PDF одним архівом (`{"job_ids": [...]}`) |

---

## Локальна збірка образу

```bash
cd web
docker build -t chifty/pdf2mb:v1.0.0 -t chifty/pdf2mb:latest .
docker push chifty/pdf2mb:v1.0.0
docker push chifty/pdf2mb:latest
```
