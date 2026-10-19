"""Dependency container: builds and caches the service singletons (one per process)."""
from __future__ import annotations

from functools import lru_cache

import redis

from app.core.config import get_settings
from app.jobs.job_store import JobStore
from app.services.embeddings import Embedder, HashEmbedder, OpenAIEmbedder
from app.services.extraction import ExtractionService
from app.services.llm import LLMClient, MockLLM, OpenAILLM
from app.services.page_extractor import PageExtractor
from app.services.pipeline import PipelineDeps
from app.services.rag import RagService
from app.services.storage import Storage, build_storage
from app.services.textract import TextractOcr
from app.services.vector_store import QdrantVectorStore
from app.services.webhook import WebhookClient


@lru_cache
def get_redis() -> redis.Redis:
    s = get_settings()
    return redis.Redis.from_url(
        s.redis_url, decode_responses=True, socket_timeout=10, socket_connect_timeout=5, health_check_interval=30
    )


@lru_cache
def get_job_store() -> JobStore:
    s = get_settings()
    return JobStore(get_redis(), ttl=s.job_ttl_seconds, cache_ttl=s.pages_cache_ttl_seconds, lock_ttl=s.document_lock_ttl_seconds)


@lru_cache
def get_storage() -> Storage:
    return build_storage(get_settings())


@lru_cache
def get_embedder() -> Embedder:
    s = get_settings()
    return HashEmbedder(s.embedding_dimensions or 1536) if s.mock_ai else OpenAIEmbedder(s)


@lru_cache
def get_llm() -> LLMClient:
    s = get_settings()
    return MockLLM() if s.mock_ai else OpenAILLM(s)


@lru_cache
def get_vector_store() -> QdrantVectorStore:
    return QdrantVectorStore(get_settings(), dim=get_embedder().dim)


@lru_cache
def get_page_extractor() -> PageExtractor:
    s = get_settings()
    return PageExtractor(s, TextractOcr(s))


@lru_cache
def get_extraction_service() -> ExtractionService:
    return ExtractionService(get_settings(), get_llm())


@lru_cache
def get_rag_service() -> RagService:
    return RagService(get_settings(), get_embedder(), get_vector_store(), get_llm())


@lru_cache
def get_webhook_client() -> WebhookClient:
    return WebhookClient(get_settings())


@lru_cache
def get_pipeline_deps() -> PipelineDeps:
    return PipelineDeps(
        settings=get_settings(), storage=get_storage(), extractor=get_page_extractor(),
        embedder=get_embedder(), vectors=get_vector_store(), extraction=get_extraction_service(),
        jobs=get_job_store(), webhook=get_webhook_client(),
    )


def reset_container() -> None:
    for fn in (get_redis, get_job_store, get_storage, get_embedder, get_llm, get_vector_store,
               get_page_extractor, get_extraction_service, get_rag_service, get_webhook_client, get_pipeline_deps):
        fn.cache_clear()
