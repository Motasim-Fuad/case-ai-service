"""Validation + submission of processing jobs (shared by the public and dev endpoints)."""
from __future__ import annotations

import uuid
from typing import Callable

from app.core.config import Settings
from app.core.errors import ApiError, ServiceUnavailableError
from app.core.logging import get_logger
from app.jobs.job_store import JobStore
from app.models.api import ProcessAccepted, ProcessRequest

log = get_logger(__name__)


def validate_process_request(request: ProcessRequest, settings: Settings) -> None:
    """Defence in depth on top of the API key: allow-list callback hosts and S3 buckets."""
    if request.callback_url is not None and settings.callback_hosts:
        host = (request.callback_url.host or "").lower()
        if host not in settings.callback_hosts:
            raise ApiError(f"callback_url host '{host}' is not allowed", code="CALLBACK_HOST_NOT_ALLOWED")
    if settings.storage_backend == "s3":
        bucket = request.s3_bucket or settings.s3_bucket_name
        if not bucket:
            raise ApiError("s3_bucket is required (or set S3_BUCKET_NAME)", code="BUCKET_REQUIRED")
        allowed = settings.s3_buckets_allowed
        if allowed and bucket not in allowed:
            raise ApiError(f"s3_bucket '{bucket}' is not allowed", code="BUCKET_NOT_ALLOWED")


def submit_job(request: ProcessRequest, jobs: JobStore, enqueue: Callable[[str], None]) -> ProcessAccepted:
    job_id = uuid.uuid4().hex
    existing = jobs.acquire_document(request.document_id, job_id)
    if existing:  # idempotent: NestJS retried the call while a job is still running
        record = jobs.get(existing)
        log.info("job_deduplicated", existing_job=existing, document_id=request.document_id)
        return ProcessAccepted(job_id=existing, status=record.status if record else "queued",  # type: ignore[arg-type]
                               document_id=request.document_id, deduplicated=True)
    jobs.create(job_id, request)
    try:
        enqueue(job_id)
    except Exception as exc:  # noqa: BLE001 - broker down
        jobs.update(job_id, status="failed", stage="failed", error="Could not enqueue job", error_code="QUEUE_UNAVAILABLE")
        jobs.release_document(request.document_id, job_id)
        raise ServiceUnavailableError("Job queue is unavailable, try again shortly", code="QUEUE_UNAVAILABLE") from exc
    log.info("job_queued", job_id=job_id, document_id=request.document_id, case_id=request.case_id)
    return ProcessAccepted(job_id=job_id, status="queued", document_id=request.document_id)
