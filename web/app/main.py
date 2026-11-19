from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import time
import uuid
import zipfile
from concurrent.futures import ProcessPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .compressor import CompressionResult, optimize_pdf_auto
from .config import settings

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("pdf2mb")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@dataclass
class JobRecord:
    id: str
    filename: str
    orig_bytes: int
    target_bytes: int
    input_path: str
    output_path: str
    status: str = "queued"  # queued | processing | done | error
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    result: dict | None = None
    error: str | None = None

    def to_public_dict(self) -> dict:
        data: dict = {
            "id": self.id,
            "filename": self.filename,
            "orig_bytes": self.orig_bytes,
            "target_bytes": self.target_bytes,
            "status": self.status,
            "created_at": self.created_at,
            "download_url": f"api/download/{self.id}" if self.status == "done" else None,
            "error": self.error,
        }
        if self.result:
            data.update(self.result)
        return data


class JobManager:
    def __init__(self) -> None:
        self.jobs: dict[str, JobRecord] = {}
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.subscribers: dict[str, list[asyncio.Queue[dict]]] = {}
        self.executor: ProcessPoolExecutor | None = None
        self._worker_tasks: list[asyncio.Task] = []
        self._cleanup_task: asyncio.Task | None = None
        self.ready = False

    async def start(self) -> None:
        os.makedirs(settings.storage_dir, exist_ok=True)
        self.executor = ProcessPoolExecutor(max_workers=settings.compress_workers)
        for idx in range(settings.compress_workers):
            self._worker_tasks.append(asyncio.create_task(self._worker_loop(idx)))
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        self.ready = True
        logger.info(
            "PDF2MB started | workers=%d | storage=%s | default_target=%d B | ttl=%ds",
            settings.compress_workers,
            settings.storage_dir,
            settings.default_target_bytes,
            settings.file_ttl_seconds,
        )

    async def stop(self) -> None:
        self.ready = False
        for t in self._worker_tasks:
            t.cancel()
        if self._cleanup_task:
            self._cleanup_task.cancel()
        if self.executor:
            self.executor.shutdown(wait=False, cancel_futures=True)

    def _notify(self, job: JobRecord) -> None:
        payload = job.to_public_dict()
        for q in list(self.subscribers.get(job.id, [])):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                pass

    def subscribe(self, job_id: str) -> asyncio.Queue[dict]:
        q: asyncio.Queue[dict] = asyncio.Queue(maxsize=32)
        self.subscribers.setdefault(job_id, []).append(q)
        return q

    def unsubscribe(self, job_id: str, q: asyncio.Queue[dict]) -> None:
        lst = self.subscribers.get(job_id)
        if lst and q in lst:
            lst.remove(q)
            if not lst:
                self.subscribers.pop(job_id, None)

    async def enqueue(self, job: JobRecord) -> None:
        self.jobs[job.id] = job
        await self.queue.put(job.id)
        self._notify(job)

    def active_counts(self) -> tuple[int, int, int]:
        queued = 0
        processing = 0
        for j in self.jobs.values():
            if j.status == "queued":
                queued += 1
            elif j.status == "processing":
                processing += 1
        return queued, processing, queued + processing

    async def _worker_loop(self, worker_id: int) -> None:
        loop = asyncio.get_running_loop()
        while True:
            job_id = await self.queue.get()
            job = self.jobs.get(job_id)
            if not job:
                self.queue.task_done()
                continue

            job.status = "processing"
            job.started_at = time.time()
            self._notify(job)

            try:
                assert self.executor is not None
                res: CompressionResult = await loop.run_in_executor(
                    self.executor,
                    optimize_pdf_auto,
                    job.input_path,
                    job.output_path,
                    job.target_bytes,
                )
                job.result = res.to_dict()
                job.status = "done"
                job.finished_at = time.time()
                logger.info(
                    "[W%d] Done %s | %dp | %d -> %d B (-%.1f%%) | %s | %.2fs",
                    worker_id,
                    job.filename,
                    res.pages,
                    res.orig_bytes,
                    res.out_bytes,
                    res.compression_pct,
                    res.strategy_id,
                    res.elapsed_sec,
                )
            except Exception as exc:
                logger.exception("[W%d] Failed %s: %s", worker_id, job.filename, exc)
                job.status = "error"
                job.error = f"Помилка обробки PDF: {exc}"
                job.finished_at = time.time()
            finally:
                # Input file is no longer needed once processed
                try:
                    if os.path.exists(job.input_path):
                        os.remove(job.input_path)
                except OSError:
                    pass
                self._notify(job)
                self.queue.task_done()

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(settings.cleanup_interval_seconds)
            now = time.time()
            expired = [
                jid
                for jid, job in list(self.jobs.items())
                if job.status in ("done", "error")
                and (now - (job.finished_at or job.created_at)) > settings.file_ttl_seconds
            ]
            for jid in expired:
                job = self.jobs.pop(jid, None)
                if not job:
                    continue
                for p in (job.input_path, job.output_path):
                    try:
                        if os.path.exists(p):
                            os.remove(p)
                    except OSError:
                        pass
            if expired:
                logger.info("Cleaned up %d expired jobs", len(expired))


manager = JobManager()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await manager.start()
    try:
        yield
    finally:
        await manager.stop()


