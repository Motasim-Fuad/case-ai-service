"""Celery application (broker = Redis). Job *state* lives in Redis via JobStore, not in Celery results."""
from __future__ import annotations

from celery import Celery
from celery.signals import setup_logging

from app.core.config import get_settings
from app.core.logging import configure_logging

settings = get_settings()

celery_app = Celery("case_ai", broker=settings.redis_url, include=["app.workers.tasks"])
celery_app.conf.update(
    task_default_queue="case_ai",
    task_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_ignore_result=True,
    task_acks_late=True,  # a job is only acknowledged when finished -> survives worker crashes
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,  # long tasks: don't hoard jobs
    broker_connection_retry_on_startup=True,
    broker_transport_options={"visibility_timeout": settings.celery_task_time_limit + 900},
    worker_hijack_root_logger=False,
    task_always_eager=settings.celery_task_always_eager,
    task_eager_propagates=False,
)


@setup_logging.connect
def _configure_logging(**_kwargs: object) -> None:
    configure_logging(settings.log_level, settings.log_json)
