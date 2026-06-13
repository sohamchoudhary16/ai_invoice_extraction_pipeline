"""
src/validation/field_normalizer.py
Type coercion and format normalisation for extracted field values.
Replaces the old transform/normalizer.py with stronger handling.
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
    if value is None:
        return None
    s = str(value).strip()
    s = re.sub(r"[€$£\s]", "", s)
    # Remove trailing % if present
    s = s.rstrip("%")
    # European thousand+decimal: 1.234,56 → 1234.56
    if re.match(r"^\d{1,3}(\.\d{3})+(,\d+)?$", s):
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        log.debug(f"Cannot parse float: '{value}'")
        return None


def normalize_iban(value: str | None) -> str | None:
    """Remove spaces from IBAN."""
    if not value:
        return None
    return re.sub(r"\s+", "", str(value)).upper()


def normalize_record(record: dict) -> dict:
    """Apply all type coercions to a flat record dict."""
    for col in FLOAT_COLS:
        if record.get(col) is not None:
            record[col] = parse_float(record[col])
    for col in DATE_COLS:
        if record.get(col) is not None:
            record[col] = parse_date(record[col])
    if record.get("Iban"):
        record["Iban"] = normalize_iban(record["Iban"])
    return record


def normalize_line_items(raw_json_str: str) -> str:
    """
    Parse _line_items_raw JSON string, normalize numeric fields,
    return cleaned JSON string.
    Handles European number strings like "22,200.00" and "19%".
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
