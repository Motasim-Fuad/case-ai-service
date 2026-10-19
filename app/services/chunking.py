"""Page-aware text chunking. Chunks NEVER cross a page boundary so every chunk keeps an exact page number."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Protocol

_SEPARATORS = [r"\n{2,}", r"\n", r"(?<=[.!?])\s+", r"\s+"]


class _PageLike(Protocol):
    page_number: int
    text: str


@dataclass(frozen=True)
class Chunk:
    chunk_index: int
    page: int
    text: str


def _split_recursive(text: str, size: int, level: int = 0) -> list[str]:
    if len(text) <= size:
        return [text]
    if level >= len(_SEPARATORS):
        return [text[i : i + size] for i in range(0, len(text), size)]
    parts = [p for p in re.split(_SEPARATORS[level], text) if p and p.strip()]
    out: list[str] = []
    for part in parts:
        out.extend(_split_recursive(part, size, level + 1) if len(part) > size else [part])
    return out


def _overlap_tail(chunk: str, overlap: int) -> str:
    if overlap <= 0:
        return ""
    tail = chunk[-overlap:]
    match = re.search(r"\s", tail)
    if match and len(chunk) > overlap:
        tail = tail[match.end():]  # do not start in the middle of a word
    return tail.strip()


def split_text(text: str, size: int, overlap: int) -> list[str]:
    text = text.strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]
    chunks: list[str] = []
    cur = ""
    for unit in _split_recursive(text, size):
        if cur and len(cur) + 1 + len(unit) > size:
            chunks.append(cur)
            tail = _overlap_tail(cur, overlap)
            cur = f"{tail} {unit}" if tail and len(tail) + 1 + len(unit) <= size else unit
        else:
            cur = f"{cur}\n{unit}" if cur else unit
    if cur:
        chunks.append(cur)
    return chunks


def chunk_pages(pages: Iterable[_PageLike], size: int, overlap: int, min_chars: int = 15) -> list[Chunk]:
    if overlap >= size:
        raise ValueError("chunk overlap must be smaller than chunk size")
    chunks: list[Chunk] = []
    for page in sorted(pages, key=lambda p: p.page_number):
        for piece in split_text(page.text or "", size, overlap):
            if sum(ch.isalnum() for ch in piece) >= min_chars // 2 and len(piece.strip()) >= 3:
                chunks.append(Chunk(chunk_index=len(chunks), page=page.page_number, text=piece))
    return chunks
