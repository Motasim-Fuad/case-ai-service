"""The document-processing pipeline (framework-agnostic, so it is unit-testable without Celery).

fetch -> sniff -> pages (text / OCR, cached by file hash)
      -> [ index (chunk + embed + Qdrant)  ||  extract (GPT, schema-enforced) ]   (run in parallel)
      -> validate/ground -> store result in Redis -> webhook to NestJS

Every stage is resumable: on a Celery retry, finished stages are reused instead of paid for again.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.core.concurrency import ContextThreadPoolExecutor
from app.core.config import Settings
from app.core.errors import FileTooLargeError, NoReadableTextError, PermanentError
from app.core.logging import bind_job_context, get_logger, timed
from app.jobs.job_store import JobStore, utcnow
from app.models.api import ProcessRequest, WebhookPayload
from app.models.extraction import DocumentExtractionResult, ExtractionOutput, PageInfo
from app.services.chunking import chunk_pages
from app.services.embeddings import Embedder
from app.services.extraction import ExtractionService
from app.services.file_sniffer import FileKind, kind_from_hint, sniff_file_kind
from app.services.page_extractor import PageContent, PageExtractor
from app.services.storage import Storage, StorageRef
from app.services.vector_store import QdrantVectorStore
from app.services.webhook import WebhookClient

log = get_logger(__name__)
INDEX_ERROR_CODES = {"VECTOR_STORE_ERROR", "EMBEDDING_ERROR", "VECTOR_DIM_MISMATCH"}


@dataclass
class PipelineDeps:
    settings: Settings
    storage: Storage
    extractor: PageExtractor
    embedder: Embedder
    vectors: QdrantVectorStore
    extraction: ExtractionService
    jobs: JobStore
    webhook: WebhookClient


# ------------------------------------------------------------------ helpers
def _pages_from_cache(raw: list[dict]) -> list[PageContent]:
    return [PageContent(**item) for item in raw]


def _load_pages(deps: PipelineDeps, sha: str | None) -> tuple[list[PageContent], list[str]] | None:
    if not sha:
        return None
    cached = deps.jobs.load_pages(sha)
    return (_pages_from_cache(cached[0]), cached[1]) if cached else None


def _elapsed_ms(iso_start: str | None) -> int | None:
    if not iso_start:
        return None
    try:
        start = datetime.fromisoformat(iso_start)
    except ValueError:
        return None
    return int((datetime.now(timezone.utc) - start).total_seconds() * 1000)


def build_result(
    request: ProcessRequest,
    pages: list[PageContent],
    extraction: ExtractionOutput | None,
    warnings: list[str],
    *,
    status: str,
    error_message: str | None = None,
) -> DocumentExtractionResult:
    infos = [
        PageInfo(page=p.page_number, method=p.method, char_count=p.char_count,
                 ocr_confidence=p.ocr_confidence, error=p.error)
        for p in pages
    ]
    if extraction is None:
        return DocumentExtractionResult(
            document_id=request.document_id, case_id=request.case_id, pages=infos,
            warnings=list(dict.fromkeys(warnings)), processing_status="failed", error_message=error_message,
        )
    return DocumentExtractionResult(
        document_id=request.document_id, case_id=request.case_id,
        document_type=extraction.document_type, summary=extraction.summary,
        parties=extraction.parties, events=extraction.events, amounts=extraction.amounts,
        requests=extraction.requests, claim_details=extraction.claim_details, pages=infos,
        warnings=list(dict.fromkeys(warnings + extraction.warnings)),
        processing_status=status,  # type: ignore[arg-type]
        error_message=error_message,
    )


def index_document(deps: PipelineDeps, request: ProcessRequest, pages: list[PageContent], version: str) -> int:
    s = deps.settings
    chunks = chunk_pages(pages, s.chunk_size_chars, s.chunk_overlap_chars)
    if not chunks:
        return 0
    vectors = deps.embedder.embed([c.text for c in chunks])
    return deps.vectors.replace_document(
        case_id=request.case_id, document_id=request.document_id, chunks=chunks, vectors=vectors, version=version
    )


def deliver(
    deps: PipelineDeps,
    job_id: str,
    request: ProcessRequest,
    result: DocumentExtractionResult,
    *,
    status: str,
    chunks_indexed: int,
    error: str | None = None,
    error_code: str | None = None,
) -> None:
    """Persist the final state FIRST (so polling always works), then try the webhook."""
    jobs = deps.jobs
    rec = jobs.get(job_id)
    duration = _elapsed_ms(rec.started_at or rec.created_at) if rec else None
    jobs.set_result(job_id, result)
    jobs.update(job_id, status=status, stage="sending_callback", progress=95, error=error, error_code=error_code,
                warnings=result.warnings, chunks_indexed=chunks_indexed)
    callback = "skipped"
    if request.callback_url:
        payload = WebhookPayload(
            job_id=job_id, document_id=request.document_id, case_id=request.case_id,
            status=status,  # type: ignore[arg-type]
            extracted_data=result, chunks_indexed=chunks_indexed, pages_total=len(result.pages),
            pages_failed=sum(1 for p in result.pages if p.error), duration_ms=duration,
            error=error, error_code=error_code, metadata=request.metadata,
        )
        try:
            deps.webhook.send(str(request.callback_url), payload)
            callback = "delivered"
        except Exception as exc:  # noqa: BLE001 - the job result is safe in Redis; NestJS can poll
            log.error("webhook_failed", error=str(exc)[:300])
            callback = "failed"
    jobs.update(job_id, stage="done" if status == "completed" else "failed", progress=100,
                callback_status=callback, finished_at=utcnow())
    jobs.release_document(request.document_id, job_id)
    log.info("job_finished", status=status, callback=callback, duration_ms=duration, chunks_indexed=chunks_indexed)


# ---------------------------------------------------------------- main flow
def run_pipeline(job_id: str, deps: PipelineDeps, *, attempt: int = 1) -> None:
    s, jobs = deps.settings, deps.jobs
    rec = jobs.get(job_id)
    if rec is None:
        raise PermanentError("Job record not found or expired", code="JOB_NOT_FOUND")
    request = rec.request
    bind_job_context(job_id=job_id, document_id=request.document_id, case_id=request.case_id, attempt=attempt)
    deliveries = jobs.bump_deliveries(job_id)
    if deliveries > s.celery_task_max_retries + 3:
        # a document that keeps crashing workers (e.g. out-of-memory) must not be redelivered forever
        raise PermanentError(f"Job was started {deliveries} times (worker crashed repeatedly)", code="POISON_JOB")
    jobs.update(job_id, status="processing", stage="fetching_file", progress=5, attempts=attempt,
                started_at=rec.started_at or utcnow(), error=None, error_code=None)
    warnings: list[str] = []

    # 1) fetch --------------------------------------------------------------
    ref = StorageRef(bucket=request.s3_bucket or s.s3_bucket_name or None, key=request.s3_key)
    with timed(log, "fetch_file", key=request.s3_key):
        size = deps.storage.size(ref)
        if size > s.max_file_bytes:
            raise FileTooLargeError(f"File is {size / 1_048_576:.1f} MB (limit {s.max_file_size_mb} MB)")
        data = deps.storage.read(ref)
    sha = hashlib.sha256(data).hexdigest()
    jobs.update(job_id, content_sha256=sha)

    # 2) detect type ----------------------------------------------------------
    hint = request.file_type or Path(request.s3_key).suffix.lstrip(".").lower() or None
    kind = sniff_file_kind(data, hint)
    hinted = kind_from_hint(hint)
    if hinted and hinted is not kind:
        warnings.append(f"FILE_TYPE_MISMATCH: declared '{hint}' but content is '{kind.value}'; using detected type")

    # 3) pages (text / OCR) -- cached by content hash --------------------------
    cached = None if request.force_reprocess else _load_pages(deps, sha)
    if cached:
        pages, page_warnings = cached
        log.info("pages_cache_hit", pages=len(pages))
    else:
        jobs.update(job_id, stage="extracting_text", progress=10)
        with timed(log, "extract_pages", kind=kind.value):
            outcome = deps.extractor.extract(
                data, kind, ref=ref, storage_supports_async=deps.storage.supports_async_ocr and kind is FileKind.PDF,
                progress=lambda pct: jobs.update(job_id, progress=10 + int(pct * 0.35)),
            )
        pages, page_warnings = outcome.pages, outcome.warnings
        if sum(len(p.text.strip()) for p in pages) < s.min_total_text_chars:
            detail = "; ".join(page_warnings)[:300]
            raise NoReadableTextError("No readable text could be extracted from the document" + (f" ({detail})" if detail else ""))
        jobs.save_pages(sha, [asdict(p) for p in pages], page_warnings)
    warnings.extend(page_warnings)

    # 4) index || extract (parallel; each stage resumable) -----------------------
    jobs.update(job_id, stage="indexing_and_extracting", progress=50)
    extraction = jobs.load_extraction(job_id)
    already_indexed = rec.chunks_indexed

    def do_extract() -> ExtractionOutput:
        if extraction is not None:
            log.info("extraction_stage_reused")
            return extraction
        with timed(log, "structured_extraction", pages=len(pages)):
            out = deps.extraction.extract(
                document_id=request.document_id, pages=pages,
                progress=lambda pct: jobs.update(job_id, progress=50 + int(pct * 0.4)),
            )
        jobs.save_extraction(job_id, out)
        return out

    def do_index() -> int:
        if already_indexed is not None:
            log.info("index_stage_reused", chunks=already_indexed)
            return already_indexed
        with timed(log, "index_document"):
            n = index_document(deps, request, pages, version=job_id)
        jobs.update(job_id, chunks_indexed=n)
        return n

    ext_exc: BaseException | None = None
    idx_exc: BaseException | None = None
    with ContextThreadPoolExecutor(max_workers=2) as pool:
        f_ext, f_idx = pool.submit(do_extract), pool.submit(do_index)
        try:
            extraction_out = f_ext.result()
        except BaseException as exc:  # noqa: BLE001
            ext_exc = exc
        try:
            chunks_indexed = f_idx.result()
        except BaseException as exc:  # noqa: BLE001
            idx_exc = exc
    if ext_exc:
        raise ext_exc
    if idx_exc:
        raise idx_exc

    # 5) result ----------------------------------------------------------------
    partial = any(p.error for p in pages) or extraction_out.windows_failed > 0
    result = build_result(request, pages, extraction_out, warnings,
                          status="partial" if partial else "completed")
    deliver(deps, job_id, request, result, status="completed", chunks_indexed=chunks_indexed)


def finalize_failure(job_id: str, deps: PipelineDeps, exc: BaseException) -> None:
    """Terminal failure: NestJS must never be left waiting, so we ALWAYS report."""
    rec = deps.jobs.get(job_id)
    if rec is None:
        log.error("finalize_failure_no_record", job_id=job_id)
        return
    request = rec.request
    code = getattr(exc, "code", "INTERNAL_ERROR")
    message = getattr(exc, "message", None) or str(exc) or exc.__class__.__name__
    cached = _load_pages(deps, rec.content_sha256)
    pages, page_warnings = cached if cached else ([], [])
    extraction = deps.jobs.load_extraction(job_id)
    log.error("job_failed", error_code=code, error=message[:500], exc_info=not isinstance(exc, PermanentError))

    if extraction is not None and code in INDEX_ERROR_CODES:
        # extraction succeeded, only the semantic index failed: don't throw the extracted data away
        warnings = page_warnings + [f"INDEXING_FAILED: {message[:200]} (semantic search unavailable for this document)"]
        result = build_result(request, pages, extraction, warnings, status="partial")
        deliver(deps, job_id, request, result, status="completed", chunks_indexed=0)
        return
    result = build_result(request, pages, None, page_warnings, status="failed", error_message=f"{code}: {message}")
    deliver(deps, job_id, request, result, status="failed", chunks_indexed=rec.chunks_indexed or 0,
            error=message[:1000], error_code=code)