app = FastAPI(
    title="PDF 2MB Smart Optimizer",
    version="1.0.0",
    root_path=settings.root_path,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _sanitize_filename(name: str) -> str:
    base = os.path.basename(name or "document.pdf").strip()
    base = re.sub(r'[\x00-\x1f<>:"/\\|?*]+', "_", base)
    if not base.lower().endswith(".pdf"):
        base += ".pdf"
    return base


def _content_disposition_utf8(filename: str) -> str:
    ascii_fallback = re.sub(r"[^\x20-\x7e]+", "_", filename).replace('"', "")
    if not ascii_fallback or ascii_fallback == ".pdf":
        ascii_fallback = "optimized.pdf"
    encoded = quote(filename, safe="")
    return f'attachment; filename="{ascii_fallback}"; filename*=UTF-8\'\'{encoded}'


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/readyz")
async def readyz():
    if not manager.ready:
        raise HTTPException(status_code=503, detail="Not ready")
    queued, processing, total_active = manager.active_counts()
    return {
        "status": "ready",
        "workers": settings.compress_workers,
        "queued": queued,
        "processing": processing,
        "active_total": total_active,
    }


@app.get("/api/status")
async def get_status():
    queued, processing, total_active = manager.active_counts()
    return {
        "ready": manager.ready,
        "workers": settings.compress_workers,
        "queued": queued,
        "processing": processing,
        "active_total": total_active,
    }


@app.get("/api/config")
async def get_config():
    return {
        "default_target_kb": round(settings.default_target_bytes / 1000),
        "min_target_kb": round(settings.min_target_bytes / 1000),
        "max_target_kb": round(settings.max_target_bytes / 1000),
        "max_upload_mb": settings.max_upload_mb,
        "file_ttl_seconds": settings.file_ttl_seconds,
        "workers": settings.compress_workers,
    }


@app.post("/api/jobs")
async def create_jobs(
    files: list[UploadFile] = File(...),
    target_kb: int | None = Form(default=None),
):
    if not files:
        raise HTTPException(status_code=400, detail="Не передано жодного файлу.")

    target_bytes = settings.default_target_bytes
    if target_kb is not None:
        target_bytes = max(settings.min_target_bytes, min(settings.max_target_bytes, int(target_kb) * 1000))

    created: list[dict] = []
    for up in files:
        fname = _sanitize_filename(up.filename or "document.pdf")
        data = await up.read()
        if not data:
            continue
        if len(data) > settings.max_upload_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"Файл '{fname}' перевищує ліміт {settings.max_upload_mb} МБ.",
            )
        if not data[:8].startswith(b"%PDF"):
            raise HTTPException(
                status_code=400,
                detail=f"Файл '{fname}' не є коректним PDF-документом.",
            )

        job_id = uuid.uuid4().hex
        in_path = os.path.join(settings.storage_dir, f"{job_id}_in.pdf")
        out_path = os.path.join(settings.storage_dir, f"{job_id}_out.pdf")
        with open(in_path, "wb") as f:
            f.write(data)

        job = JobRecord(
            id=job_id,
            filename=fname,
            orig_bytes=len(data),
            target_bytes=target_bytes,
            input_path=in_path,
            output_path=out_path,
        )
        await manager.enqueue(job)
        created.append(job.to_public_dict())

    if not created:
        raise HTTPException(status_code=400, detail="Усі передані файли порожні.")

    return JSONResponse({"jobs": created})


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    job = manager.jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Завдання не знайдено або термін зберігання вичерпано.")
    return job.to_public_dict()


@app.get("/api/jobs/{job_id}/events")
async def stream_job_events(job_id: str, request: Request):
    job = manager.jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Завдання не знайдено.")

    q = manager.subscribe(job_id)

    async def event_gen():
        try:
            # Immediately push current state
            current = job.to_public_dict()
            yield f"data: {json.dumps(current, ensure_ascii=False)}\n\n"
            if current["status"] in ("done", "error"):
                return

            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(q.get(), timeout=15.0)
                    yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                    if payload["status"] in ("done", "error"):
                        break
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            manager.unsubscribe(job_id, q)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/download/{job_id}")
async def download_job(job_id: str):
    job = manager.jobs.get(job_id)
    if not job or job.status != "done" or not os.path.exists(job.output_path):
        raise HTTPException(status_code=404, detail="Оптимізований файл не знайдено або вже видалено.")
    return FileResponse(
        path=job.output_path,
        media_type="application/pdf",
        headers={"Content-Disposition": _content_disposition_utf8(job.filename)},
    )


class ZipRequest(BaseModel):
    job_ids: list[str]


@app.post("/api/download-zip")
async def download_zip(req: ZipRequest):
    ready_jobs = [
        manager.jobs[jid]
        for jid in req.job_ids
        if jid in manager.jobs
        and manager.jobs[jid].status == "done"
        and os.path.exists(manager.jobs[jid].output_path)
    ]
    if not ready_jobs:
        raise HTTPException(status_code=404, detail="Немає готових файлів для архіву.")

    buf = io.BytesIO()
    used_names: dict[str, int] = {}
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        for job in ready_jobs:
            name = job.filename
            if name in used_names:
                used_names[name] += 1
                stem, ext = os.path.splitext(name)
                name = f"{stem} ({used_names[name]}){ext}"
            else:
                used_names[name] = 0
            zf.write(job.output_path, arcname=name)

    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="optimized_pdfs.zip"'},
    )


# Serve ultra-light SPA frontend
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")

