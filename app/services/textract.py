"""AWS Textract OCR adapter.

* images / single pages -> synchronous `DetectDocumentText` (fast, image bytes <= 10 MB)
* large scanned PDFs     -> asynchronous `StartDocumentTextDetection` straight from S3 (no download)
"""
from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Callable, Protocol

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_random_exponential

from app.core.config import Settings
from app.core.errors import OcrError, OcrPageError, OcrUnavailableError
from app.core.logging import get_logger

log = get_logger(__name__)

_RETRYABLE = {
    "ThrottlingException", "ProvisionedThroughputExceededException", "InternalServerError",
    "ServiceUnavailableException", "LimitExceededException", "RequestTimeout", "RequestTimeoutException",
}
_AUTH = {"AccessDeniedException", "UnrecognizedClientException", "InvalidSignatureException",
         "ExpiredTokenException", "InvalidClientTokenId", "AuthFailure"}
_BAD_DOC = {"InvalidParameterException", "UnsupportedDocumentException", "BadDocumentException",
            "DocumentTooLargeException", "InvalidS3ObjectException"}


@dataclass
class OcrResult:
    text: str
    confidence: float | None = None


class OcrEngine(Protocol):
    def ocr_image(self, data: bytes) -> OcrResult: ...

    def ocr_s3_pdf(self, bucket: str, key: str) -> dict[int, OcrResult]: ...


def _code(exc: BaseException) -> str:
    if isinstance(exc, ClientError):
        return str(exc.response.get("Error", {}).get("Code", ""))
    return ""


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, ClientError):
        return _code(exc) in _RETRYABLE
    return isinstance(exc, BotoCoreError) and not isinstance(exc, NoCredentialsError)


def blocks_to_pages(blocks: list[dict[str, Any]]) -> dict[int, OcrResult]:
    """Group Textract LINE blocks by page (Textract returns them in reading order)."""
    lines: dict[int, list[str]] = defaultdict(list)
    confs: dict[int, list[float]] = defaultdict(list)
    for block in blocks:
        if block.get("BlockType") != "LINE":
            continue
        page = int(block.get("Page", 1))
        lines[page].append(block.get("Text", ""))
        if "Confidence" in block:
            confs[page].append(float(block["Confidence"]))
    return {
        page: OcrResult(
            text="\n".join(texts),
            confidence=round(sum(confs[page]) / len(confs[page]) / 100, 4) if confs[page] else None,
        )
        for page, texts in lines.items()
    }


class TextractOcr:
    def __init__(self, settings: Settings, client=None, sleep: Callable[[float], None] = time.sleep) -> None:
        self._settings = settings
        self._sleep = sleep
        self._client = client or boto3.client(
            "textract",
            region_name=settings.aws_region or None,
            aws_access_key_id=settings.aws_access_key_id or None,
            aws_secret_access_key=settings.aws_secret_access_key or None,
            endpoint_url=settings.aws_endpoint_url or None,
            config=Config(retries={"max_attempts": 3, "mode": "adaptive"}, connect_timeout=5, read_timeout=60),
        )

    # ------------------------------------------------------------------ sync
    def ocr_image(self, data: bytes) -> OcrResult:
        response = self._call(self._client.detect_document_text, Document={"Bytes": data})
        return blocks_to_pages(response.get("Blocks", [])).get(1, OcrResult(text="", confidence=None))

    # ----------------------------------------------------------------- async
    def ocr_s3_pdf(self, bucket: str, key: str) -> dict[int, OcrResult]:
        start = self._call(
            self._client.start_document_text_detection,
            DocumentLocation={"S3Object": {"Bucket": bucket, "Name": key}},
        )
        job_id = start["JobId"]
        deadline = time.monotonic() + self._settings.max_textract_wait_seconds
        delay = 2.0
        while True:
            resp = self._call(self._client.get_document_text_detection, JobId=job_id)
            status = resp.get("JobStatus")
            if status in ("SUCCEEDED", "PARTIAL_SUCCESS"):
                break
            if status == "FAILED":
                raise OcrError(f"Textract job failed: {resp.get('StatusMessage', 'unknown')}")
            if time.monotonic() > deadline:
                raise OcrError("Textract job timed out", code="OCR_TIMEOUT")
            self._sleep(delay)
            delay = min(delay * 1.5, 10.0)
        blocks = list(resp.get("Blocks", []))
        token = resp.get("NextToken")
        while token:
            resp = self._call(self._client.get_document_text_detection, JobId=job_id, NextToken=token)
            blocks.extend(resp.get("Blocks", []))
            token = resp.get("NextToken")
        log.info("textract_async_done", job=job_id, pages=len({b.get("Page", 1) for b in blocks}))
        return blocks_to_pages(blocks)

    # ---------------------------------------------------------------- plumbing
    def _call(self, fn: Callable[..., Any], **kwargs: Any) -> Any:
        @retry(
            retry=retry_if_exception(_is_retryable),
            wait=wait_random_exponential(multiplier=1, max=20),
            stop=stop_after_attempt(5),
            reraise=True,
        )
        def _do() -> Any:
            return fn(**kwargs)

        try:
            return _do()
        except NoCredentialsError as exc:
            raise OcrUnavailableError("AWS credentials not configured for Textract") from exc
        except ClientError as exc:
            code = _code(exc)
            if code in _AUTH:
                raise OcrUnavailableError(f"Textract access denied ({code}). Check AWS keys / IAM policy") from exc
            if code in _BAD_DOC:
                raise OcrPageError(f"Textract rejected the document ({code})") from exc
            raise OcrError(f"Textract error ({code or exc})") from exc
        except BotoCoreError as exc:
            raise OcrError(f"Textract connection error: {exc}") from exc
