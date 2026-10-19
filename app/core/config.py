"""Central configuration. Every setting can be overridden with an environment variable
(or a `.env` file). Names are the upper-cased field names, e.g. `OPENAI_API_KEY`."""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEFAULT_DEV_KEY = "dev-internal-key"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ------------------------------------------------------------------ app
    environment: Literal["development", "test", "production"] = "development"
    service_name: str = "case-ai-service"
    service_version: str = "1.0.0"
    log_level: str = "INFO"
    log_json: bool = True
    api_prefix: str = "/api/v1"
    internal_api_key: str = _DEFAULT_DEV_KEY
    webhook_signing_secret: str = ""  # optional HMAC signature for webhooks
    callback_allowed_hosts: str = ""  # comma separated; empty = allow any (dev only)
    allowed_s3_buckets: str = ""  # comma separated; empty = only S3_BUCKET_NAME
    enable_dev_endpoints: bool | None = None  # default: on unless production
    dev_webhook_echo_url: str = "http://api:8000/api/v1/dev/webhook-echo"

    # ------------------------------------------------------------- AI switch
    mock_ai: bool = False  # True = offline heuristics instead of OpenAI (smoke tests only)

    # ---------------------------------------------------------------- OpenAI
    openai_api_key: str = ""
    openai_base_url: str = ""
    openai_extraction_model: str = "gpt-4.1-mini"
    openai_qa_model: str = "gpt-4.1-mini"
    openai_embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int | None = None
    embedding_batch_size: int = 64
    embedding_concurrency: int = 4
    llm_timeout_seconds: float = 120.0
    llm_max_attempts: int = 5
    llm_temperature: float | None = None  # leave unset for models that only allow the default

    # ------------------------------------------------------------------- AWS
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_region: str = "us-east-1"
    aws_endpoint_url: str = ""  # e.g. MinIO / LocalStack
    s3_bucket_name: str = ""
    storage_backend: Literal["s3", "local"] = "s3"
    local_storage_dir: str = "./data/uploads"

    # ------------------------------------------------------- file / OCR limits
    max_file_size_mb: int = 50
    max_pages: int = 300
    scanned_page_min_chars: int = 40
    min_total_text_chars: int = 20
    textract_mode: Literal["auto", "sync", "async"] = "auto"
    textract_async_page_threshold: int = 15  # >N scanned pages (and S3 storage) -> async API
    textract_concurrency: int = 4
    max_textract_wait_seconds: int = 300
    ocr_render_dpi: int = 200

    # ---------------------------------------------------------- chunk / index
    chunk_size_chars: int = 2400  # ~600 tokens
    chunk_overlap_chars: int = 300
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: str = ""
    qdrant_collection: str = "case_chunks"

    # -------------------------------------------------------------- extraction
    extraction_single_call_max_chars: int = 60000  # ~15k tokens: one LLM call
    extraction_window_chars: int = 24000  # ~6k tokens per window for long docs
    extraction_window_overlap_pages: int = 1
    extraction_concurrency: int = 3

    # --------------------------------------------------------------------- RAG
    rag_top_k: int = 6
    rag_score_threshold: float | None = None

    # ------------------------------------------------------------ jobs / celery
    redis_url: str = "redis://localhost:6379/0"
    job_ttl_seconds: int = 60 * 60 * 24 * 7
    pages_cache_ttl_seconds: int = 60 * 60 * 24 * 7
    document_lock_ttl_seconds: int = 60 * 60
    celery_task_max_retries: int = 3
    celery_task_soft_time_limit: int = 1500
    celery_task_time_limit: int = 1800
    celery_task_always_eager: bool = False  # tests only

    # ----------------------------------------------------------------- webhook
    webhook_timeout_seconds: float = 20.0
    webhook_max_attempts: int = 5

    # ---------------------------------------------------------------- derived
    @property
    def dev_endpoints_enabled(self) -> bool:
        if self.enable_dev_endpoints is not None:
            return self.enable_dev_endpoints
        return self.environment != "production"

    @property
    def max_file_bytes(self) -> int:
        return self.max_file_size_mb * 1024 * 1024

    @property
    def callback_hosts(self) -> set[str]:
        return {h.strip().lower() for h in self.callback_allowed_hosts.split(",") if h.strip()}

    @property
    def s3_buckets_allowed(self) -> set[str]:
        extra = {b.strip() for b in self.allowed_s3_buckets.split(",") if b.strip()}
        if self.s3_bucket_name:
            extra.add(self.s3_bucket_name)
        return extra

    @property
    def qdrant_collection_name(self) -> str:
        """Mock embeddings must never be mixed with real ones in the same collection."""
        return f"{self.qdrant_collection}_mock" if self.mock_ai else self.qdrant_collection

    @model_validator(mode="after")
    def _check_production(self) -> "Settings":
        if self.environment == "production":
            if not self.internal_api_key or self.internal_api_key == _DEFAULT_DEV_KEY:
                raise ValueError("INTERNAL_API_KEY must be set to a strong secret in production")
            if self.mock_ai:
                raise ValueError("MOCK_AI must be false in production")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
