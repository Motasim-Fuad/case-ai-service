"""Turns any supported file into a list of normalised pages: {page, text, method}.

Text PDFs   -> PyMuPDF (fallback pdfplumber)
Scanned PDF -> Textract (sync per page, or async for large jobs)
Images      -> Textract sync
DOCX / TXT  -> direct read (page numbers are approximate "virtual pages")
Mixed PDFs  -> decided page by page.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from typing import Callable, Literal

import pymupdf

from app.core.concurrency import ContextThreadPoolExecutor
from app.core.config import Settings
from app.core.errors import (
    CorruptDocumentError, EncryptedDocumentError, OcrError, OcrPageError,
    OcrUnavailableError, TooManyPagesError,
)
from app.core.logging import get_logger
from app.services.file_sniffer import FileKind
from app.services.storage import StorageRef
from app.services.text_utils import alnum_ratio, clean_text
from app.services.textract import OcrEngine

log = get_logger(__name__)

_MAX_OCR_BYTES = 9_500_000  # Textract sync limit is 10 MB
_VIRTUAL_PAGE_CHARS = 3000
ProgressFn = Callable[[int], None]


@dataclass
class PageContent:
    page_number: int
    text: str
    method: Literal["text", "ocr", "docx", "txt"]
    ocr_confidence: float | None = None
    error: str | None = None

    @property
    def char_count(self) -> int:
        return len(self.text)


@dataclass
class ExtractionOutcome:
    pages: list[PageContent]
    warnings: list[str] = field(default_factory=list)


# ------------------------------------------------------------------ helpers
def classify_text_layer(text: str, image_coverage: float, min_chars: int) -> Literal["text", "scanned"]:
    """Decide whether a PDF page has a usable text layer or needs OCR."""
    compact = re.sub(r"\s+", " ", text or "").strip()
    if len(compact) < min_chars:
        return "scanned"
    if alnum_ratio(compact) < 0.35 or compact.count("\ufffd") / len(compact) > 0.1:
        return "scanned"  # garbage / broken font mapping
    if image_coverage >= 0.6 and len(compact) < 300:
        return "scanned"  # page is mostly a picture with a stamp/footer text
    return "text"


def image_coverage(page: "pymupdf.Page") -> float:
    try:
        infos = page.get_image_info()
    except Exception:  # noqa: BLE001
        return 0.0
    area = abs(page.rect.width * page.rect.height) or 1.0
    covered = 0.0
    for info in infos:
        inter = pymupdf.Rect(info["bbox"]) & page.rect
        if not inter.is_empty:
            covered += inter.width * inter.height
    return min(covered / area, 1.0)


def render_page_png(page: "pymupdf.Page", dpi: int) -> bytes:
    current = dpi
    while True:
        pix = page.get_pixmap(dpi=current, colorspace=pymupdf.csGRAY, alpha=False)
        data = pix.tobytes("png")
        if len(data) <= _MAX_OCR_BYTES or current <= 72:
            return data
        current = max(72, int(current * 0.7))


def _shrink_image(data: bytes) -> bytes:
    pix = pymupdf.Pixmap(data)
    if pix.alpha:
        pix = pymupdf.Pixmap(pix, 0)
    if pix.colorspace is None or pix.colorspace.n not in (1, 3):
        pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
    while True:
        out = pix.tobytes("jpeg", jpg_quality=80)
        if len(out) <= _MAX_OCR_BYTES or min(pix.width, pix.height) < 400:
            return out
        pix.shrink(1)


def _paginate(paragraphs: list[str], target: int = _VIRTUAL_PAGE_CHARS) -> list[str]:
    pages: list[str] = []
    cur: list[str] = []
    size = 0
    for para in paragraphs:
        if cur and size + len(para) > target:
            pages.append("\n\n".join(cur))
            cur, size = [], 0
        cur.append(para)
        size += len(para) + 2
    if cur:
        pages.append("\n\n".join(cur))
    return pages


class PageExtractor:
    def __init__(self, settings: Settings, ocr: OcrEngine | None) -> None:
        self.settings = settings
        self.ocr = ocr

    # --------------------------------------------------------------- public
    def extract(
        self,
        data: bytes,
        kind: FileKind,
        *,
        ref: StorageRef | None = None,
        storage_supports_async: bool = False,
        progress: ProgressFn | None = None,
    ) -> ExtractionOutcome:
        progress = progress or (lambda _pct: None)
        if kind is FileKind.PDF:
            pages = self._extract_pdf(data, ref, storage_supports_async, progress)
        elif kind in (FileKind.JPEG, FileKind.PNG):
            pages = self._extract_image(data)
        elif kind is FileKind.TIFF:
            pages = self._extract_tiff(data, progress)
        elif kind is FileKind.DOCX:
            pages = self._extract_docx(data)
        else:
            pages = self._extract_txt(data)
        pages.sort(key=lambda p: p.page_number)
        warnings = [f"PAGE_FAILED: page {p.page_number}: {p.error}" for p in pages if p.error]
        return ExtractionOutcome(pages=pages, warnings=warnings)

    # ------------------------------------------------------------------ PDF
    def _open_pdf(self, data: bytes, filetype: str = "pdf") -> "pymupdf.Document":
        try:
            doc = pymupdf.open(stream=data, filetype=filetype)
        except Exception as exc:  # noqa: BLE001
            raise CorruptDocumentError(f"Cannot open document: {exc}") from exc
        if doc.needs_pass and not doc.authenticate(""):
            doc.close()
            raise EncryptedDocumentError("Document is password protected")
        if doc.page_count == 0:
            doc.close()
            raise CorruptDocumentError("Document has no pages")
        if doc.page_count > self.settings.max_pages:
            count = doc.page_count
            doc.close()
            raise TooManyPagesError(f"Document has {count} pages (limit {self.settings.max_pages})")
        return doc

    def _extract_pdf(
        self, data: bytes, ref: StorageRef | None, storage_supports_async: bool, progress: ProgressFn
    ) -> list[PageContent]:
        doc = self._open_pdf(data)
        try:
            total = doc.page_count
            results: dict[int, PageContent] = {}
            scanned: list[int] = []
            plumber = None
            min_chars = self.settings.scanned_page_min_chars

            for idx in range(total):
                number = idx + 1
                page = doc[idx]
                try:
                    text = clean_text(page.get_text("text", sort=True))
                except Exception as exc:  # noqa: BLE001
                    log.warning("pymupdf_text_failed", page=number, error=str(exc)[:200])
                    text = ""
                coverage = image_coverage(page)
                if classify_text_layer(text, coverage, min_chars) == "text":
                    results[number] = PageContent(number, text, "text")
                else:
                    alt = ""
                    if coverage < 0.6:  # likely real (vector) text that PyMuPDF missed -> try pdfplumber
                        plumber, alt = self._plumber_text(plumber, data, idx)
                    if alt and classify_text_layer(alt, coverage, min_chars) == "text":
                        results[number] = PageContent(number, alt, "text")
                    else:
                        scanned.append(number)
                progress(int(30 * number / total))

            log.info("pdf_classified", pages=total, text_pages=len(results), scanned_pages=len(scanned))
            if plumber is not None:
                try:
                    plumber.close()
                except Exception:  # noqa: BLE001
                    pass
            if scanned:
                results.update(self._ocr_pages(doc, scanned, ref, storage_supports_async, progress))
            return list(results.values())
        finally:
            doc.close()

    @staticmethod
    def _plumber_text(plumber, data: bytes, idx: int):
        try:
            if plumber is None:
                import pdfplumber

                plumber = pdfplumber.open(io.BytesIO(data))
            return plumber, clean_text(plumber.pages[idx].extract_text() or "")
        except Exception as exc:  # noqa: BLE001
            log.warning("pdfplumber_failed", page=idx + 1, error=str(exc)[:200])
            return plumber, ""

    # ------------------------------------------------------------------ OCR
    def _ocr_pages(
        self,
        doc: "pymupdf.Document",
        page_numbers: list[int],
        ref: StorageRef | None,
        allow_async: bool,
        progress: ProgressFn,
    ) -> dict[int, PageContent]:
        if self.ocr is None:
            return {
                p: PageContent(p, "", "ocr", error="OCR_UNAVAILABLE: OCR engine is not configured")
                for p in page_numbers
            }
        s = self.settings
        use_async = (
            allow_async and ref is not None and s.textract_mode != "sync"
            and (s.textract_mode == "async" or len(page_numbers) > s.textract_async_page_threshold)
        )
        if use_async:
            assert ref is not None
            bucket = ref.bucket or s.s3_bucket_name
            try:
                ocr_map = self.ocr.ocr_s3_pdf(bucket, ref.key)
                progress(100)
                out: dict[int, PageContent] = {}
                for p in page_numbers:
                    res = ocr_map.get(p)
                    out[p] = (
                        PageContent(p, clean_text(res.text), "ocr", res.confidence)
                        if res else PageContent(p, "", "ocr", error="OCR_EMPTY: Textract returned no text for this page")
                    )
                return out
            except OcrUnavailableError as exc:
                return {p: PageContent(p, "", "ocr", error=f"{exc.code}: {exc.message}") for p in page_numbers}
            except OcrPageError as exc:
                log.warning("textract_async_rejected_falling_back", error=exc.message)

        return self._ocr_sync(doc, page_numbers, progress)

    def _ocr_sync(self, doc: "pymupdf.Document", page_numbers: list[int], progress: ProgressFn) -> dict[int, PageContent]:
        assert self.ocr is not None
        s = self.settings
        results: dict[int, PageContent] = {}
        transient: OcrError | None = None
        batch = max(1, s.textract_concurrency * 2)
        with ContextThreadPoolExecutor(max_workers=s.textract_concurrency) as pool:
            for i in range(0, len(page_numbers), batch):
                group = page_numbers[i : i + batch]
                futures = {}
                for p in group:  # render on this thread: PyMuPDF documents are not thread-safe
                    try:
                        png = render_page_png(doc[p - 1], s.ocr_render_dpi)
                    except Exception as exc:  # noqa: BLE001
                        results[p] = PageContent(p, "", "ocr", error=f"RENDER_FAILED: {exc}"[:300])
                        continue
                    futures[p] = pool.submit(self.ocr.ocr_image, png)
                for p, fut in futures.items():
                    try:
                        res = fut.result()
                        results[p] = PageContent(p, clean_text(res.text), "ocr", res.confidence)
                    except (OcrPageError, OcrUnavailableError) as exc:
                        results[p] = PageContent(p, "", "ocr", error=f"{exc.code}: {exc.message}")
                    except OcrError as exc:
                        transient = transient or exc
                progress(30 + int(70 * min(i + batch, len(page_numbers)) / len(page_numbers)))
        if transient is not None:
            raise transient  # let Celery retry the job (throttling / network)
        return results

    # --------------------------------------------------------------- images
    def _extract_image(self, data: bytes) -> list[PageContent]:
        if self.ocr is None:
            return [PageContent(1, "", "ocr", error="OCR_UNAVAILABLE: OCR engine is not configured")]
        try:
            payload = data if len(data) <= _MAX_OCR_BYTES else _shrink_image(data)
        except Exception as exc:  # noqa: BLE001
            raise CorruptDocumentError(f"Cannot read image: {exc}") from exc
        try:
            res = self.ocr.ocr_image(payload)
        except (OcrPageError, OcrUnavailableError) as exc:
            return [PageContent(1, "", "ocr", error=f"{exc.code}: {exc.message}")]
        return [PageContent(1, clean_text(res.text), "ocr", res.confidence)]

    def _extract_tiff(self, data: bytes, progress: ProgressFn) -> list[PageContent]:
        doc = self._open_pdf(data, filetype="tiff")
        try:
            pages = list(range(1, doc.page_count + 1))
            return list(self._ocr_pages(doc, pages, None, False, progress).values())
        finally:
            doc.close()

    # ------------------------------------------------------------ DOCX / TXT
    def _extract_docx(self, data: bytes) -> list[PageContent]:
        import docx
        from docx.table import Table
        from docx.text.paragraph import Paragraph

        try:
            document = docx.Document(io.BytesIO(data))
        except Exception as exc:  # noqa: BLE001
            raise CorruptDocumentError(f"Cannot open .docx: {exc}") from exc
        paragraphs: list[str] = []
        for child in document.element.body.iterchildren():
            tag = child.tag.rsplit("}", 1)[-1]
            if tag == "p":
                text = Paragraph(child, document).text
            elif tag == "tbl":
                table = Table(child, document)
                text = "\n".join(" | ".join(c.text.strip() for c in row.cells) for row in table.rows)
            else:
                continue
            if text.strip():
                paragraphs.append(text.strip())
        return [PageContent(i + 1, clean_text(t), "docx") for i, t in enumerate(_paginate(paragraphs))]

    def _extract_txt(self, data: bytes) -> list[PageContent]:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("latin-1")
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text.replace("\r\n", "\n")) if p.strip()]
        return [PageContent(i + 1, clean_text(t), "txt") for i, t in enumerate(_paginate(paragraphs))]
