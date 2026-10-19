"""Detect the real file type from its bytes (never trust the extension / client hint)."""
from __future__ import annotations

import io
import zipfile
from enum import Enum

from app.core.errors import UnsupportedFileError


class FileKind(str, Enum):
    PDF = "pdf"
    JPEG = "jpeg"
    PNG = "png"
    TIFF = "tiff"
    DOCX = "docx"
    TXT = "txt"


_HINT_TO_KIND = {
    "pdf": FileKind.PDF, "jpg": FileKind.JPEG, "jpeg": FileKind.JPEG, "png": FileKind.PNG,
    "tif": FileKind.TIFF, "tiff": FileKind.TIFF, "docx": FileKind.DOCX, "txt": FileKind.TXT,
}


def kind_from_hint(hint: str | None) -> FileKind | None:
    return _HINT_TO_KIND.get((hint or "").lower().lstrip("."))


def sniff_file_kind(data: bytes, hint: str | None = None) -> FileKind:
    if not data:
        raise UnsupportedFileError("File is empty")
    head = data[:1024]
    if b"%PDF-" in head:
        return FileKind.PDF
    if data.startswith(b"\xff\xd8\xff"):
        return FileKind.JPEG
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return FileKind.PNG
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return FileKind.TIFF
    if data.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                if "word/document.xml" in zf.namelist():
                    return FileKind.DOCX
        except zipfile.BadZipFile:
            pass
        raise UnsupportedFileError("ZIP container is not a .docx document")
    if _looks_like_text(data) and kind_from_hint(hint) in (FileKind.TXT, None):
        return FileKind.TXT
    raise UnsupportedFileError("Unsupported or unrecognised file type (supported: PDF, JPG, PNG, TIFF, DOCX, TXT)")


def _looks_like_text(data: bytes) -> bool:
    sample = data[:4096]
    if b"\x00" in sample:
        return False
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        # tolerate a multi-byte char cut at the boundary
        try:
            sample[:-3].decode("utf-8")
        except UnicodeDecodeError:
            return False
    printable = sum(32 <= b < 127 or b in (9, 10, 13) or b >= 128 for b in sample)
    return printable / max(len(sample), 1) > 0.95
