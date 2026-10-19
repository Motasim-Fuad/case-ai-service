"""Runs the real Celery task in eager mode (no broker) against the in-memory harness."""
import pytest

import app.workers.tasks as tasks
from app.core.errors import VectorStoreError
from tests.helpers import make_text_pdf


@pytest.fixture
def eager(harness, monkeypatch):
    monkeypatch.setattr(tasks, "get_pipeline_deps", lambda: harness.deps)
    tasks.celery_app.conf.task_always_eager = True
    tasks.celery_app.conf.task_eager_propagates = False
    yield harness
    tasks.celery_app.conf.task_always_eager = False


def test_task_success(eager):
    job_id = eager.submit("letter.pdf", make_text_pdf(), run=False)
    tasks.process_document.apply(args=[job_id], task_id=job_id)
    assert eager.jobs.get(job_id).status == "completed"


def test_task_permanent_error_fails_immediately_with_webhook(eager):
    job_id = eager.submit("missing.pdf", None, run=False)
    tasks.process_document.apply(args=[job_id], task_id=job_id)
    rec = eager.jobs.get(job_id)
    assert rec.status == "failed" and rec.error_code == "FILE_NOT_FOUND" and rec.attempts == 1
    assert eager.webhook.sent[-1][1].status == "failed"


def test_task_retries_transient_then_succeeds(eager, monkeypatch):
    monkeypatch.setattr(tasks.process_document, "retry", tasks.process_document.retry)  # real retry
    job_id = eager.submit("letter.pdf", make_text_pdf(), run=False)
    real = eager.deps.vectors.replace_document
    state = {"n": 0}

    def flaky(**kw):
        state["n"] += 1
        if state["n"] < 2:
            raise VectorStoreError("temporary")
        return real(**kw)

    monkeypatch.setattr(eager.deps.vectors, "replace_document", flaky)
    tasks.process_document.apply(args=[job_id], task_id=job_id)
    rec = eager.jobs.get(job_id)
    assert rec.status == "completed" and rec.attempts == 2


def test_task_gives_up_after_max_retries_but_still_reports(eager, monkeypatch):
    job_id = eager.submit("letter.pdf", make_text_pdf(), run=False)
    monkeypatch.setattr(eager.deps.vectors, "replace_document",
                        lambda **kw: (_ for _ in ()).throw(VectorStoreError("always down")))
    tasks.process_document.apply(args=[job_id], task_id=job_id)
    rec = eager.jobs.get(job_id)
    result = eager.jobs.get_result(job_id)
    assert rec.status == "completed" and result.processing_status == "partial"  # degraded, NestJS still notified
    assert any(w.startswith("INDEXING_FAILED") for w in result.warnings)
    assert eager.webhook.sent[-1][1].chunks_indexed == 0
