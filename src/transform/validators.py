"""
src/transform/validators.py
Business-rule validation on normalized invoice records.
Flags issues without blocking the pipeline — everything gets written,
validation errors are surfaced as a "_validation_errors" key.
"""

import logging
from src.models.schema import FLOAT_COLS

logger = logging.getLogger(__name__)

# Fields that must be present in a valid invoice record
REQUIRED_FIELDS = ["InvoiceId", "IssueDate", "SellerName", "InvGrandTotal"]


def validate_record(record: dict) -> dict:
    """
    Run validation checks on a normalized record.
    Attaches a "_validation_errors" list to the record (empty = valid).
    Returns the record (mutated in place).
    """
    errors = []

    # 1. Required fields present
    for field in REQUIRED_FIELDS:
        if not record.get(field):
            errors.append(f"MISSING_REQUIRED_FIELD: {field}")

    # 2. Grand total is positive
    grand_total = record.get("InvGrandTotal")
    if grand_total is not None:
        try:
            if float(grand_total) < 0:
                errors.append(f"NEGATIVE_GRAND_TOTAL: {grand_total}")
        except (ValueError, TypeError):
            errors.append(f"INVALID_GRAND_TOTAL_TYPE: {grand_total}")

    # 3. Tax total sanity: InvTaxBasisTotal + InvTaxTotal ≈ InvGrandTotal
    basis = record.get("InvTaxBasisTotal")
    tax = record.get("InvTaxTotal")
    total = record.get("InvGrandTotal")
    if all(v is not None for v in [basis, tax, total]):
        try:
            computed = float(basis) + float(tax)
            if abs(computed - float(total)) > 0.05:
                errors.append(
                    f"TAX_SUM_MISMATCH: basis({basis}) + tax({tax}) = {computed:.2f} "
                    f"!= grand_total({total})"
                )
        except (ValueError, TypeError):
            pass

    # 4. Line total sanity: NetPrice * BilledQuantity ≈ LineTotalAmount
    net = record.get("NetPrice")
    qty = record.get("BilledQuantity")
    line = record.get("LineTotalAmount")
    if all(v is not None for v in [net, qty, line]):
        try:
            computed = float(net) * float(qty)
            if abs(computed - float(line)) > 0.05:
                errors.append(
                    f"LINE_TOTAL_MISMATCH: net({net}) * qty({qty}) = {computed:.2f} "
                    f"!= line_total({line})"
                )
        except (ValueError, TypeError):
            pass

    # 5. All declared FLOAT_COLS must actually be numeric if present
    for col in FLOAT_COLS:
        val = record.get(col)
        if val is not None:
            try:
                float(val)
            except (ValueError, TypeError):
                errors.append(f"NON_NUMERIC_FLOAT_FIELD: {col}={val}")

    record["_validation_errors"] = errors
    record["_is_valid"] = len(errors) == 0

    if errors:
        logger.warning(
            f"  [Validator] {record.get('SourceFile', '?')} — "
            f"{len(errors)} validation error(s): {errors}"
        )
    else:
        logger.info(f"  [Validator] {record.get('SourceFile', '?')} — valid ✅")

    return record
