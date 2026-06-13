"""
src/validation/business_rules.py
Business rule validation on normalised invoice records.
Produces structured error list — does NOT block pipeline.
Replaces the old transform/validators.py with more comprehensive checks.
"""

import re
import logging
from src.models.schema import FLOAT_COLS

log = logging.getLogger(__name__)

REQUIRED_FIELDS = ["InvoiceId", "IssueDate", "SellerName", "InvGrandTotal", "Currency"]

_TOLERANCE = 0.06   # EUR tolerance for math checks (rounding)

# German IBAN: DE + 2 check digits + 18 alphanumeric
_IBAN_PATTERN = re.compile(r"^DE\d{2}[A-Z0-9]{18}$")
# German VAT ID: DE + 9 digits
_VAT_DE_PATTERN = re.compile(r"^DE\d{9}$")
# SWIFT/BIC: 8 or 11 chars
_BIC_PATTERN = re.compile(r"^[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}([A-Z0-9]{3})?$")


def validate(record: dict) -> list[str]:
    """
    Run all validation checks. Returns list of error strings.
    Empty list = all checks passed.
    """
    errors: list[str] = []

    # A. Required fields
    for f in REQUIRED_FIELDS:
        if not record.get(f):
            errors.append(f"MISSING_REQUIRED: {f}")

    # B. Grand total positive
    _assert_positive(record, "InvGrandTotal", errors)

    # C. Tax math: TaxBasis + TaxTotal ≈ GrandTotal
    basis = _f(record.get("InvTaxBasisTotal"))
    tax   = _f(record.get("InvTaxTotal"))
    total = _f(record.get("InvGrandTotal"))
    if all(v is not None for v in [basis, tax, total]):
        computed = basis + tax
        if abs(computed - total) > _TOLERANCE:
            errors.append(
                f"TAX_MATH: {basis} + {tax} = {computed:.2f} ≠ {total}"
            )

    # D. Line total math: NetPrice × Qty ≈ LineTotalAmount
    net  = _f(record.get("NetPrice"))
    qty  = _f(record.get("BilledQuantity"))
    line = _f(record.get("LineTotalAmount"))
    if all(v is not None for v in [net, qty, line]):
        computed = net * qty
        if abs(computed - line) > _TOLERANCE:
            errors.append(
                f"LINE_MATH: {net} × {qty} = {computed:.2f} ≠ {line}"
            )

    # E. Due date >= Issue date
    issue = record.get("IssueDate")
    due   = record.get("DueDate")
    if issue and due:
        try:
            if due < issue:
                errors.append(f"DUE_BEFORE_ISSUE: due={due} < issue={issue}")
        except TypeError:
            pass

    # F. IBAN format (German)
    iban = record.get("Iban")
    if iban and not _IBAN_PATTERN.match(iban.replace(" ", "").upper()):
        errors.append(f"INVALID_IBAN: '{iban}'")

    # G. VAT ID format (German)
    vat = record.get("SellerVatId")
    if vat and not _VAT_DE_PATTERN.match(vat.replace(" ", "").upper()):
        errors.append(f"INVALID_VAT_ID: '{vat}'")

    # H. BIC format
    bic = record.get("Bic")
    if bic and not _BIC_PATTERN.match(bic.strip().upper()):
        errors.append(f"INVALID_BIC: '{bic}'")

    # I. Currency is valid ISO 4217 (simple allowlist)
    currency = record.get("Currency")
    if currency and currency.upper() not in {
        "EUR", "USD", "GBP", "CHF", "JPY", "CNY", "SEK", "NOK", "DKK", "PLN"
    }:
        errors.append(f"UNKNOWN_CURRENCY: '{currency}'")

    # J. All FLOAT_COLS that have a value must be numeric
    for col in FLOAT_COLS:
        val = record.get(col)
        if val is not None:
            try:
                float(val)
            except (ValueError, TypeError):
                errors.append(f"NON_NUMERIC: {col}='{val}'")

    if errors:
        log.warning("[Validator] %s — %d error(s)", record.get("SourceFile", "?"), len(errors))
    return errors


def _f(v) -> float | None:
    try:
        return float(v) if v is not None else None
    except (ValueError, TypeError):
        return None


def _assert_positive(record: dict, field: str, errors: list):
    val = _f(record.get(field))
    if val is not None and val < 0:
        errors.append(f"NEGATIVE_VALUE: {field}={val}")
