"""Embedding providers: OpenAI (production) and a deterministic hash embedder (mock/offline)."""
from __future__ import annotations

import hashlib
import math
import re
from typing import Protocol

import openai
from openai import OpenAI
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_random_exponential

from app.core.concurrency import ContextThreadPoolExecutor
from app.core.config import Settings
from app.core.errors import EmbeddingError, LLMConfigError

_KNOWN_DIMS = {"text-embedding-3-small": 1536, "text-embedding-3-large": 3072, "text-embedding-ada-002": 1536}
_TRANSIENT = (openai.RateLimitError, openai.APIConnectionError, openai.APITimeoutError, openai.InternalServerError)


class Embedder(Protocol):
    @property
    def dim(self) -> int: ...

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class OpenAIEmbedder:
    def __init__(self, settings: Settings, client: OpenAI | None = None) -> None:
        self._s = settings
        self._client = client or OpenAI(
            api_key=settings.openai_api_key or None,
            base_url=settings.openai_base_url or None,
            timeout=settings.llm_timeout_seconds,
            max_retries=0,
        )
        self._dim = settings.embedding_dimensions or _KNOWN_DIMS.get(settings.openai_embedding_model)

    @property
    def dim(self) -> int:
        if self._dim is None:  # unknown model: probe once
            self._dim = len(self._embed_batch(["dimension probe"])[0])
        return self._dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        safe = [t if t.strip() else "." for t in texts]  # the API rejects empty strings
        size = max(1, self._s.embedding_batch_size)
        batches = [safe[i : i + size] for i in range(0, len(safe), size)]
        with ContextThreadPoolExecutor(max_workers=max(1, self._s.embedding_concurrency)) as pool:
            results = list(pool.map(self._embed_batch, batches))
        return [vec for batch in results for vec in batch]

    def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        @retry(
            retry=retry_if_exception_type(_TRANSIENT),
            wait=wait_random_exponential(multiplier=1, max=30),
            stop=stop_after_attempt(self._s.llm_max_attempts),
            reraise=True,
        )
        def _call():
            kwargs = {"model": self._s.openai_embedding_model, "input": batch}
            if self._s.embedding_dimensions:
                kwargs["dimensions"] = self._s.embedding_dimensions
            return self._client.embeddings.create(**kwargs)

        try:
            resp = _call()
        except (openai.AuthenticationError, openai.PermissionDeniedError, openai.NotFoundError) as exc:
            raise LLMConfigError(f"OpenAI embeddings config error: {exc}") from exc
        except openai.OpenAIError as exc:
            raise EmbeddingError(f"OpenAI embeddings failed: {exc}") from exc
        return [d.embedding for d in sorted(resp.data, key=lambda d: d.index)]


class HashEmbedder:
    """Feature-hashing bag-of-words. Offline, deterministic, lexical only (NOT semantic).
    Used when MOCK_AI=true so the whole flow can be tested without an OpenAI key."""

    def __init__(self, dim: int = 1536) -> None:
        self._dim = dim

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]

    def _one(self, text: str) -> list[float]:
        vec = [0.0] * self._dim
        for tok in re.findall(r"\w+", text.lower()):
            h = int(hashlib.md5(tok.encode()).hexdigest()[:8], 16)
            vec[h % self._dim] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]
