"""Error taxonomy.

* `PermanentError`  -> retrying will not help (bad file, encrypted PDF ...). Job fails at once.
* `TransientError`  -> might work on retry (throttling, network ...). Celery retries with backoff.
"""
from __future__ import annotations


class ServiceError(Exception):
    code: str = "INTERNAL_ERROR"
    retryable: bool = False
    http_status: int = 500

    def __init__(self, message: str = "", *, code: str | None = None) -> None:
        self.message = message or self.__class__.__name__
        super().__init__(self.message)
        if code:
            self.code = code


class PermanentError(ServiceError):
    retryable = False


class TransientError(ServiceError):
    retryable = True


# ---- permanent (file / input problems)
class UnsupportedFileError(PermanentError):
    code = "UNSUPPORTED_FILE_TYPE"


class FileTooLargeError(PermanentError):
    code = "FILE_TOO_LARGE"


class TooManyPagesError(PermanentError):
    code = "TOO_MANY_PAGES"


class EncryptedDocumentError(PermanentError):
    code = "ENCRYPTED_DOCUMENT"


class CorruptDocumentError(PermanentError):
    code = "CORRUPT_DOCUMENT"


class StorageNotFoundError(PermanentError):
    code = "FILE_NOT_FOUND"


class NoReadableTextError(PermanentError):
    code = "NO_READABLE_TEXT"


class OcrUnavailableError(PermanentError):
    code = "OCR_UNAVAILABLE"


class OcrPageError(PermanentError):
    code = "OCR_PAGE_FAILED"


class LLMRefusalError(PermanentError):
    code = "LLM_REFUSAL"


class LLMConfigError(PermanentError):
    code = "LLM_CONFIG_ERROR"


# ---- transient (infrastructure)
class StorageError(TransientError):
    code = "STORAGE_ERROR"


class OcrError(TransientError):
    code = "OCR_ERROR"


class LLMError(TransientError):
    code = "LLM_ERROR"


class LLMOutputError(TransientError):
    code = "LLM_OUTPUT_INVALID"


class ExtractionFailedError(TransientError):
    code = "EXTRACTION_FAILED"


class EmbeddingError(TransientError):
    code = "EMBEDDING_ERROR"


class VectorStoreError(TransientError):
    code = "VECTOR_STORE_ERROR"


# ---- API-level errors (mapped to HTTP responses)
class ApiError(ServiceError):
    http_status = 400
    code = "BAD_REQUEST"


class NotFoundError(ApiError):
    http_status = 404
    code = "NOT_FOUND"


class ServiceUnavailableError(ApiError):
    http_status = 503
    code = "SERVICE_UNAVAILABLE"
