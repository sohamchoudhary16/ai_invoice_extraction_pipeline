"""
src/ocr/bbox_mapper.py
Maps OCR words to logical invoice zones using bounding-box heuristics.

Zones (relative to page height):
  header   : top 25%  — invoice number, date, reference
  seller   : top 25-45% left half
  buyer    : top 25-45% right half
  items    : middle 45-75% — line item table
  totals   : bottom 60-85%
  payment  : bottom 75-100%

This is a heuristic, not pixel-perfect.  Good enough to give the LLM
spatial hints that improve field attribution accuracy.
"""

from src.ocr.tesseract_runner import OcrWord
from src.observability.logger import get_logger

log = get_logger(__name__)

ZONES = ["header", "seller", "buyer", "items", "totals", "payment", "unknown"]


def assign_zone(word: OcrWord, page_width: int, page_height: int) -> str:
    """Return the logical zone name for a bounding-box word."""
    if page_height == 0:
        return "unknown"

    rel_top  = word.top  / page_height
    rel_left = word.left / page_width if page_width else 0.5

    if rel_top < 0.25:
        return "header"
    if 0.25 <= rel_top < 0.45:
        return "seller" if rel_left < 0.5 else "buyer"
    if 0.45 <= rel_top < 0.72:
        return "items"
    if 0.72 <= rel_top < 0.85:
        return "totals"
    return "payment"


def group_words_by_zone(
    words: list[OcrWord],
    page_width: int,
    page_height: int,
) -> dict[str, list[OcrWord]]:
    """Return {zone_name: [OcrWord, ...]} for all words on a page."""
    groups: dict[str, list[OcrWord]] = {z: [] for z in ZONES}
    for w in words:
        zone = assign_zone(w, page_width, page_height)
        groups[zone].append(w)
    return groups


def zone_text(groups: dict[str, list[OcrWord]]) -> dict[str, str]:
    """Convert grouped words to {zone: 'joined text'} for prompt injection."""
    return {
        zone: " ".join(w.text for w in words)
        for zone, words in groups.items()
        if words
    }
