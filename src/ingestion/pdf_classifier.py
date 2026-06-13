"""
src/ingestion/pdf_classifier.py
Classifies each page of a PDF as:
  - digital   : has extractable text layer
  - scanned   : image-only, needs OCR
  - mixed     : some digital pages, some scanned

Also checks for embedded ZUGFeRD/Factur-X XML
(if present, we can parse that directly and skip OCR entirely).
"""

import io
import pymupdf
from dataclasses import dataclass, field
from src.observability.logger import get_logger

log = get_logger(__name__)

# If a page has fewer than this many characters it is treated as scanned
_DIGITAL_CHAR_THRESHOLD = 50


@dataclass
class PageClass:
    page_number: int        # 1-based
    kind: str               # "digital" | "scanned"
    char_count: int


@dataclass
class DocClassification:
    path: str
    doc_kind: str           # "digital" | "scanned" | "mixed"
    total_pages: int
    digital_pages: int
    scanned_pages: int
    has_zugferd_xml: bool
    zugferd_xml_bytes: bytes | None
    page_classes: list[PageClass] = field(default_factory=list)


def classify_pdf(path: str) -> DocClassification:
    doc = pymupdf.open(path)
    page_classes = []
    digital_count = 0

    for i, page in enumerate(doc):
        text = page.get_text("text").strip()
        char_count = len(text)
        kind = "digital" if char_count >= _DIGITAL_CHAR_THRESHOLD else "scanned"
        if kind == "digital":
            digital_count += 1
        page_classes.append(PageClass(page_number=i + 1, kind=kind, char_count=char_count))

    total = len(page_classes)
    scanned_count = total - digital_count

    if digital_count == total:
        doc_kind = "digital"
    elif scanned_count == total:
        doc_kind = "scanned"
    else:
        doc_kind = "mixed"

    # Check for embedded ZUGFeRD / Factur-X XML
    xml_bytes = _extract_embedded_xml(doc)

    doc.close()

    result = DocClassification(
        path=path,
        doc_kind=doc_kind,
        total_pages=total,
        digital_pages=digital_count,
        scanned_pages=scanned_count,
        has_zugferd_xml=xml_bytes is not None,
        zugferd_xml_bytes=xml_bytes,
        page_classes=page_classes,
    )
    log.info("pdf_classified",
             path=path, kind=doc_kind,
             digital=digital_count, scanned=scanned_count,
             has_xml=result.has_zugferd_xml)
    return result


def _extract_embedded_xml(doc: pymupdf.Document) -> bytes | None:
    """Return first embedded XML payload (ZUGFeRD/Factur-X), or None."""
    try:
        # PyMuPDF way to access embedded files
        count = doc.embfile_count()
        for i in range(count):
            info = doc.embfile_info(i)
            name = info.get("filename", "").lower()
            if name.endswith(".xml"):
                return doc.embfile_get(i)
    except Exception:
        pass
    return None
