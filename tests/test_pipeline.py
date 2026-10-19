import pytest

from app.core.errors import NoReadableTextError, PermanentError, VectorStoreError
from app.services.pipeline import finalize_failure, run_pipeline
from tests.helpers import make_docx, make_scanned_pdf, make_text_pdf


def _result(h, job_id):
    return h.jobs.get_result(job_id)


def test_text_pdf_end_to_end(harness):
    job_id = harness.submit("letter.pdf", make_text_pdf())
    rec = harness.jobs.get(job_id)
    assert rec.status == "completed" and rec.stage == "done" and rec.progress == 100
    assert rec.callback_status == "delivered" and rec.chunks_indexed >= 2

    result = _result(harness, job_id)
    assert result.processing_status == "completed" and result.document_type == "insurance_letter"
    assert result.claim_details.claim_number == "CLM-2026-88431"
    assert result.claim_details.policy_number == "POL-AX-552019"
    assert any(e.event_date == "2026-10-15" and e.is_deadline and e.evidence.page == 1 for e in result.events)
    assert any(a.amount == 4250.0 and a.currency == "USD" for a in result.amounts)
    assert any(a.amount == 1820.75 for a in result.amounts)
    assert [p.method for p in result.pages] == ["text", "text"]

    (url, payload), = harness.webhook.sent
    assert url == "http://nest.test/hook" and payload.status == "completed"
    assert payload.extracted_data.document_id == "doc-1" and payload.chunks_indexed == rec.chunks_indexed


def test_scanned_pdf_uses_ocr_and_is_extracted(harness):
    job_id = harness.submit("scan.pdf", make_scanned_pdf())
    result = _result(harness, job_id)
    assert result.pages[0].method == "ocr" and result.pages[0].ocr_confidence == 0.96
    assert result.claim_details.claim_number == "CLM-2026-88431"
    assert harness.ocr.image_calls == 1


def test_pages_cache_skips_second_ocr_and_reprocessing_does_not_duplicate_vectors(harness):
    data = make_scanned_pdf()
    harness.submit("scan.pdf", data, document_id="doc-1")
    n1 = harness.deps.vectors._client.count(harness.deps.vectors._collection).count
    harness.submit("scan.pdf", None, document_id="doc-1")  # same file, new job
    n2 = harness.deps.vectors._client.count(harness.deps.vectors._collection).count
    assert harness.ocr.image_calls == 1  # OCR paid once (cache by sha256)
    assert n1 == n2 > 0
    harness.submit("scan.pdf", None, document_id="doc-1", force_reprocess=True)
    assert harness.ocr.image_calls == 2


def test_rag_search_is_scoped_to_the_case(harness):
    harness.submit("a.pdf", make_text_pdf(), case_id="case-A", document_id="docA")
    harness.submit("b.pdf", make_text_pdf(["Totally different topic about gardening tools and soil."]),
                   case_id="case-B", document_id="docB")
    hits_a = harness.rag.search("case-A", "photographs damage deadline", top_k=10)
    assert hits_a and all(h.document_id == "docA" for h in hits_a)
    hits_b = harness.rag.search("case-B", "gardening", top_k=10)
    assert hits_b and all(h.document_id == "docB" for h in hits_b)
    answer = harness.rag.answer("case-A", "When must the photographs be submitted?")
    assert answer.answerable and answer.sources and answer.sources[0].document_id == "docA"
    assert answer.sources[0].page == 1
    empty = harness.rag.answer("case-Z", "anything?")
    assert not empty.answerable and empty.sources == []


def test_delete_document_index(harness):
    harness.submit("a.pdf", make_text_pdf(), document_id="docA")
    harness.deps.vectors.delete_document("docA")
    assert harness.rag.search("case-1", "photographs", top_k=5) == []


def test_docx_supported(harness):
    job_id = harness.submit("note.docx", make_docx(["Claim Number: CLM-77 filed on 2026-08-01.", "Please submit records by 2026-09-30."]))
    result = _result(harness, job_id)
    assert result.pages[0].method == "docx" and result.claim_details.claim_number == "CLM-77"


