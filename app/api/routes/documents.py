from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Depends

from app.api.deps import get_enqueue, get_job_store, get_settings, get_vector_store
from app.core.config import Settings
from app.core.errors import NotFoundError
from app.jobs.job_store import JobStore
from app.models.api import DeleteIndexResponse, JobStatusResponse, ProcessAccepted, ProcessRequest
from app.services.submission import submit_job, validate_process_request
from app.services.vector_store import QdrantVectorStore

router = APIRouter(prefix="/documents", tags=["documents"])


@router.post("/process", response_model=ProcessAccepted, status_code=202,
             summary="Queue a document for OCR + AI extraction + indexing")
def process_document(
    request: ProcessRequest,
    settings: Settings = Depends(get_settings),
    jobs: JobStore = Depends(get_job_store),
    enqueue: Callable[[str], None] = Depends(get_enqueue),
) -> ProcessAccepted:
    """Returns immediately with a `job_id`. The heavy work runs in a Celery worker; the result is
    POSTed to `callback_url` and can also be polled with `GET /documents/jobs/{job_id}`."""
    validate_process_request(request, settings)
    return submit_job(request, jobs, enqueue)


@router.get("/jobs/{job_id}", response_model=JobStatusResponse, summary="Job status / result (polling fallback)")
def get_job(job_id: str, jobs: JobStore = Depends(get_job_store)) -> JobStatusResponse:
    rec = jobs.get(job_id)
    if rec is None:
        raise NotFoundError("Job not found or expired", code="JOB_NOT_FOUND")
    result = jobs.get_result(job_id) if rec.status in ("completed", "failed") else None
    return JobStatusResponse(
        job_id=rec.job_id, document_id=rec.document_id, case_id=rec.case_id,
        status=rec.status,  # type: ignore[arg-type]
        stage=rec.stage, progress=rec.progress, attempts=rec.attempts, result=result,
        chunks_indexed=rec.chunks_indexed, error=rec.error, error_code=rec.error_code, warnings=rec.warnings,
        callback_status=rec.callback_status,  # type: ignore[arg-type]
        created_at=rec.created_at, updated_at=rec.updated_at, finished_at=rec.finished_at,
    )


@router.delete("/{document_id}/index", response_model=DeleteIndexResponse,
               summary="Remove a document's vectors (call when the document is deleted)")
def delete_document_index(document_id: str, vectors: QdrantVectorStore = Depends(get_vector_store)) -> DeleteIndexResponse:
    vectors.delete_document(document_id)
    return DeleteIndexResponse(scope="document", id=document_id)
