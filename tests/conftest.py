from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from app.core.config import Settings
from app.dev_harness import CapturingWebhook, build_inmemory_deps
from app.jobs.job_store import JobStore
from app.models.api import ProcessRequest
from app.services.pipeline import PipelineDeps, run_pipeline
from app.services.rag import RagService
from app.services.storage import LocalStorage
from app.services.textract import OcrResult
from tests.helpers import LETTER


class FakeOcr:
    """Stands in for AWS Textract."""

    def __init__(self, text: str = LETTER, confidence: float = 0.96) -> None:
        self.text, self.confidence = text, confidence
        self.image_calls = 0
        self.async_calls = 0

    def ocr_image(self, data: bytes) -> OcrResult:
        self.image_calls += 1
        return OcrResult(self.text, self.confidence)

    def ocr_s3_pdf(self, bucket: str, key: str) -> dict[int, OcrResult]:
        self.async_calls += 1
        return {1: OcrResult(self.text, self.confidence)}


@dataclass
class Harness:
    settings: Settings
    deps: PipelineDeps
    rag: RagService
    webhook: CapturingWebhook
    ocr: FakeOcr
    counter: list[int] = field(default_factory=lambda: [0])

    @property
    def jobs(self) -> JobStore:
        return self.deps.jobs

    def submit(self, filename: str, data: bytes | None = None, *, case_id: str = "case-1",
               document_id: str = "doc-1", callback: bool = True, run: bool = True, **extra) -> str:
        storage = self.deps.storage
        assert isinstance(storage, LocalStorage)
        if data is not None:
            storage.save(f"t/{document_id}/{filename}", data)
        self.counter[0] += 1
        job_id = f"job-{self.counter[0]}"
        request = ProcessRequest(
            document_id=document_id, case_id=case_id, s3_key=f"t/{document_id}/{filename}",
            callback_url="http://nest.test/hook" if callback else None, **extra,
        )
        self.jobs.create(job_id, request)
        if run:
            run_pipeline(job_id, self.deps)
        return job_id


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None, environment="test", mock_ai=True, storage_backend="local",
        local_storage_dir=str(tmp_path / "uploads"), qdrant_url=":memory:", log_json=False,
        textract_concurrency=2, webhook_signing_secret="",
    )


@pytest.fixture
def harness(settings) -> Harness:
    webhook, ocr = CapturingWebhook(), FakeOcr()
    deps, rag = build_inmemory_deps(settings, ocr=ocr, webhook=webhook)
    return Harness(settings=settings, deps=deps, rag=rag, webhook=webhook, ocr=ocr)
