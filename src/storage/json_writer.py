"""
src/storage/json_writer.py
Writes one merged JSON file containing all extracted invoice records.
output/zugferd/file.json
"""

import json
import logging
import os

logger = logging.getLogger(__name__)


def write_json(records: list[dict], output_path: str, indent: int = 2) -> None:
    """
    Write all records to a single JSON file.
    Creates parent directories if needed.
    """
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=indent, ensure_ascii=False, default=str)
    logger.info(f"[JSON Writer] Written {len(records)} record(s) → {output_path}")
