"""
src/classifiers/document_router.py
Detects whether a PDF is digital (has selectable text) or scanned (image-only).
Routes accordingly so we skip OCR on digital PDFs — faster and more accurate.
"""

import logging
import pymupdf

logger = logging.getLogger(__name__)


def classify_pdf(pdf_path: str) -> dict:
    """
    Inspect a PDF and determine:
      - is_digital : True if pages contain extractable text
      - page_types : per-page breakdown
      - total_pages: page count

    Returns
    -------
    {
        "is_digital": bool,
        "total_pages": int,
        "page_types": [{"page": 1, "type": "digital"|"scanned", "char_count": int}]
    }
    """
    doc = pymupdf.open(pdf_path)
    page_types = []
    digital_page_count = 0

    for i, page in enumerate(doc):
        text = page.get_text("text").strip()
        char_count = len(text)
        # Heuristic: if more than 50 chars on the page it likely has real text
        page_type = "digital" if char_count > 50 else "scanned"
        if page_type == "digital":
            digital_page_count += 1
        page_types.append({
            "page": i + 1,
            "type": page_type,
            "char_count": char_count
        })

    total = len(page_types)
    # If majority of pages are digital, treat whole doc as digital
    is_digital = digital_page_count > (total / 2)

    logger.info(
        f"[Classifier] {pdf_path} → "
        f"{'digital' if is_digital else 'scanned'} "
        f"({digital_page_count}/{total} digital pages)"
    )

    doc.close()
    return {
        "is_digital": is_digital,
        "total_pages": total,
        "page_types": page_types,
    }
