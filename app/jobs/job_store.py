"""Redis-backed job state, resumable-stage cache and per-document lock."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field
from redis import Redis

from app.models.api import ProcessRequest
from app.models.extraction import DocumentExtractionResult, ExtractionOutput

PREFIX = "case_ai"
ACTIVE = ("queued", "processing")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class JobRecord(BaseModel):
    job_id: str
    document_id: str
    case_id: str
    status: str = "queued"
    stage: str = "queued"
    progress: int = 0
    attempts: int = 0
    error: str | None = None
    error_code: str | None = None
    callback_status: str = "pending"
    chunks_indexed: int | None = None
    content_sha256: str | None = None
    warnings: list[str] = Field(default_factory=list)
    created_at: str | None = None
    updated_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    request_json: str = "{}"

    @property
    def request(self) -> ProcessRequest:
        return ProcessRequest.model_validate_json(self.request_json)


class JobStore:
    def __init__(self, redis: Redis, *, ttl: int, cache_ttl: int, lock_ttl: int) -> None:
        self.r = redis
        self.ttl = ttl
        self.cache_ttl = cache_ttl
        self.lock_ttl = lock_ttl

    @staticmethod
    def _k(*parts: str) -> str:
        return ":".join((PREFIX, *parts))

    # ----------------------------------------------------------------- jobs
    def create(self, job_id: str, request: ProcessRequest) -> None:
        now = utcnow()
        mapping = {
            "job_id": job_id, "document_id": request.document_id, "case_id": request.case_id,
            "status": "queued", "stage": "queued", "progress": 0, "attempts": 0,
            "callback_status": "pending" if request.callback_url else "skipped",
            "created_at": now, "updated_at": now,
            "request_json": request.model_dump_json(),
        }
        key = self._k("job", job_id)
        pipe = self.r.pipeline()
        pipe.hset(key, mapping=mapping)
        pipe.expire(key, self.ttl)
        pipe.execute()

    def bump_deliveries(self, job_id: str) -> int:
        """Counts how many times a worker STARTED this job (survives worker crashes, unlike Celery's retry count)."""
        return int(self.r.hincrby(self._k("job", job_id), "deliveries", 1))

    def update(self, job_id: str, **fields: Any) -> None:
        key = self._k("job", job_id)
        mapping: dict[str, Any] = {"updated_at": utcnow()}
        for name, value in fields.items():
            if value is None:
                mapping[name] = ""
            elif isinstance(value, (list, dict)):
                mapping[name] = json.dumps(value)
            else:
                mapping[name] = value
        pipe = self.r.pipeline()
        pipe.hset(key, mapping=mapping)
        pipe.expire(key, self.ttl)
        pipe.execute()

    def get(self, job_id: str) -> JobRecord | None:
        raw = self.r.hgetall(self._k("job", job_id))
        if not raw:
            return None
        data: dict[str, Any] = {k: (v if v != "" else None) for k, v in raw.items()}
        data["warnings"] = json.loads(data["warnings"]) if data.get("warnings") else []
        for int_field in ("progress", "attempts", "chunks_indexed"):
            if data.get(int_field) is not None:
                data[int_field] = int(data[int_field])
        data["request_json"] = raw.get("request_json", "{}")
        return JobRecord(**data)

    # -------------------------------------------------------------- results
    def set_result(self, job_id: str, result: DocumentExtractionResult) -> None:
        self.r.set(self._k("result", job_id), result.model_dump_json(), ex=self.ttl)

    def get_result(self, job_id: str) -> DocumentExtractionResult | None:
        raw = self.r.get(self._k("result", job_id))
        return DocumentExtractionResult.model_validate_json(raw) if raw else None

    # ------------------------------------------- resumable stage outputs (per job)
    def save_extraction(self, job_id: str, output: ExtractionOutput) -> None:
        self.r.set(self._k("stage", job_id, "extraction"), output.model_dump_json(), ex=self.cache_ttl)

    def load_extraction(self, job_id: str) -> ExtractionOutput | None:
        raw = self.r.get(self._k("stage", job_id, "extraction"))
        return ExtractionOutput.model_validate_json(raw) if raw else None

    # ----------------------------------------- extracted-pages cache (per file hash)
    def save_pages(self, sha256: str, pages: list[dict[str, Any]], warnings: list[str]) -> None:
        self.r.set(self._k("pages", sha256), json.dumps({"pages": pages, "warnings": warnings}), ex=self.cache_ttl)

    def load_pages(self, sha256: str) -> tuple[list[dict[str, Any]], list[str]] | None:
        raw = self.r.get(self._k("pages", sha256))
        if not raw:
            return None
        data = json.loads(raw)
        return data["pages"], data["warnings"]

    # ---------------------------------------------- one active job per document
    def acquire_document(self, document_id: str, job_id: str) -> str | None:
        """Return None if the lock was acquired, else the job_id of the job that is already active."""
        key = self._k("doclock", document_id)
        if self.r.set(key, job_id, nx=True, ex=self.lock_ttl):
            return None
        existing = self.r.get(key)
        if existing == job_id:
            return None
        record = self.get(existing) if existing else None
        if record is not None and record.status in ACTIVE:
            return existing
        self.r.set(key, job_id, ex=self.lock_ttl)  # stale lock -> take over
        return None

    def release_document(self, document_id: str, job_id: str) -> None:
        key = self._k("doclock", document_id)
        if self.r.get(key) == job_id:
            self.r.delete(key)

    # -------------------------------------------------------------- dev helpers
    def push_dev_webhook(self, item: dict[str, Any]) -> None:
        key = self._k("dev", "webhooks")
        self.r.lpush(key, json.dumps(item))
        self.r.ltrim(key, 0, 19)

    def list_dev_webhooks(self) -> list[dict[str, Any]]:
        return [json.loads(x) for x in self.r.lrange(self._k("dev", "webhooks"), 0, 19)]

    def ping(self) -> None:
        self.r.ping()
