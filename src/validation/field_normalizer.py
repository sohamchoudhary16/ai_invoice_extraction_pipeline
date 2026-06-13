"""
src/validation/field_normalizer.py
Type coercion and format normalisation for extracted field values.

v3 change: parse_float rewritten to correctly distinguish:
  - European format:  1.234,56  →  1234.56  (comma decimal, dot thousands)
  - US format:        1,234.56  →  1234.56  (dot decimal, comma thousands)
  - Plain decimal:    8.875     →  8.875    (was incorrectly → 8875.0)
  - Tax rate string:  19%       →  19.0
"""

import re
import logging
from datetime import datetime
from src.models.schema import COLUMNS, FLOAT_COLS, DATE_COLS

log = logging.getLogger(__name__)

_DATE_FORMATS = [
    "%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y",
    "%Y%m%d", "%d-%m-%Y", "%B %d, %Y",
]


def parse_date(value: str) -> str | None:
    """
    Parse a date string in any supported European/ISO format.

    Returns ISO YYYY-MM-DD string, the original string if unparseable,
    or None if input is empty.
    """
    if not value:
        return None
    value = str(value).strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    log.debug(f"Cannot parse date: '{value}'")
    return value   # return as-is rather than losing it


def parse_float(value) -> float | None:
    """
    Parse a numeric string to float. Handles:

    European format  (comma decimal, dot thousands):
        1.234,56   →  1234.56
        38.413,20  →  38413.20

    US format  (dot decimal, comma thousands):
        1,234.56   →  1234.56
        $35,144.85 →  35144.85

    Plain decimal (no thousands separator):
        8.875      →  8.875      ← was broken before v3
        1850.0     →  1850.0
        19%        →  19.0

    The key fix: European format requires BOTH a comma decimal separator
    AND dots as thousands separators with exactly 3 digits between each.
    A plain number like 8.875 has no comma so it cannot be European format.
    """
    if value is None:
        return None
    s = str(value).strip()

    # Strip currency symbols and whitespace
    s = re.sub(r"[€$£\s]", "", s)
    # Strip trailing percent
    s = s.rstrip("%")

    if not s:
        return None

    # ── European format: has comma AND dot-thousands ──────────────────────
    # Pattern: digits(1-3) + (.ddd)+ + ,decimals
    # Examples: 1.234,56 / 38.413,20 / 1.234.567,89
    # Requires a comma to be present — rules out plain decimals like 8.875
    if re.match(r"^\d{1,3}(\.\d{3})+,\d+$", s):
        s = s.replace(".", "").replace(",", ".")
        try:
            return float(s)
        except ValueError:
            return None

    # ── European format: dot-thousands but no decimal part ───────────────
    # Example: 1.234 could be European 1234 OR plain 1.234
    # We only treat as European if there are multiple dot-groups
    # (1.234.567 is clearly European; 1.234 is ambiguous — treat as decimal)
    if re.match(r"^\d{1,3}(\.\d{3}){2,}$", s):
        s = s.replace(".", "")
        try:
            return float(s)
        except ValueError:
            return None

    # ── US format: comma thousands + dot decimal ──────────────────────────
    # Examples: 1,234.56 / 35,144.85
    # Only treat as US thousands if: comma present, dot present after comma,
    # and digits after comma are exactly 3 (thousands separator pattern)
    if re.match(r"^\d{1,3}(,\d{3})+\.\d+$", s):
        s = s.replace(",", "")
        try:
            return float(s)
        except ValueError:
            return None

    # ── US format: comma thousands, no decimal ────────────────────────────
    # Example: 1,234 / 35,000
    if re.match(r"^\d{1,3}(,\d{3})+$", s):
        s = s.replace(",", "")
        try:
            return float(s)
        except ValueError:
            return None

    # ── Plain decimal or integer ──────────────────────────────────────────
    # Remove any remaining commas (stray formatting), try direct parse
    s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        log.debug(f"Cannot parse float: '{value}'")
        return None


def normalize_iban(value: str | None) -> str | None:
    """Remove spaces from IBAN and uppercase."""
    if not value:
        return None
    return re.sub(r"\s+", "", str(value)).upper()


def normalize_record(record: dict) -> dict:
    """
    Apply all type coercions to a flat record dict, then validate
    the result against the Pydantic InvoiceRecord schema.

    Returns the coerced dict (never raises — pipeline always continues).
    """
    for col in FLOAT_COLS:
        if record.get(col) is not None:
            record[col] = parse_float(record[col])
    for col in DATE_COLS:
        if record.get(col) is not None:
            record[col] = parse_date(record[col])
    if record.get("Iban"):
        record["Iban"] = normalize_iban(record["Iban"])

    # Pydantic validation pass — catches any remaining type/format issues
    try:
        from src.models.invoice import InvoiceRecord
        validated = InvoiceRecord.model_validate(record)
        coerced = validated.model_dump(exclude_none=False)
        for key, val in coerced.items():
            if key in record or val is not None:
                record[key] = val
        warnings = validated.get_format_warnings()
        if warnings:
            record["_pydantic_warnings"] = warnings
            log.warning(
                "pydantic_validation_warnings file=%s warnings=%s",
                record.get("SourceFile", "?"), warnings,
            )
    except Exception as e:
        log.warning(
            "pydantic_validation_failed file=%s error=%s",
            record.get("SourceFile", "?"), str(e),
        )

    return record


def normalize_line_items(raw_json_str: str) -> str:
    """
    Parse a _line_items_raw JSON string, normalize numeric fields,
    return cleaned JSON string.
    """
    import json
    if not raw_json_str:
        return "[]"
    try:
        items = json.loads(raw_json_str)
        numeric_fields = {
            "LineTotalAmount", "NetPrice", "DiscountAmount",
            "BilledQuantity", "TaxRatePercent"
        }
        for item in items:
            for field in numeric_fields:
                val = item.get(field)
                if val is not None:
                    item[field] = parse_float(str(val).replace("%", ""))
        return json.dumps(items)
    except (json.JSONDecodeError, Exception):
        return raw_json_str
