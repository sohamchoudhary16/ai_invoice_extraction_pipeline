"""
src/storage/json_writer.py
FIX: Timestamped output — runs never overwrite each other.

Output structure:
  output/processed/<run_timestamp>/invoices.json
  output/processed/<run_timestamp>/line_items.json
  output/processed/latest/invoices.json      ← always current run
  output/processed/latest/line_items.json

write_json() signature changed:
  OLD: write_json(records, path, indent)
  NEW: write_json(invoice_records, line_item_records, output_dir, run_timestamp, indent)
"""

import json
import os
import shutil
import logging

log = logging.getLogger(__name__)


def write_json(
    invoice_records: list[dict],
    line_item_records: list[dict],
    output_dir: str,
    run_timestamp: str,
    indent: int = 2,
) -> tuple[str, str]:
    """
    Write invoices + line items to timestamped JSON files.
    Also copies to output/processed/latest/ for stable downstream access.

    Returns (invoices_path, line_items_path)
    """
    run_dir = os.path.join(output_dir, "processed", run_timestamp)
    os.makedirs(run_dir, exist_ok=True)

    inv_path  = os.path.join(run_dir, "invoices.json")
    line_path = os.path.join(run_dir, "line_items.json")

    with open(inv_path, "w", encoding="utf-8") as f:
        json.dump(invoice_records, f, indent=indent, ensure_ascii=False, default=str)
    log.info("[JSON] %d invoice(s) → %s", len(invoice_records), inv_path)

    with open(line_path, "w", encoding="utf-8") as f:
        json.dump(line_item_records, f, indent=indent, ensure_ascii=False, default=str)
    log.info("[JSON] %d line item(s) → %s", len(line_item_records), line_path)

    # Update latest/
    latest_dir = os.path.join(output_dir, "processed", "latest")
    if os.path.exists(latest_dir):
        shutil.rmtree(latest_dir)
    shutil.copytree(run_dir, latest_dir)

    return inv_path, line_path
