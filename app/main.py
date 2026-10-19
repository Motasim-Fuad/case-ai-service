"""FastAPI application factory."""
from __future__ import annotations

import time
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from app import __version__
from app.api.routes import dev, documents, health, query
from app.core.config import get_settings
from app.core.errors import ApiError, ServiceError
from app.core.logging import bind_job_context, clear_job_context, configure_logging, get_logger
from app.core.security import require_api_key

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ANN201
    s = get_settings()
    log.info("service_starting", environment=s.environment, mock_ai=s.mock_ai, storage=s.storage_backend)
    if s.environment == "production" and not s.callback_hosts:
        log.warning("config_warning", detail="CALLBACK_ALLOWED_HOSTS is empty: any callback host is accepted")
    try:  # warm-up: create the Qdrant collection early (non-fatal, retried lazily on first use)
        from app.container import get_vector_store

        get_vector_store().ensure_ready()
    except Exception as exc:  # noqa: BLE001
        log.warning("qdrant_not_ready_at_startup", error=str(exc)[:200])
    yield


def create_app() -> FastAPI:
    s = get_settings()
    configure_logging(s.log_level, s.log_json)
    app = FastAPI(
        title="Case AI Service",
        version=__version__,
        description="Document intelligence for the AI Case Assistant: OCR, structured extraction, RAG. "
                    "Called by the NestJS backend only. Authenticate with the `X-Internal-Api-Key` header.",
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def access_log(request: Request, call_next):  # noqa: ANN001, ANN202
        rid = request.headers.get("X-Request-Id") or uuid.uuid4().hex[:12]
        bind_job_context(request_id=rid)
        start = time.monotonic()
        try:
            response = await call_next(request)
        finally:
            duration = int((time.monotonic() - start) * 1000)
            if request.url.path != "/health":
                log.info("http_request", method=request.method, path=request.url.path, duration_ms=duration)
            clear_job_context()
        response.headers["X-Request-Id"] = rid
        return response

    @app.exception_handler(ServiceError)
    async def service_error_handler(_request: Request, exc: ServiceError) -> JSONResponse:
        status = exc.http_status if isinstance(exc, ApiError) else (503 if exc.retryable else 500)
        return JSONResponse(status_code=status, content={"error": {"code": exc.code, "message": exc.message}})

    app.include_router(health.router)
    v1_deps = [Depends(require_api_key)]
    app.include_router(documents.router, prefix=s.api_prefix, dependencies=v1_deps)
    app.include_router(query.router, prefix=s.api_prefix, dependencies=v1_deps)
    if s.dev_endpoints_enabled:
        app.include_router(dev.router, prefix=s.api_prefix, dependencies=v1_deps)
    return app


app = create_app()
