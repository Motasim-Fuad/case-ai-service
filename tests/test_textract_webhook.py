import httpx
import pytest
from botocore.exceptions import ClientError, NoCredentialsError

from app.core.errors import OcrError, OcrUnavailableError
from app.models.api import WebhookPayload
from app.models.extraction import DocumentExtractionResult
from app.core.security import sign_body
from app.services.textract import TextractOcr, blocks_to_pages
from app.services.webhook import WebhookClient, WebhookDeliveryError


def line(text, page=1, conf=99.0):
    return {"BlockType": "LINE", "Text": text, "Page": page, "Confidence": conf}


def test_blocks_to_pages_groups_and_averages():
    pages = blocks_to_pages([line("a", 1, 90), line("b", 1, 100), line("c", 2, 80), {"BlockType": "WORD", "Text": "x"}])
    assert pages[1].text == "a\nb" and pages[1].confidence == pytest.approx(0.95)
    assert pages[2].text == "c"


class FakeTextract:
    def __init__(self, statuses, blocks_pages):
        self.statuses, self.pages = list(statuses), blocks_pages
        self.polls = 0

    def detect_document_text(self, **kw):
        return {"Blocks": [line("sync text")]}

    def start_document_text_detection(self, **kw):
        return {"JobId": "J1"}

    def get_document_text_detection(self, JobId, NextToken=None):
        if NextToken == "t2":
            return {"JobStatus": "SUCCEEDED", "Blocks": [line("page two", 2)]}
        self.polls += 1
        status = self.statuses.pop(0)
        if status == "SUCCEEDED":
            return {"JobStatus": status, "Blocks": [line("page one", 1)], "NextToken": "t2"}
        return {"JobStatus": status, "StatusMessage": "boom"}


def test_async_polling_pagination(settings):
    sleeps = []
    client = FakeTextract(["IN_PROGRESS", "IN_PROGRESS", "SUCCEEDED"], None)
    ocr = TextractOcr(settings, client=client, sleep=sleeps.append)
    pages = ocr.ocr_s3_pdf("b", "k.pdf")
    assert pages[1].text == "page one" and pages[2].text == "page two" and len(sleeps) == 2


def test_async_failed_job_raises(settings):
    ocr = TextractOcr(settings, client=FakeTextract(["FAILED"], None), sleep=lambda s: None)
    with pytest.raises(OcrError):
        ocr.ocr_s3_pdf("b", "k")


def test_sync_image(settings):
    assert TextractOcr(settings, client=FakeTextract([], None)).ocr_image(b"img").text == "sync text"


def test_missing_credentials_and_access_denied(settings):
    class NoCreds:
        def detect_document_text(self, **kw):
            raise NoCredentialsError()

    with pytest.raises(OcrUnavailableError):
        TextractOcr(settings, client=NoCreds()).ocr_image(b"x")

    class Denied:
        def detect_document_text(self, **kw):
            raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "DetectDocumentText")

    with pytest.raises(OcrUnavailableError):
        TextractOcr(settings, client=Denied()).ocr_image(b"x")


def _payload():
    return WebhookPayload(job_id="j1", document_id="d1", case_id="c1", status="completed",
                          extracted_data=DocumentExtractionResult(document_id="d1", case_id="c1"))


def test_webhook_headers_signature_and_retry(settings, monkeypatch):
    monkeypatch.setattr("tenacity.nap.time.sleep", lambda s: None)
    s = settings.model_copy(update={"webhook_signing_secret": "topsecret", "internal_api_key": "k123"})
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(500 if len(seen) < 3 else 200)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    WebhookClient(s, client).send("http://nest.test/hook", _payload())
    assert len(seen) == 3
    last = seen[-1]
    assert last.headers["X-Internal-Api-Key"] == "k123" and last.headers["Idempotency-Key"] == "j1"
    assert last.headers["X-Signature"] == sign_body("topsecret", last.content)


def test_webhook_4xx_is_not_retried_and_raises(settings, monkeypatch):
    monkeypatch.setattr("tenacity.nap.time.sleep", lambda s: None)
    calls = []
    client = httpx.Client(transport=httpx.MockTransport(lambda r: (calls.append(1), httpx.Response(401))[1]))
    with pytest.raises(WebhookDeliveryError):
        WebhookClient(settings, client).send("http://nest.test/hook", _payload())
    assert len(calls) == 1