def test_type_mismatch_warning(harness):
    job_id = harness.submit("wrong_ext.png", make_text_pdf())  # PDF bytes named .png
    result = _result(harness, job_id)
    assert result.processing_status == "completed"
    assert any(w.startswith("FILE_TYPE_MISMATCH") for w in result.warnings)


def test_permanent_failure_reports_failed_webhook(harness):
    harness.ocr.text = ""  # a blank page: no text layer AND OCR finds nothing
    job_id = harness.submit("empty.pdf", make_text_pdf(["   "]), run=False)
    with pytest.raises(NoReadableTextError) as info:
        run_pipeline(job_id, harness.deps)
    finalize_failure(job_id, harness.deps, info.value)
    rec = harness.jobs.get(job_id)
    assert rec.status == "failed" and rec.error_code == "NO_READABLE_TEXT" and rec.stage == "failed"
    payload = harness.webhook.sent[-1][1]
    assert payload.status == "failed" and payload.extracted_data.processing_status == "failed"
    assert "NO_READABLE_TEXT" in payload.extracted_data.error_message


def test_missing_file_is_permanent(harness):
    job_id = harness.submit("nope.pdf", None, run=False)
    with pytest.raises(PermanentError) as info:
        run_pipeline(job_id, harness.deps)
    assert info.value.code == "FILE_NOT_FOUND"


def test_index_failure_retry_reuses_extraction_and_ocr(harness, monkeypatch):
    job_id = harness.submit("scan.pdf", make_scanned_pdf(), run=False)
    calls = {"n": 0}
    real = harness.deps.vectors.replace_document

    def flaky(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise VectorStoreError("qdrant down")
        return real(**kw)

    monkeypatch.setattr(harness.deps.vectors, "replace_document", flaky)
    with pytest.raises(VectorStoreError):
        run_pipeline(job_id, harness.deps, attempt=1)  # Celery would now retry
    assert harness.jobs.load_extraction(job_id) is not None  # extraction was saved
    llm_calls_before = harness.ocr.image_calls
    run_pipeline(job_id, harness.deps, attempt=2)
    assert harness.ocr.image_calls == llm_calls_before  # OCR NOT repeated
    assert harness.jobs.get(job_id).status == "completed"


def test_index_failure_after_max_retries_degrades_to_partial(harness, monkeypatch):
    job_id = harness.submit("letter.pdf", make_text_pdf(), run=False)
    monkeypatch.setattr(harness.deps.vectors, "replace_document",
                        lambda **kw: (_ for _ in ()).throw(VectorStoreError("qdrant down")))
    with pytest.raises(VectorStoreError) as info:
        run_pipeline(job_id, harness.deps)
    finalize_failure(job_id, harness.deps, info.value)  # what the task does on the last attempt
    result = harness.jobs.get_result(job_id)
    assert harness.jobs.get(job_id).status == "completed"
    assert result.processing_status == "partial" and result.events  # extracted data NOT thrown away
    assert any(w.startswith("INDEXING_FAILED") for w in result.warnings)


def test_polling_only_when_no_callback(harness):
    job_id = harness.submit("letter.pdf", make_text_pdf(), callback=False)
    assert harness.jobs.get(job_id).callback_status == "skipped" and harness.webhook.sent == []


def test_document_lock_deduplicates_active_jobs(harness):
    a = harness.submit("letter.pdf", make_text_pdf(), run=False)
    assert harness.jobs.acquire_document("doc-1", a) is None
    assert harness.jobs.acquire_document("doc-1", "another-job") == a
    harness.jobs.update(a, status="completed")
    assert harness.jobs.acquire_document("doc-1", "another-job") is None  # stale lock taken over


def test_poison_job_stops_after_repeated_worker_crashes(harness):
    job_id = harness.submit("letter.pdf", make_text_pdf(), run=False)
    for _ in range(harness.settings.celery_task_max_retries + 3):
        harness.jobs.bump_deliveries(job_id)  # simulates redeliveries after the worker was killed
    with pytest.raises(PermanentError) as info:
        run_pipeline(job_id, harness.deps)
    assert info.value.code == "POISON_JOB"
    finalize_failure(job_id, harness.deps, info.value)
    assert harness.jobs.get(job_id).status == "failed" and harness.webhook.sent[-1][1].status == "failed"
