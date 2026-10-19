"""Generate sample documents in ./samples (text PDF, scanned PDF, PNG scan, DOCX-free TXT)."""
from __future__ import annotations

from pathlib import Path

import pymupdf

OUT = Path(__file__).resolve().parent.parent / "samples"
OUT.mkdir(exist_ok=True)

LETTER_P1 = """SAFEROAD INSURANCE COMPANY
Claims Department - 500 Main Street, Springfield

Date: September 12, 2026
Claim Number: CLM-2026-88431
Policy Number: POL-AX-552019
Date of Loss: 2026-08-28

Insured: John A. Miller
Claimant: Sarah K. Thompson
Adjuster: Robert Chen

Dear Mr. Miller,

We are writing about the auto accident that occurred on August 28, 2026 at the intersection of
Oak Avenue and 5th Street. Our investigation is ongoing.

Please submit photographs of the damage to your vehicle no later than October 15, 2026.
The estimated repair cost is $4,250.00 and your deductible is $500.00."""

LETTER_P2 = """Page 2

Please provide a signed recorded-statement form by 2026-10-01 so we can proceed with the claim.
Medical bills received to date total $1,820.75 from Springfield General Hospital.
A coverage decision will be issued on November 5, 2026.

Sincerely,
Robert Chen, Claims Adjuster"""


def text_pdf(path: Path) -> None:
    doc = pymupdf.open()
    for body in (LETTER_P1, LETTER_P2):
        page = doc.new_page()
        page.insert_textbox(pymupdf.Rect(50, 60, 545, 780), body, fontsize=11, fontname="helv")
    doc.save(path)


def scanned_pdf_and_png(pdf_path: Path, png_path: Path) -> None:
    src = pymupdf.open()
    page = src.new_page()
    page.insert_textbox(pymupdf.Rect(50, 60, 545, 780), LETTER_P1, fontsize=11, fontname="helv")
    pix = page.get_pixmap(dpi=150, colorspace=pymupdf.csGRAY)
    png_path.write_bytes(pix.tobytes("png"))
    scan = pymupdf.open()  # image-only PDF = no text layer, exactly like a scanned page
    sp = scan.new_page(width=page.rect.width, height=page.rect.height)
    sp.insert_image(sp.rect, stream=pix.tobytes("png"))
    scan.save(pdf_path)


if __name__ == "__main__":
    text_pdf(OUT / "sample_insurance_letter.pdf")
    scanned_pdf_and_png(OUT / "sample_scanned_letter.pdf", OUT / "sample_scanned_letter.png")
    (OUT / "sample_notes.txt").write_text(LETTER_P1 + "\n\n" + LETTER_P2, encoding="utf-8")
    print("samples written to", OUT)
