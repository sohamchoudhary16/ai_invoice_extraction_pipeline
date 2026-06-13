"""
src/storage/record_splitter.py
Splits flat extraction records into two normalised tables.

Your pipeline currently produces flat records with both invoice-header
fields and line-item fields mixed in one dict. This module separates them
into:
  invoice_records   — one dict per invoice (header fields only)
  line_item_records — one dict per line item (FK: SourceFile + InvoiceId)

Called from main.py after all records are collected — pipeline.py
is NOT touched.
"""

from src.observability.logger import get_logger

log = get_logger(__name__)

# Invoice header fields — same value for every line item in an invoice
_INVOICE_FIELDS = {
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
    # Internal metadata
    "_is_valid", "_validation_errors", "_confidence_score",
    "_confidence_tier", "_action", "_conflicts", "_is_duplicate",
}

# Line item fields — one value per line item
_LINE_ITEM_FIELDS = {
    "SourceFile", "InvoiceId",                    # FK to invoices table
    "LineId", "LineNote", "SellerProductId",
    "ProductName", "ProductDescription",
    "BuyerOrderLineId", "BilledQuantity", "BilledUnit",
    "NetPrice", "DiscountAmount", "LineTotalAmount",
    "TaxType", "TaxCategory", "TaxRatePercent",
    "BillingStartDate", "DiscountIndicator", "DiscountReason",
}

# Fields that count as a "real" line item (not an empty placeholder row)
_LINE_ITEM_REQUIRED = {"ProductName", "LineTotalAmount", "NetPrice", "BilledQuantity"}


def split_records(
    flat_records: list[dict],
    run_timestamp: str,
    file_hash: str = "",
) -> tuple[list[dict], list[dict]]:
    """
    Split a list of flat dicts (from pipeline.py) into
    (invoice_records, line_item_records).

    Deduplicates invoices — if multiple line items share the same
    SourceFile+InvoiceId, the invoice header is written only once.

    Returns
    -------
    (invoice_records, line_item_records)
    """
    seen_invoices: dict[str, dict] = {}
    line_items: list[dict] = []

    for row in flat_records:
        source = row.get("SourceFile", "")
        inv_id = row.get("InvoiceId", "")
        key    = f"{source}||{inv_id}"

        # ── Invoice record (deduplicated) ─────────────────────
        if key not in seen_invoices:
            inv_rec = {f: row.get(f) for f in _INVOICE_FIELDS}
            inv_rec["RunTimestamp"]     = run_timestamp
            inv_rec["FileHash"]         = file_hash
            inv_rec["ExtractionMethod"] = "llm_ocr"
            seen_invoices[key] = inv_rec

        # ── Line item record ──────────────────────────────────
        li_rec = {f: row.get(f) for f in _LINE_ITEM_FIELDS}
        # Only append if at least one meaningful line item field is present
        if any(li_rec.get(f) is not None for f in _LINE_ITEM_REQUIRED):
            line_items.append(li_rec)

    invoices = list(seen_invoices.values())
    log.info("records_split",
             flat_in=len(flat_records),
             invoices_out=len(invoices),
             line_items_out=len(line_items))

    return invoices, line_items
