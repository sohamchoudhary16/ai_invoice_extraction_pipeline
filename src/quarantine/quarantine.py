"""
src/quarantine/quarantine.py
Moves bad files to output/quarantine/ with a sidecar explaining why.

Triggered by load_pdf() failures:
  ENCRYPTED       — password-protected PDF
  CORRUPT         — cannot be opened
  TOO_MANY_PAGES  — exceeds page limit
  TOO_SMALL       — file too small to be real
  NOT_A_PDF       — wrong file type
  ZERO_PAGES      — empty PDF
"""

import os
import shutil
from datetime import datetime, timezone
from src.observability.logger import get_logger

log = get_logger(__name__)
QUARANTINE_DIR = "output/quarantine"


def quarantine_file(pdf_path: str, reason: str, detail: str = "") -> str:
    """
    Copy file to quarantine dir + write sidecar.
    Returns path of quarantined copy (empty string on failure).
    """
    os.makedirs(QUARANTINE_DIR, exist_ok=True)
    ts       = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    basename = os.path.basename(pdf_path)
    dest     = os.path.join(QUARANTINE_DIR, f"{ts}_{basename}")

    try:
        shutil.copy2(pdf_path, dest)
    except Exception as e:
        log.error("quarantine_copy_failed", src=pdf_path, error=str(e))
        return ""

    sidecar = dest.replace(".pdf", "_why.txt")
    with open(sidecar, "w", encoding="utf-8") as f:
        f.write(f"File:        {basename}\n")
        f.write(f"Quarantined: {datetime.now(timezone.utc).isoformat()}\n")
        f.write(f"Reason:      {reason}\n")
        f.write(f"Detail:      {detail}\n")

    log.warning("file_quarantined", file=basename, reason=reason, dest=dest)
    return dest
