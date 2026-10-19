"""FastAPI dependencies (thin wrappers so tests can override them)."""
from __future__ import annotations

from typing import Callable

from app.container import (  # noqa: F401
    get_job_store, get_rag_service, get_redis, get_storage, get_vector_store,
)
from app.core.config import Settings, get_settings  # noqa: F401


def get_enqueue() -> Callable[[str], None]:
    from app.workers.tasks import enqueue_job

    return enqueue_job
