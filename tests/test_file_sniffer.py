import pytest

from app.core.errors import UnsupportedFileError
from app.services.file_sniffer import FileKind, sniff_file_kind
from tests.helpers import make_docx, make_png, make_text_pdf


def test_detects_by_content_not_extension():
    assert sniff_file_kind(make_text_pdf(), hint="png") is FileKind.PDF
    assert sniff_file_kind(make_png(), hint="pdf") is FileKind.PNG


def test_jpeg_tiff_docx_txt():
    assert sniff_file_kind(b"\xff\xd8\xff\xe0" + b"0" * 20) is FileKind.JPEG
    assert sniff_file_kind(b"II*\x00" + b"0" * 20) is FileKind.TIFF
    assert sniff_file_kind(make_docx(["hello"])) is FileKind.DOCX
    assert sniff_file_kind("plain text file\nline 2".encode(), hint="txt") is FileKind.TXT


def test_rejects_unknown_and_empty():
    with pytest.raises(UnsupportedFileError):
        sniff_file_kind(b"")
    with pytest.raises(UnsupportedFileError):
        sniff_file_kind(b"\x00\x01\x02\x03" * 100)
    with pytest.raises(UnsupportedFileError):
        sniff_file_kind(b"PK\x03\x04not really a zip")
