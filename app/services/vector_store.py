"""Qdrant vector store.

Design notes
* one collection, every point carries payload {case_id, document_id, page, chunk_index, text, index_version}
* keyword payload indexes on case_id / document_id => fast filtered search (multi-tenant safe)
* deterministic point ids (uuid5 of document_id+chunk_index) => re-processing overwrites, never duplicates
* "upsert new version, then delete old versions" => no window where a document has zero vectors
"""
from __future__ import annotations

import uuid
import warnings
from dataclasses import dataclass
from typing import Sequence

from qdrant_client import QdrantClient
from qdrant_client import models as qm

from app.core.config import Settings
from app.core.errors import PermanentError, VectorStoreError
from app.core.logging import get_logger
from app.services.chunking import Chunk

log = get_logger(__name__)
_NS = uuid.UUID("6f3c9c1e-5b7a-4f0e-9d0a-2f6b1c7a8e11")


@dataclass
class Hit:
    document_id: str
    page: int | None
    chunk_index: int | None
    score: float
    text: str


def _match(field: str, value: str) -> qm.FieldCondition:
    return qm.FieldCondition(key=field, match=qm.MatchValue(value=value))


class QdrantVectorStore:
    def __init__(self, settings: Settings, dim: int, client: QdrantClient | None = None) -> None:
        self._s = settings
        self._dim = dim
        self._collection = settings.qdrant_collection_name
        self._ready = False
        if client is not None:
            self._client = client
        elif settings.qdrant_url == ":memory:":
            self._client = QdrantClient(location=":memory:")
        else:
            self._client = QdrantClient(
                url=settings.qdrant_url, api_key=settings.qdrant_api_key or None, timeout=30
            )

    # -------------------------------------------------------------- lifecycle
    def ensure_ready(self) -> None:
        if self._ready:
            return
        try:
            if not self._client.collection_exists(self._collection):
                self._client.create_collection(
                    collection_name=self._collection,
                    vectors_config=qm.VectorParams(size=self._dim, distance=qm.Distance.COSINE),
                )
                log.info("qdrant_collection_created", collection=self._collection, dim=self._dim)
            else:
                info = self._client.get_collection(self._collection)
                existing = info.config.params.vectors
                size = getattr(existing, "size", None)
                if size is not None and size != self._dim:
                    raise PermanentError(
                        f"Collection '{self._collection}' has vector size {size} but the embedding model "
                        f"produces {self._dim}. Use a new QDRANT_COLLECTION or delete the old one.",
                        code="VECTOR_DIM_MISMATCH",
                    )
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")  # local (in-memory) mode ignores payload indexes; server mode uses them
                for field in ("case_id", "document_id"):
                    self._client.create_payload_index(
                        self._collection, field_name=field, field_schema=qm.PayloadSchemaType.KEYWORD
                    )
        except (VectorStoreError, PermanentError):
            raise
        except Exception as exc:  # noqa: BLE001
            raise VectorStoreError(f"Qdrant unavailable: {exc}") from exc
        self._ready = True

    def ping(self) -> None:
        try:
            self._client.get_collections()
        except Exception as exc:  # noqa: BLE001
            raise VectorStoreError(f"Qdrant unavailable: {exc}") from exc

    # ------------------------------------------------------------------ write
    def replace_document(
        self,
        *,
        case_id: str,
        document_id: str,
        chunks: Sequence[Chunk],
        vectors: Sequence[Sequence[float]],
        version: str,
    ) -> int:
        self.ensure_ready()
        if len(chunks) != len(vectors):
            raise VectorStoreError("chunks/vectors length mismatch")
        try:
            points = [
                qm.PointStruct(
                    id=str(uuid.uuid5(_NS, f"{document_id}:{c.chunk_index}")),
                    vector=list(v),
                    payload={
                        "case_id": case_id, "document_id": document_id, "page": c.page,
                        "chunk_index": c.chunk_index, "text": c.text, "index_version": version,
                    },
                )
                for c, v in zip(chunks, vectors)
            ]
            for i in range(0, len(points), 128):
                self._client.upsert(self._collection, points=points[i : i + 128], wait=True)
            # drop stale points from earlier versions of this document
            self._client.delete(
                self._collection,
                points_selector=qm.FilterSelector(
                    filter=qm.Filter(
                        must=[_match("document_id", document_id)],
                        must_not=[_match("index_version", version)],
                    )
                ),
                wait=True,
            )
        except Exception as exc:  # noqa: BLE001
            raise VectorStoreError(f"Qdrant write failed: {exc}") from exc
        return len(points)

    def delete_document(self, document_id: str) -> None:
        self._delete(qm.Filter(must=[_match("document_id", document_id)]))

    def delete_case(self, case_id: str) -> None:
        self._delete(qm.Filter(must=[_match("case_id", case_id)]))

    def _delete(self, flt: qm.Filter) -> None:
        self.ensure_ready()
        try:
            self._client.delete(self._collection, points_selector=qm.FilterSelector(filter=flt), wait=True)
        except Exception as exc:  # noqa: BLE001
            raise VectorStoreError(f"Qdrant delete failed: {exc}") from exc

    # ------------------------------------------------------------------- read
    def search(
        self,
        *,
        vector: Sequence[float],
        case_id: str,
        top_k: int,
        document_ids: Sequence[str] | None = None,
        score_threshold: float | None = None,
    ) -> list[Hit]:
        """Always filtered by case_id: a case can never see another case's documents."""
        self.ensure_ready()
        must: list[qm.Condition] = [_match("case_id", case_id)]
        if document_ids:
            must.append(qm.Filter(should=[_match("document_id", d) for d in document_ids]))
        try:
            res = self._client.query_points(
                self._collection,
                query=list(vector),
                query_filter=qm.Filter(must=must),
                limit=top_k,
                with_payload=True,
                score_threshold=score_threshold,
            )
        except Exception as exc:  # noqa: BLE001
            raise VectorStoreError(f"Qdrant search failed: {exc}") from exc
        hits: list[Hit] = []
        for p in res.points:
            payload = p.payload or {}
            hits.append(
                Hit(
                    document_id=str(payload.get("document_id", "")),
                    page=payload.get("page"),
                    chunk_index=payload.get("chunk_index"),
                    score=float(p.score),
                    text=str(payload.get("text", "")),
                )
            )
        return hits
