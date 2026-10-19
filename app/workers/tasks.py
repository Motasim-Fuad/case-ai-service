"""Celery tasks. Retry policy: PermanentError -> fail now; anything else -> retry with backoff, then fail."""
from __future__ import annotations

from celery.exceptions import SoftTimeLimitExceeded

from app.container import get_pipeline_deps
from app.core.config import get_settings
from app.core.errors import PermanentError
from app.core.logging import clear_job_context, get_logger
from app.services.pipeline import finalize_failure, run_pipeline
from app.workers.celery_app import celery_app

log = get_logger(__name__)
_settings = get_settings()


@celery_app.task(
    bind=True,
    name="case_ai.process_document",
    max_retries=_settings.celery_task_max_retries,
    soft_time_limit=_settings.celery_task_soft_time_limit,
    time_limit=_settings.celery_task_time_limit,
    acks_late=True,
)
def process_document(self, job_id: str) -> None:  # type: ignore[no-untyped-def]
    deps = get_pipeline_deps()
    attempt = self.request.retries + 1
    try:
        run_pipeline(job_id, deps, attempt=attempt)
    except SoftTimeLimitExceeded:
        finalize_failure(job_id, deps, PermanentError("Processing exceeded the time limit", code="PROCESSING_TIMEOUT"))
    except PermanentError as exc:
        finalize_failure(job_id, deps, exc)
    except Exception as exc:  # noqa: BLE001
        if self.request.retries >= self.max_retries:
            finalize_failure(job_id, deps, exc)
        else:
            countdown = min(15 * (2 ** self.request.retries), 300)
            log.warning("job_retrying", attempt=attempt, countdown=countdown, error=str(exc)[:300])
            deps.jobs.update(job_id, stage="retrying", status="processing", error=str(exc)[:500])
            raise self.retry(exc=exc, countdown=countdown)
    finally:
        clear_job_context()


def enqueue_job(job_id: str) -> None:
    """Send a job to the queue (task_id == job_id so Flower / logs correlate)."""
    process_document.apply_async(args=[job_id], task_id=job_id)
