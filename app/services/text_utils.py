"""Small text helpers shared across the pipeline."""
from __future__ import annotations

import re
import unicodedata

_MULTI_NL = re.compile(r"\n{3,}")
_TRAILING_WS = re.compile(r"[ \t]+\n")
_NON_WORD = re.compile(r"[^\w]+", re.UNICODE)


def clean_text(text: str) -> str:
    """Normalise line endings / whitespace while keeping paragraph structure."""
    if not text:
        return ""
    text = text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u00ad", "")  # soft hyphen
    text = _TRAILING_WS.sub("\n", text)
    text = _MULTI_NL.sub("\n\n", text)
    return text.strip()


def normalize_for_match(text: str) -> str:
    """Aggressive normalisation used ONLY for verifying that a quote exists in a page."""
    text = unicodedata.normalize("NFKC", text or "").casefold()
    return _NON_WORD.sub(" ", text).strip()


def alnum_ratio(text: str) -> float:
    if not text:
        return 0.0
    return sum(ch.isalnum() for ch in text) / len(text)
