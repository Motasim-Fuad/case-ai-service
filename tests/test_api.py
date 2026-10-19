import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_enqueue, get_job_store, get_rag_service, get_settings, get_storage, get_vector_store
from app.main import app
from app.services.pipeline import run_pipeline
from tests.helpers import make_text_pdf

KEY = {"X-Internal-Api-Key": "dev-internal-key"}


@pytest.fixture
def client(harness):
    settings = harness.settings.model_copy(update={"environment": "development", "enable_dev_endpoints": True})
    queued: list[str] = []

    def enqueue(job_id: str) -> None:  # run synchronously instead of via Celery
        queued.append(job_id)
        run_pipeline(job_id, harness.deps)

    app.dependency_overrides.update({
        get_settings: lambda: settings, get_job_store: lambda: harness.jobs, get_enqueue: lambda: enqueue,
        get_rag_service: lambda: harness.rag, get_vector_store: lambda: harness.deps.vectors,
        get_storage: lambda: harness.deps.storage,
    })
    c = TestClient(app)
    c.queued = queued  # type: ignore[attr-defined]
    yield c
    app.dependency_overrides.clear()


def _body(**over):
    body = {"document_id": "doc-9", "case_id": "case-9", "s3_key": "t/doc-9/letter.pdf", "file_type": "pdf",
            "callback_url": "http://nest.test/hook", "metadata": {"request_id": "abc"}}
    return body | over


def test_requires_api_key(client):
    assert client.post("/api/v1/documents/process", json=_body()).status_code == 401
    assert client.post("/api/v1/documents/process", json=_body(), headers={"X-Internal-Api-Key": "wrong"}).status_code == 401
    assert client.get("/health").status_code in (200, 503)  # health is open


def test_process_then_poll_full_contract(client, harness):
    harness.deps.storage.save("t/doc-9/letter.pdf", make_text_pdf())
    r = client.post("/api/v1/documents/process", json=_body(), headers=KEY)
    assert r.status_code == 202
    accepted = r.json()
    assert accepted["status"] == "queued" and accepted["document_id"] == "doc-9" and accepted["deduplicated"] is False

    j = client.get(f"/api/v1/documents/jobs/{accepted['job_id']}", headers=KEY).json()
    assert j["status"] == "completed" and j["progress"] == 100 and j["callback_status"] == "delivered"
    assert j["result"]["claim_details"]["claim_number"] == "CLM-2026-88431"
    assert j["result"]["events"][0]["evidence"]["page"] in (1, 2)
    payload = harness.webhook.sent[0][1]
    assert payload.metadata == {"request_id": "abc"}  # opaque metadata echoed back


def test_unknown_job_404(client):
    r = client.get("/api/v1/documents/jobs/nope", headers=KEY)
    assert r.status_code == 404 and r.json()["error"]["code"] == "JOB_NOT_FOUND"


def test_validation_errors(client):
    r = client.post("/api/v1/documents/process", json={"case_id": "c"}, headers=KEY)
    assert r.status_code == 422


def test_callback_host_allowlist(client, harness):
    settings = harness.settings.model_copy(update={"callback_allowed_hosts": "nest.internal"})
    app.dependency_overrides[get_settings] = lambda: settings
    r = client.post("/api/v1/documents/process", json=_body(), headers=KEY)
    assert r.status_code == 400 and r.json()["error"]["code"] == "CALLBACK_HOST_NOT_ALLOWED"


def test_active_job_is_deduplicated(client, harness):
    harness.deps.storage.save("t/doc-9/letter.pdf", make_text_pdf())
    # simulate a still-running job holding the lock
    harness.jobs.create("running-job", __import__("app.models.api", fromlist=["ProcessRequest"]).ProcessRequest(
        document_id="doc-9", case_id="case-9", s3_key="k"))
    assert harness.jobs.acquire_document("doc-9", "running-job") is None
    r = client.post("/api/v1/documents/process", json=_body(), headers=KEY).json()
    assert r["deduplicated"] is True and r["job_id"] == "running-job" and client.queued == []


def test_query_search_and_delete(client, harness):
    harness.deps.storage.save("t/doc-9/letter.pdf", make_text_pdf())
    client.post("/api/v1/documents/process", json=_body(), headers=KEY)
    q = client.post("/api/v1/case/case-9/query", json={"question": "When are photographs due?"}, headers=KEY).json()
    assert q["answerable"] and q["sources"][0]["document_id"] == "doc-9" and "chunk_text_snippet" in q["sources"][0]
    s = client.post("/api/v1/case/case-9/search", json={"query": "deductible", "top_k": 3}, headers=KEY).json()
    assert s["results"] and s["results"][0]["document_id"] == "doc-9"
    assert client.post("/api/v1/case/other-case/query", json={"question": "x"}, headers=KEY).json()["answerable"] is False
    assert client.delete("/api/v1/documents/doc-9/index", headers=KEY).json()["deleted"] is True
    assert client.post("/api/v1/case/case-9/search", json={"query": "deductible"}, headers=KEY).json()["results"] == []


def test_dev_upload_and_webhook_echo(client, harness):
    r = client.post("/api/v1/dev/process-upload", headers=KEY, data={"case_id": "case-up", "send_webhook": "false"},
                    files={"file": ("my letter.pdf", make_text_pdf(), "application/pdf")})
    assert r.status_code == 202
    job = client.get(f"/api/v1/documents/jobs/{r.json()['job_id']}", headers=KEY).json()
    assert job["status"] == "completed" and job["result"]["case_id"] == "case-up"
    pages = client.get(f"/api/v1/dev/jobs/{job['job_id']}/pages", headers=KEY).json()
    assert pages["pages"][0]["method"] == "text"
    client.post("/api/v1/dev/webhook-echo", json={"hello": "world"}, headers=KEY)
    assert client.get("/api/v1/dev/webhook-echo", headers=KEY).json()[0]["body"] == {"hello": "world"}


def test_queue_down_returns_503_and_releases_lock(client, harness):
    def broken(_job_id: str) -> None:
        raise ConnectionError("redis down")

    app.dependency_overrides[get_enqueue] = lambda: broken
    r = client.post("/api/v1/documents/process", json=_body(), headers=KEY)
    assert r.status_code == 503 and r.json()["error"]["code"] == "QUEUE_UNAVAILABLE"
    assert harness.jobs.acquire_document("doc-9", "next-job") is None  # lock was released
