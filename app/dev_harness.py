"""In-memory wiring of the whole pipeline (fake Redis + in-memory Qdrant + local files).
Used by the tests and by `scripts/demo_local.py`, so you can run the full flow WITHOUT Docker.
Requires the dev dependencies (fakeredis)."""
from __future__ import annotations

from app.core.config import Settings
from app.jobs.job_store import JobStore
from app.models.api import WebhookPayload
from app.services.embeddings import Embedder, HashEmbedder, OpenAIEmbedder
from app.services.extraction import ExtractionService
from app.services.llm import LLMClient, MockLLM, OpenAILLM
from app.services.page_extractor import PageExtractor
from app.services.pipeline import PipelineDeps
from app.services.rag import RagService
from app.services.storage import LocalStorage
from app.services.textract import OcrEngine, TextractOcr
from app.services.vector_store import QdrantVectorStore


class CapturingWebhook:
    """Stands in for the NestJS webhook: keeps every payload it would have sent."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, WebhookPayload]] = []

    def send(self, url: str, payload: WebhookPayload) -> None:
        self.sent.append((url, payload))


def build_inmemory_deps(
    settings: Settings,
    *,
    ocr: OcrEngine | None = None,
    llm: LLMClient | None = None,
    embedder: Embedder | None = None,
    webhook: CapturingWebhook | None = None,
) -> tuple[PipelineDeps, RagService]:
    import fakeredis
    from qdrant_client import QdrantClient

    embedder = embedder or (HashEmbedder(settings.embedding_dimensions or 1536) if settings.mock_ai else OpenAIEmbedder(settings))
    llm = llm or (MockLLM() if settings.mock_ai else OpenAILLM(settings))
    jobs = JobStore(
        fakeredis.FakeRedis(decode_responses=True),
        ttl=settings.job_ttl_seconds, cache_ttl=settings.pages_cache_ttl_seconds, lock_ttl=settings.document_lock_ttl_seconds,
    )
    vectors = QdrantVectorStore(settings, dim=embedder.dim, client=QdrantClient(location=":memory:"))
    deps = PipelineDeps(
        settings=settings,
        storage=LocalStorage(settings.local_storage_dir),
        extractor=PageExtractor(settings, ocr if ocr is not None else _default_ocr(settings)),
        embedder=embedder,
        vectors=vectors,
        extraction=ExtractionService(settings, llm),
        jobs=jobs,
        webhook=webhook or CapturingWebhook(),  # type: ignore[arg-type]
    )
    return deps, RagService(settings, embedder, vectors, llm)


def _default_ocr(settings: Settings) -> OcrEngine | None:
    return TextractOcr(settings) if (not settings.mock_ai or settings.aws_access_key_id) else None
