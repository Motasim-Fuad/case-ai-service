from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import get_rag_service, get_vector_store
from app.models.api import DeleteIndexResponse, QueryRequest, QueryResponse, SearchRequest, SearchResponse
from app.services.rag import RagService
from app.services.vector_store import QdrantVectorStore

router = APIRouter(prefix="/case", tags=["rag"])


@router.post("/{case_id}/query", response_model=QueryResponse, summary="Ask a question about one case (RAG)")
def query_case(case_id: str, body: QueryRequest, rag: RagService = Depends(get_rag_service)) -> QueryResponse:
    """Embeds the question, searches ONLY this case's chunks, and returns a grounded answer + sources."""
    return rag.answer(case_id, body.question, body.top_k, body.document_ids, body.history)


@router.post("/{case_id}/search", response_model=SearchResponse, summary="Semantic search inside one case")
def search_case(case_id: str, body: SearchRequest, rag: RagService = Depends(get_rag_service)) -> SearchResponse:
    return SearchResponse(results=rag.search_response(case_id, body.query, body.top_k, body.document_ids))


@router.delete("/{case_id}/index", response_model=DeleteIndexResponse, summary="Remove all vectors of a case")
def delete_case_index(case_id: str, vectors: QdrantVectorStore = Depends(get_vector_store)) -> DeleteIndexResponse:
    vectors.delete_case(case_id)
    return DeleteIndexResponse(scope="case", id=case_id)
