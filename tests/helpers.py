from __future__ import annotations

import pymupdf

LETTER = (
    "SAFEROAD INSURANCE COMPANY\n"
    "Claim Number: CLM-2026-88431\nPolicy Number: POL-AX-552019\nDate of Loss: 2026-08-28\n"
    "Insured: John A. Miller\nAdjuster: Robert Chen\n"
    "Please submit photographs of the damage to your vehicle no later than October 15, 2026.\n"
    "The estimated repair cost is $4,250.00 and your deductible is $500.00."
)
LETTER_P2 = "Page 2\nPlease provide a signed statement form by 2026-10-01.\nMedical bills total $1,820.75."


def _textbox(page: "pymupdf.Page", text: str) -> None:
    page.insert_textbox(pymupdf.Rect(50, 60, 545, 780), text, fontsize=11, fontname="helv")


def make_text_pdf(pages: list[str] | None = None) -> bytes:
    doc = pymupdf.open()
    for body in pages or [LETTER, LETTER_P2]:
        _textbox(doc.new_page(), body)
    return doc.tobytes()


def _image_page(doc: "pymupdf.Document", text: str) -> None:
    src = pymupdf.open()
    p = src.new_page()
    _textbox(p, text)
    png = p.get_pixmap(dpi=100, colorspace=pymupdf.csGRAY).tobytes("png")
    page = doc.new_page(width=p.rect.width, height=p.rect.height)
    page.insert_image(page.rect, stream=png)


def make_scanned_pdf(text: str = LETTER) -> bytes:
    doc = pymupdf.open()
    _image_page(doc, text)
    return doc.tobytes()


def make_mixed_pdf() -> bytes:
    doc = pymupdf.open()
    _textbox(doc.new_page(), LETTER)
    _image_page(doc, LETTER_P2)
    return doc.tobytes()


def make_png(text: str = LETTER) -> bytes:
    src = pymupdf.open()
    p = src.new_page()
    _textbox(p, text)
    return p.get_pixmap(dpi=100, colorspace=pymupdf.csGRAY).tobytes("png")


def make_docx(paragraphs: list[str]) -> bytes:
    import io

    import docx

    d = docx.Document()
    for para in paragraphs:
        d.add_paragraph(para)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()
