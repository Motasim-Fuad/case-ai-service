"""Structured (JSON) logging with per-job context variables."""
from __future__ import annotations

import logging
import sys
import time
from contextlib import contextmanager
from typing import Iterator

import structlog


def configure_logging(level: str = "INFO", json_logs: bool = True) -> None:
    shared = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]
    structlog.configure(
        processors=shared + [structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    renderer = (
        structlog.processors.JSONRenderer()
        if json_logs
        else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    )
    post = [structlog.stdlib.ProcessorFormatter.remove_processors_meta]
    if json_logs:
        post.append(structlog.processors.format_exc_info)
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared, processors=post + [renderer]
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
    for noisy in ("httpx", "httpcore", "botocore", "boto3", "urllib3", "pdfminer", "qdrant_client"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)


def bind_job_context(**kwargs: object) -> None:
    structlog.contextvars.bind_contextvars(**kwargs)


def clear_job_context() -> None:
    structlog.contextvars.clear_contextvars()


@contextmanager
def timed(log: structlog.stdlib.BoundLogger, step: str, **extra: object) -> Iterator[None]:
    """Log start/finish + duration of a pipeline step (so slow jobs are diagnosable from logs)."""
    start = time.monotonic()
    log.info("step_started", step=step, **extra)
    try:
        yield
    except Exception as exc:
        log.warning("step_failed", step=step, duration_ms=_ms(start), error=str(exc)[:300], **extra)
        raise
    log.info("step_finished", step=step, duration_ms=_ms(start), **extra)


def _ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)
