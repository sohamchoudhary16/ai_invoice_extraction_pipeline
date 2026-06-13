"""
src/models/schema.py
Column definitions derived from the Pydantic InvoiceRecord model.

v2 change: COLUMNS is now derived from InvoiceRecord.model_fields so
there is a single source of truth.  FLOAT_COLS and DATE_COLS are still
maintained as sets here because they are used by field_normalizer.py
and sqlite_writer.py independently of Pydantic.

Backward compatibility: all existing imports of COLUMNS, FLOAT_COLS,
DATE_COLS continue to work without any changes to other files.
"""

# ── Derive COLUMNS from the Pydantic model ────────────────────────────────────
# We import lazily inside a try block so that if Pydantic is somehow not
# installed, the old hardcoded list is used as a fallback — existing code
# never breaks.

try:
    from src.models.invoice import InvoiceRecord, LineItem

    # Invoice header columns — all public fields on InvoiceRecord
    _INV_FIELDS = [
        k for k in InvoiceRecord.model_fields
        if not k.startswith("_")
    ]

    # Line item columns — all public fields on LineItem
    _LINE_FIELDS = [
        k for k in LineItem.model_fields
        if not k.startswith("_")
    ]

    # Combined flat column list (invoice fields first, then line item fields,
    # deduped while preserving order)
    _seen: set[str] = set()
    COLUMNS: list[str] = []
    for col in _INV_FIELDS + _LINE_FIELDS:
        if col not in _seen:
            COLUMNS.append(col)
            _seen.add(col)

except Exception:
    # Fallback to hardcoded list — keeps existing code working if invoice.py
    # import fails (e.g. during a fresh install before pydantic is available)
    COLUMNS = [
        "SourceFile", "FileHash", "RunTimestamp", "ExtractionMethod",
        "InvoiceId", "InvoiceTypeCode", "IssueDate", "ReferencedInvoiceId",
        "Currency", "BuyerReference",
        "SellerName", "SellerContactName", "SellerPhone", "SellerEmail",
        "SellerStreet", "SellerPostcode", "SellerCity", "SellerCountry", "SellerVatId",
        "BuyerId", "BuyerName", "BuyerStreet", "BuyerPostcode",
        "BuyerCity", "BuyerCountry", "BuyerEmail",
        "ShipToName", "ShipToStreet", "ShipToPostcode", "ShipToCity", "ShipToCountry",
        "InvLineTotal", "InvChargeTotal", "InvAllowanceTotal",
        "InvTaxBasisTotal", "InvTaxTotal", "InvTaxTotalCurrencyId",
        "InvRounding", "InvGrandTotal", "InvPrepaidTotal", "InvDuePayable",
        "PaymentMeansCode", "Iban", "BankAccountName", "Bic", "PaymentTerms", "DueDate",
        "LineId", "LineNote", "SellerProductId", "ProductName", "ProductDescription",
        "BuyerOrderLineId", "BilledUnit", "TaxType", "TaxCategory",
        "BilledQuantity", "NetPrice", "DiscountAmount", "LineTotalAmount",
        "TaxRatePercent", "BillingStartDate", "DiscountIndicator", "DiscountReason",
    ]


# ── These stay hardcoded — they are semantic, not structural ──────────────────

FLOAT_COLS: set[str] = {
    "NetPrice", "BilledQuantity", "TaxRatePercent", "DiscountAmount",
    "LineTotalAmount", "InvLineTotal", "InvChargeTotal", "InvAllowanceTotal",
    "InvTaxBasisTotal", "InvTaxTotal", "InvRounding", "InvGrandTotal",
    "InvPrepaidTotal", "InvDuePayable",
}

DATE_COLS: set[str] = {"IssueDate", "DueDate", "BillingStartDate"}
