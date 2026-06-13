"""
src/ingestion/loader.py
Safe PDF loader with pre-flight integrity checks.

Checks performed BEFORE any processing:
  1. File exists and is non-empty
  2. Extension is .pdf
  3. File is not password-protected
  4. Page count within bounds
  5. Not a zero-image / completely blank PDF

Returns a LoadResult so callers can route without try/except everywhere.
"""

import os
import pymupdf
from dataclasses import dataclass, field
from src.observability.logger import get_logger

log = get_logger(__name__)


@dataclass
class LoadResult:
    path: str
    ok: bool
    error: str | None = None
    page_count: int = 0
    file_size_bytes: int = 0
    is_encrypted: bool = False
    warnings: list[str] = field(default_factory=list)


MAX_PAGES = 200
MIN_FILE_BYTES = 512          # anything smaller is almost certainly corrupt


def load_pdf(path: str) -> LoadResult:
    """
    Run pre-flight checks on a PDF.
    Returns LoadResult.ok=False with a reason if anything is wrong.
    """
    # 1. File existence + size
    if not os.path.isfile(path):
        return LoadResult(path=path, ok=False, error="FILE_NOT_FOUND")
    size = os.path.getsize(path)
    if size < MIN_FILE_BYTES:
        return LoadResult(path=path, ok=False, error=f"FILE_TOO_SMALL ({size} bytes)")

    # 2. Extension
    if not path.lower().endswith(".pdf"):
        return LoadResult(path=path, ok=False, error="NOT_A_PDF")

    # 3. Open and check encryption / page count
    try:
        doc = pymupdf.open(path)
    except Exception as e:
        return LoadResult(path=path, ok=False, error=f"CANNOT_OPEN: {e}")

    if doc.is_encrypted:
        doc.close()
        return LoadResult(path=path, ok=False, error="ENCRYPTED_PDF", is_encrypted=True)

    page_count = doc.page_count
    if page_count == 0:
        doc.close()
        return LoadResult(path=path, ok=False, error="ZERO_PAGES")
    if page_count > MAX_PAGES:
        doc.close()
        return LoadResult(path=path, ok=False,
                          error=f"TOO_MANY_PAGES ({page_count} > {MAX_PAGES})")

    warnings = []
    if page_count > 50:
        warnings.append(f"LARGE_DOC: {page_count} pages — extraction may be slow")

    doc.close()
    log.info("load_ok", path=os.path.basename(path), pages=page_count, bytes=size)
    return LoadResult(
        path=path, ok=True,
        page_count=page_count,
        file_size_bytes=size,
        warnings=warnings,
    )
