"""Delivers the result to the NestJS webhook (retried with exponential backoff)."""
from __future__ import annotations

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_random_exponential

from app.core.config import Settings
from app.core.errors import TransientError
from app.core.logging import get_logger
from app.core.security import sign_body
from app.models.api import WebhookPayload

log = get_logger(__name__)


class WebhookDeliveryError(TransientError):
    code = "WEBHOOK_FAILED"


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code >= 500 or code in (408, 425, 429)
    return isinstance(exc, httpx.TransportError)


class WebhookClient:
    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        self.s = settings
        self._client = client or httpx.Client(timeout=settings.webhook_timeout_seconds)

    def send(self, url: str, payload: WebhookPayload) -> None:
        body = payload.model_dump_json().encode()
        headers = {
            "Content-Type": "application/json",
            "X-Internal-Api-Key": self.s.internal_api_key,
            "Idempotency-Key": payload.job_id,
        }
        if self.s.webhook_signing_secret:
            headers["X-Signature"] = sign_body(self.s.webhook_signing_secret, body)

        @retry(
            retry=retry_if_exception(_retryable),
            wait=wait_random_exponential(multiplier=2, max=60),
            stop=stop_after_attempt(self.s.webhook_max_attempts),
            reraise=True,
        )
        def _post() -> None:
            resp = self._client.post(url, content=body, headers=headers)
            resp.raise_for_status()

        try:
            _post()
        except httpx.HTTPError as exc:
            raise WebhookDeliveryError(f"Webhook delivery failed: {exc}") from exc
        log.info("webhook_delivered", url=url, status=payload.status)
