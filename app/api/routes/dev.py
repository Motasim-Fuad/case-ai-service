"""Developer-only helpers (disabled when ENVIRONMENT=production).

* POST /dev/process-upload  : upload a file straight from Swagger UI -> local storage -> normal job
* POST/GET /dev/webhook-echo: a fake NestJS webhook receiver so you can see the exact payload
* GET  /dev/jobs/{id}/pages : the normalised page text (to debug OCR / text extraction quality)
"""
from __future__ import annotations

import re
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile

from app.api.deps import get_enqueue, get_job_store, get_settings, get_storage
from app.core.config import Settings
from app.core.errors import ApiError, NotFoundError
from app.core.security import verify_signature
from app.jobs.job_store import JobStore
from app.models.api import ProcessAccepted, ProcessRequest
from app.services.file_sniffer import kind_from_hint
from app.services.storage import LocalStorage, Storage
from app.services.submission import submit_job

router = APIRouter(prefix="/dev", tags=["dev (disabled in production)"])


@router.post("/process-upload", response_model=ProcessAccepted, status_code=202)
def process_upload(
    file: UploadFile = File(..., description="PDF / JPG / PNG / TIFF / DOCX / TXT"),
    case_id: str = Form("case-dev-1"),
    document_id: str | None = Form(None),
    send_webhook: bool = Form(True, description="Send the result to the built-in echo receiver."),
    settings: Settings = Depends(get_settings),
    jobs: JobStore = Depends(get_job_store),
    storage: Storage = Depends(get_storage),
    enqueue: Callable[[str], None] = Depends(get_enqueue),
) -> ProcessAccepted:
    if not isinstance(storage, LocalStorage):
        raise ApiError("process-upload needs STORAGE_BACKEND=local", code="LOCAL_STORAGE_REQUIRED")
    data = file.file.read()
    if len(data) > settings.max_file_bytes:
        raise ApiError(f"File larger than {settings.max_file_size_mb} MB", code="FILE_TOO_LARGE")
    doc_id = document_id or f"doc-{uuid.uuid4().hex[:8]}"
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", file.filename or "upload.bin")
    key = f"dev/{doc_id}/{safe_name}"
    storage.save(key, data)
    hint = Path(safe_name).suffix.lstrip(".").lower()
    request = ProcessRequest(
        document_id=doc_id, case_id=case_id, s3_key=key,
        file_type=hint if kind_from_hint(hint) else None,  # type: ignore[arg-type]
        callback_url=settings.dev_webhook_echo_url if send_webhook else None,  # type: ignore[arg-type]
    )
    return submit_job(request, jobs, enqueue)


@router.post("/webhook-echo", summary="Fake NestJS webhook receiver (stores the last 20 payloads)")
async def webhook_echo(
    request: Request, settings: Settings = Depends(get_settings), jobs: JobStore = Depends(get_job_store)
) -> dict:
    raw = await request.body()
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {"_raw": raw.decode(errors="replace")[:2000]}
    signature = request.headers.get("X-Signature")
    jobs.push_dev_webhook({
        "received_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "idempotency_key": request.headers.get("Idempotency-Key"),
        "signature_valid": verify_signature(settings.webhook_signing_secret, raw, signature) if settings.webhook_signing_secret else None,
        "body": body,
    })
    return {"received": True}


@router.get("/webhook-echo", summary="List the last webhook payloads received")
def webhook_echo_list(jobs: JobStore = Depends(get_job_store)) -> list[dict]:
    return jobs.list_dev_webhooks()


@router.get("/jobs/{job_id}/pages", summary="Normalised page text of a processed job")
def job_pages(job_id: str, jobs: JobStore = Depends(get_job_store)) -> dict:
    rec = jobs.get(job_id)
    if rec is None or not rec.content_sha256:
        raise NotFoundError("Job or its extracted pages not found", code="PAGES_NOT_FOUND")
    cached = jobs.load_pages(rec.content_sha256)
    if cached is None:
        raise NotFoundError("Pages cache expired", code="PAGES_NOT_FOUND")
    return {"job_id": job_id, "pages": cached[0], "warnings": cached[1]}
