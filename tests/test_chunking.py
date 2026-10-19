from dataclasses import dataclass

import pytest

from app.services.chunking import chunk_pages, split_text


@dataclass
class P:
    page_number: int
    text: str


def test_short_page_single_chunk():
    chunks = chunk_pages([P(1, "Hello claim number 12345 here.")], 500, 50)
    assert len(chunks) == 1 and chunks[0].page == 1 and chunks[0].chunk_index == 0


def test_chunks_never_cross_pages_and_respect_size():
    para = "This is a sentence about the accident. " * 30
    pages = [P(1, para), P(2, para + "\n\n" + para)]
    chunks = chunk_pages(pages, 400, 60)
    assert all(len(c.text) <= 400 for c in chunks)
    assert {c.page for c in chunks} == {1, 2}
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))
    page1 = "".join(c.text for c in chunks if c.page == 1)
    assert "accident" in page1


def test_overlap_repeats_context():
    text = " ".join(f"word{i}" for i in range(400))
    chunks = split_text(text, 300, 60)
    assert len(chunks) > 2
    assert chunks[0].split()[-1] in chunks[1]  # tail of chunk N reappears at start of N+1


def test_giant_token_is_hard_split():
    chunks = split_text("x" * 1000, 300, 30)
    assert all(len(c) <= 300 for c in chunks) and len(chunks) >= 4


def test_empty_and_invalid():
    assert chunk_pages([P(1, "   ")], 300, 30) == []
    with pytest.raises(ValueError):
        chunk_pages([P(1, "x")], 100, 100)
