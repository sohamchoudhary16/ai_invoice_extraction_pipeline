"""
src/extraction/merger.py
Merges multi-page extraction results into invoice records.

Output: one flat dict PER LINE ITEM (matching COLUMNS schema).
Header fields (seller, buyer, totals, payment) are repeated on every row.
If no line items extracted, returns one row with nulls for line item fields.

This matches the schema design where LineId, ProductName, NetPrice etc
are flat columns — meaning one row per line item, not one row per invoice.
"""

import json
from src.models.schema import COLUMNS
from src.observability.logger import get_logger

log = get_logger(__name__)

# Header fields — same value on every line item row
_HEADER_FIELDS = {
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
}

# Line item fields — one value per line item row
_LINE_ITEM_FIELDS = {
    "LineId", "LineNote", "SellerProductId", "ProductName", "ProductDescription",
    "BuyerOrderLineId", "BilledUnit", "TaxType", "TaxCategory",
    "BillingStartDate", "DiscountIndicator", "DiscountReason",
    "NetPrice", "BilledQuantity", "TaxRatePercent", "DiscountAmount", "LineTotalAmount",
}

_LAST_WINS = {
    "InvLineTotal", "InvChargeTotal", "InvAllowanceTotal",
    "InvTaxBasisTotal", "InvTaxTotal", "InvGrandTotal",
    "InvRounding", "InvPrepaidTotal", "InvDuePayable",
}

_SKIP_KEYS = {"line_items", "extracted_fields", "_extraction_notes"}

_NULL_SENTINELS = {"null", "none", "n/a", "na", "", "unknown"}


def _is_null(value) -> bool:
    if value is None:
        return True
    return str(value).strip().lower() in _NULL_SENTINELS


def _normalise_field(field_data) -> dict:
    if isinstance(field_data, dict):
        return field_data
    return {"value": field_data, "confidence": "medium", "source_text": ""}


def _extract_line_item_fields(item: dict) -> dict:
    """Flatten a line item dict (handles both flat and nested values)."""
    row = {}
    for field, val in item.items():
        if isinstance(val, dict):
            row[field] = val.get("value")
        else:
            row[field] = val if not _is_null(val) else None
    return row


def merge_page_results(page_results: list[dict]) -> list[dict]:
    """
    Merge multi-page extraction results.

    Returns a LIST of flat dicts — one per line item.
    Each dict contains all header fields + one line item's fields.
    If no line items, returns a single dict with null line item fields.
    """
    header: dict[str, dict] = {}
    conflicts: dict[str, list] = {}
    all_line_items: list[dict] = []

    for page in page_results:
        extracted = page.get("extracted", {})
        if not extracted or not isinstance(extracted, dict):
            continue

        # Resolve response shape (flat vs nested)
        if "extracted_fields" in extracted and isinstance(
            extracted["extracted_fields"], dict
        ):
            raw_fields = dict(extracted["extracted_fields"])
        else:
            raw_fields = dict(extracted)

        # Pull line items
        line_items = raw_fields.pop("line_items", None) or []
        for sk in _SKIP_KEYS:
            raw_fields.pop(sk, None)

        if isinstance(line_items, list):
            all_line_items.extend(line_items)

        # Merge header fields
        for field_name, field_data in raw_fields.items():
            field_dict = _normalise_field(field_data)
            value = field_dict.get("value")
            if _is_null(value):
                continue
            if field_name not in header:
                header[field_name] = field_dict
            elif field_name in _LAST_WINS:
                header[field_name] = field_dict
            else:
                existing = header[field_name].get("value")
                if str(existing).strip() != str(value).strip():
                    if field_name not in conflicts:
                        conflicts[field_name] = [existing]
                    conflicts[field_name].append(value)

    if conflicts:
        log.warning("merge_conflicts", fields=list(conflicts.keys()))

    # Build flat header dict
    flat_header: dict = {col: None for col in COLUMNS}
    for field_name, field_dict in header.items():
        if field_name in flat_header:
            flat_header[field_name] = field_dict.get("value")

    # Internal metadata
    flat_header["_conflicts"]      = json.dumps(conflicts) if conflicts else ""
    flat_header["_field_metadata"] = json.dumps({
        k: {"confidence": v.get("confidence"), "source": v.get("source_text", "")}
        for k, v in header.items()
    })

    # Expand into one row per line item
    if all_line_items:
        rows = []
        for item in all_line_items:
            row = dict(flat_header)   # copy header fields
            li = _extract_line_item_fields(item)
            # Map line item fields into row
            for field in _LINE_ITEM_FIELDS:
                val = li.get(field)
                if val is not None:
                    row[field] = val
            row["_line_items_raw"] = ""   # not needed per-row
            rows.append(row)
        log.info("line_items_expanded",
                 invoice=flat_header.get("InvoiceId"),
                 rows=len(rows))
        return rows
    else:
        # No line items — return single header row
        flat_header["_line_items_raw"] = "[]"
        log.warning("no_line_items_extracted",
                    invoice=flat_header.get("InvoiceId"))
        return [flat_header]
