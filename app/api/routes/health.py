from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from app.api.deps import get_job_store, get_settings, get_vector_store
from app.core.config import Settings
from app.jobs.job_store import JobStore
from app.services.vector_store import QdrantVectorStore

router = APIRouter(tags=["health"])


def _check(fn) -> str:  # noqa: ANN001
    try:
        fn()
        return "ok"
    except Exception as exc:  # noqa: BLE001
        return f"error: {str(exc)[:120]}"


@router.get("/health", summary="Liveness + dependency check (Redis, Qdrant)")
def health(
    response: Response,
    settings: Settings = Depends(get_settings),
    jobs: JobStore = Depends(get_job_store),
) -> dict:
    checks = {"redis": _check(jobs.ping)}
    try:
        vectors: QdrantVectorStore = get_vector_store()
        checks["qdrant"] = _check(vectors.ping)
    except Exception as exc:  # noqa: BLE001
        checks["qdrant"] = f"error: {str(exc)[:120]}"
    healthy = all(v == "ok" for v in checks.values())
    response.status_code = 200 if healthy else 503
    return {
        "status": "ok" if healthy else "degraded",
        "service": settings.service_name,
        "version": settings.service_version,
        "environment": settings.environment,
        "mock_ai": settings.mock_ai,
        "checks": checks,
    }
