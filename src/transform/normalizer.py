"""
src/transform/normalizer.py
Maps raw extracted page data → flat dict aligned to schema.COLUMNS.
Handles type coercion for FLOAT_COLS and DATE_COLS.
"""

import re
import logging
from datetime import datetime
from src.models.schema import COLUMNS, FLOAT_COLS, DATE_COLS

logger = logging.getLogger(__name__)

# Common date formats seen in European invoices
_DATE_FORMATS = [
    "%Y-%m-%d",
    "%d.%m.%Y",
    "%d/%m/%Y",
    "%Y%m%d",
    "%d-%m-%Y",
    "%B %d, %Y",
]


def _parse_date(value: str) -> str | None:
    """Try to parse a date string into ISO format YYYY-MM-DD."""
    if not value:
        return None
    value = str(value).strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    logger.debug(f"  [Normalizer] Could not parse date: '{value}'")
    return value  # return as-is rather than dropping it


def _parse_float(value) -> float | None:
    """Parse a numeric string, handling European comma-decimal notation."""
    if value is None:
        return None
    s = str(value).strip()
    # Remove currency symbols and thousand separators
    s = re.sub(r"[€$£\s]", "", s)
    # European format: 1.234,56 → 1234.56
    if re.match(r"^\d{1,3}(\.\d{3})+(,\d+)?$", s):
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        logger.debug(f"  [Normalizer] Could not parse float: '{value}'")
        return None


def normalize_page_extractions(pages: list[dict], source_file: str) -> list[dict]:
    """
    Take raw page results from extractor and produce a list of flat dicts,
    one per invoice line item detected, aligned to schema.COLUMNS.

    Strategy:
    - Merge high-confidence OCR text fields with LLM extracted_fields
    - LLM fields take precedence over raw OCR text for structured fields
    - All column values are coerced to correct types
    """
    # Collect all LLM extracted fields across pages (merge pages into one invoice)
    merged_fields: dict = {}

    for page in pages:
        llm = page.get("llm_extraction", {})
        extracted = llm.get("extracted_fields", {})
        for field_name, field_data in extracted.items():
            if isinstance(field_data, dict):
                value = field_data.get("value")
            else:
                value = field_data
            if value and value != "null" and field_name not in merged_fields:
                merged_fields[field_name] = value

    # Build a base record aligned to COLUMNS
    record = {col: None for col in COLUMNS}
    record["SourceFile"] = source_file

    # Map extracted fields into record (case-insensitive match)
    col_lower_map = {col.lower(): col for col in COLUMNS}
    for raw_key, raw_val in merged_fields.items():
        normalized_key = col_lower_map.get(raw_key.lower())
        if normalized_key:
            record[normalized_key] = raw_val
        else:
            logger.debug(f"  [Normalizer] Unknown field from LLM: '{raw_key}' — skipped")

    # Type coercion
    for col in FLOAT_COLS:
        if record.get(col) is not None:
            record[col] = _parse_float(record[col])

    for col in DATE_COLS:
        if record.get(col) is not None:
            record[col] = _parse_date(record[col])

    # Return as list — future: support multi-line invoices (one record per LineId)
    return [record]
