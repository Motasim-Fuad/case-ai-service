import pymupdf
import pytest

from app.core.errors import EncryptedDocumentError, TooManyPagesError
from app.services.file_sniffer import FileKind
from app.services.page_extractor import PageExtractor, classify_text_layer
from app.services.storage import StorageRef
from tests.conftest import FakeOcr
from tests.helpers import LETTER, make_docx, make_mixed_pdf, make_png, make_scanned_pdf, make_text_pdf


def _extractor(settings, ocr="default"):
    return PageExtractor(settings, FakeOcr() if ocr == "default" else ocr)


def test_classifier():
    assert classify_text_layer("", 0, 40) == "scanned"
    assert classify_text_layer("short", 0, 40) == "scanned"
    assert classify_text_layer("\ufffd" * 200, 0, 40) == "scanned"
    assert classify_text_layer("A normal readable sentence about an insurance claim. " * 3, 0.0, 40) == "text"
    assert classify_text_layer("Bates 000123 confidential stamp text over a big scanned image " * 2, 0.9, 40) == "scanned"


def test_text_pdf_uses_direct_extraction_no_ocr(settings):
    ocr = FakeOcr()
    out = PageExtractor(settings, ocr).extract(make_text_pdf(), FileKind.PDF)
    assert [p.method for p in out.pages] == ["text", "text"]
    assert "CLM-2026-88431" in out.pages[0].text
    assert ocr.image_calls == 0 and not out.warnings


def test_scanned_pdf_goes_to_ocr(settings):
    ocr = FakeOcr()
    out = PageExtractor(settings, ocr).extract(make_scanned_pdf(), FileKind.PDF)
    assert out.pages[0].method == "ocr" and out.pages[0].ocr_confidence == 0.96
    assert "Claim Number" in out.pages[0].text and ocr.image_calls == 1


def test_mixed_pdf_is_decided_page_by_page(settings):
    ocr = FakeOcr(text="OCR TEXT of the scanned second page with enough characters")
    out = PageExtractor(settings, ocr).extract(make_mixed_pdf(), FileKind.PDF)
    assert [p.method for p in out.pages] == ["text", "ocr"]
    assert ocr.image_calls == 1 and out.pages[1].text.startswith("OCR TEXT")


def test_large_scanned_pdf_on_s3_uses_async_textract(settings):
    settings = settings.model_copy(update={"textract_async_page_threshold": 0})
    ocr = FakeOcr()
    out = PageExtractor(settings, ocr).extract(
        make_scanned_pdf(), FileKind.PDF, ref=StorageRef("bucket", "k.pdf"), storage_supports_async=True)
    assert ocr.async_calls == 1 and ocr.image_calls == 0 and out.pages[0].method == "ocr"


def test_no_ocr_engine_gives_page_error_not_crash(settings):
    out = PageExtractor(settings, None).extract(make_mixed_pdf(), FileKind.PDF)
    assert out.pages[0].method == "text" and out.pages[1].error.startswith("OCR_UNAVAILABLE")
    assert any(w.startswith("PAGE_FAILED") for w in out.warnings)


def test_image_file(settings):
    out = _extractor(settings).extract(make_png(), FileKind.PNG)
    assert len(out.pages) == 1 and out.pages[0].method == "ocr"


def test_encrypted_pdf_rejected(settings):
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "secret " * 20)
    data = doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="u", owner_pw="o")
    with pytest.raises(EncryptedDocumentError):
        _extractor(settings).extract(data, FileKind.PDF)


def test_page_limit(settings):
    s = settings.model_copy(update={"max_pages": 1})
    with pytest.raises(TooManyPagesError):
        PageExtractor(s, None).extract(make_text_pdf(), FileKind.PDF)


def test_docx_and_txt_virtual_pages(settings):
    out = _extractor(settings).extract(make_docx(["First paragraph.", "Second paragraph."]), FileKind.DOCX)
    assert out.pages[0].method == "docx" and "Second paragraph." in out.pages[0].text
    out = _extractor(settings).extract(LETTER.encode(), FileKind.TXT)
    assert out.pages[0].method == "txt" and "CLM-2026" in out.pages[0].text
