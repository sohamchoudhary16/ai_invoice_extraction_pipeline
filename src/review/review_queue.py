"""
src/review/review_queue.py
Writes documents that need human review to output/review_queue/.

Each review item gets:
  - a JSON sidecar explaining WHY it was flagged
  - the original PDF copied alongside

This gives a human reviewer everything in one place.
"""

import json
import os
import shutil
import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)

REVIEW_DIR = "output/review_queue"


def queue_for_review(
    pdf_path: str,
    record: dict,
    reasons: list[str],
    confidence_result: dict,
) -> str:
    """
    Write a review item to the queue.
    Returns path to the sidecar JSON file.
    """
    os.makedirs(REVIEW_DIR, exist_ok=True)
    basename = os.path.splitext(os.path.basename(pdf_path))[0]
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    stem = f"{basename}_{ts}"

    # Copy original PDF
    dest_pdf = os.path.join(REVIEW_DIR, f"{stem}.pdf")
    shutil.copy2(pdf_path, dest_pdf)

    # Write sidecar
    sidecar = {
        "queued_at": datetime.now(timezone.utc).isoformat(),
        "source_pdf": os.path.basename(pdf_path),
        "review_reasons": reasons,
        "confidence": confidence_result,
        "extracted_record": {
            k: v for k, v in record.items()
            if not k.startswith("_")
        },
        "validation_errors": record.get("_validation_errors", []),
        "review_status": "pending",
    }
    sidecar_path = os.path.join(REVIEW_DIR, f"{stem}_review.json")
    with open(sidecar_path, "w", encoding="utf-8") as f:
        json.dump(sidecar, f, indent=2, default=str)

    log.warning(
        "[ReviewQueue] Queued: %s — reasons: %s",
        os.path.basename(pdf_path), reasons
    )
    return sidecar_path
