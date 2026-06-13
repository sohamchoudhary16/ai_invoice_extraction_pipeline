"""
src/ocr/confidence.py
Per-page and per-field OCR quality scoring.

Splits words into:
  - high_confidence : conf >= threshold
  - low_confidence  : conf < threshold

Also detects common OCR corruption patterns:
  - digit/letter confusion:  O→0, I→1, l→1
  - broken VAT/IBAN patterns
"""

import re
from src.ocr.tesseract_runner import OcrPageResult, OcrWord
from src.observability.logger import get_logger

log = get_logger(__name__)

# Patterns that suggest OCR corruption
_CORRUPTION_PATTERNS = [
    re.compile(r"\b[O][0-9]{7,}\b"),          # O instead of 0 in numbers
    re.compile(r"\b[Il][0-9]{3,}\b"),          # I or l instead of 1
    re.compile(r"[A-Z]{2}[0-9]{2}[0-9A-Z ]*[IiOo][0-9A-Z ]{3,}"),  # IBAN with I/O corruption only
]


def split_by_confidence(
    result: OcrPageResult,
    threshold: int,
) -> dict:
    """
    Split words into high/low confidence buckets.

    Returns
    -------
    {
        "high_conf_words": [OcrWord, ...],
        "low_conf_words":  [OcrWord, ...],
        "high_conf_text":  str,
        "low_conf_text":   str,
        "avg_confidence":  float,
        "corruption_hints": [str],
    }
    """
    high, low = [], []
    for w in result.words:
        if w.confidence >= threshold:
            high.append(w)
        else:
            low.append(w)

    # Detect corruption in full text
    corruption_hints = []
    for pattern in _CORRUPTION_PATTERNS:
        matches = pattern.findall(result.full_text)
        if matches:
            corruption_hints.append(f"pattern={pattern.pattern!r} matches={matches[:3]}")

    if corruption_hints:
        log.warning("ocr_corruption_hints",
                    page=result.page_number, hints=corruption_hints)

    return {
        "high_conf_words": high,
        "low_conf_words":  low,
        "high_conf_text":  " ".join(w.text for w in high),
        "low_conf_text":   " ".join(w.text for w in low),
        "avg_confidence":  result.avg_confidence,
        "corruption_hints": corruption_hints,
    }
