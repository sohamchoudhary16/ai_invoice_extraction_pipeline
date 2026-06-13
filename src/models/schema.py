"""
src/models/schema.py  —  Column list + pandas dtype maps
"""

FLOAT_COLS = {
    "NetPrice", "BilledQuantity", "TaxRatePercent", "DiscountAmount",
    "LineTotalAmount", "InvLineTotal", "InvChargeTotal", "InvAllowanceTotal",
    "InvTaxBasisTotal", "InvTaxTotal", "InvRounding", "InvGrandTotal",
    "InvPrepaidTotal", "InvDuePayable",
}

DATE_COLS = {"IssueDate", "DueDate", "BillingStartDate"}

COLUMNS = [
    "SourceFile",
    "InvoiceId", "InvoiceTypeCode", "IssueDate", "ReferencedInvoiceId",
    "Currency", "BuyerReference",
    "SellerName", "SellerContactName", "SellerPhone", "SellerEmail",
    "SellerStreet", "SellerPostcode", "SellerCity", "SellerCountry", "SellerVatId",
    "BuyerId", "BuyerName", "BuyerStreet", "BuyerPostcode",
    "BuyerCity", "BuyerCountry", "BuyerEmail",
    "ShipToName", "ShipToStreet", "ShipToPostcode", "ShipToCity", "ShipToCountry",
    "NetPrice", "BilledQuantity", "TaxRatePercent", "DiscountAmount", "LineTotalAmount",
    "InvLineTotal", "InvChargeTotal", "InvAllowanceTotal",
    "InvTaxBasisTotal", "InvTaxTotal", "InvTaxTotalCurrencyId",
    "InvRounding", "InvGrandTotal", "InvPrepaidTotal", "InvDuePayable",
    "PaymentMeansCode", "Iban", "BankAccountName", "Bic", "PaymentTerms", "DueDate",
    "LineId", "LineNote", "SellerProductId", "ProductName", "ProductDescription",
    "BuyerOrderLineId", "BilledUnit", "TaxType", "TaxCategory",
    "BillingStartDate", "DiscountIndicator", "DiscountReason",
]
