"""Request / response models of the public HTTP API and the webhook."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, HttpUrl

from app.models.extraction import SCHEMA_VERSION, DocumentExtractionResult

FileTypeLiteral = Literal["pdf", "jpg", "jpeg", "png", "tif", "tiff", "docx", "txt"]
JobStatus = Literal["queued", "processing", "completed", "failed"]
JobStage = Literal[
    "queued", "fetching_file", "extracting_text", "indexing_and_extracting",
    "retrying", "sending_callback", "done", "failed",
]


class ProcessRequest(BaseModel):
    document_id: str = Field(min_length=1, max_length=128)
    case_id: str = Field(min_length=1, max_length=128)
    s3_bucket: str | None = Field(default=None, description="Defaults to S3_BUCKET_NAME.")
    s3_key: str = Field(min_length=1, max_length=1024)
    file_type: FileTypeLiteral | None = Field(
        default=None, description="Hint only. The real type is detected from the file bytes."
    )
    callback_url: HttpUrl | None = Field(
        default=None, description="Webhook target. Omit to use polling only."
    )
    force_reprocess: bool = Field(default=False, description="Ignore the extracted-text cache.")
    metadata: dict[str, str] = Field(
        default_factory=dict, description="Opaque values echoed back in the webhook (e.g. request id)."
    )


class ProcessAccepted(BaseModel):
    job_id: str
    status: JobStatus
    document_id: str
    deduplicated: bool = Field(
        default=False, description="True when an active job for this document already existed."
    )


class JobStatusResponse(BaseModel):
    job_id: str
    document_id: str
    case_id: str
    status: JobStatus
    stage: str
    progress: int = Field(ge=0, le=100)
    attempts: int = 0
    result: DocumentExtractionResult | None = None
    chunks_indexed: int | None = None
    error: str | None = None
    error_code: str | None = None
    warnings: list[str] = Field(default_factory=list)
    callback_status: Literal["pending", "delivered", "failed", "skipped"] = "pending"
    created_at: str | None = None
    updated_at: str | None = None
    finished_at: str | None = None


class WebhookPayload(BaseModel):
    schema_version: str = SCHEMA_VERSION
    job_id: str
    document_id: str
    case_id: str
    status: Literal["completed", "failed"]
    extracted_data: DocumentExtractionResult
    chunks_indexed: int = 0
    pages_total: int = 0
    pages_failed: int = 0
    duration_ms: int | None = None
    error: str | None = None
    error_code: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)


# ------------------------------------------------------------------- RAG / search
class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=4000)


class QueryRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    top_k: int | None = Field(default=None, ge=1, le=20)
    document_ids: list[str] | None = Field(default=None, description="Restrict to these documents.")
    history: list[ChatTurn] = Field(default_factory=list, max_length=10)


class SourceRef(BaseModel):
    document_id: str
    page: int | None
    chunk_index: int | None = None
    score: float | None = None
    chunk_text_snippet: str


class QueryResponse(BaseModel):
    answer: str
    answerable: bool
    sources: list[SourceRef]


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    top_k: int | None = Field(default=None, ge=1, le=50)
    document_ids: list[str] | None = None


class SearchHit(BaseModel):
    document_id: str
    page: int | None
    chunk_index: int | None
    score: float
    text: str


class SearchResponse(BaseModel):
    results: list[SearchHit]


class DeleteIndexResponse(BaseModel):
    deleted: bool = True
    scope: str
    id: str
