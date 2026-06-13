"""
src/models/invoice.py
Pydantic v2 data models for the invoice extraction pipeline.

Models
------
LineItem        — one row in invoice_line_items table
InvoiceRecord   — one row in invoices table
ExtractionResult — full pipeline result for one PDF

Usage
-----
    from src.models.invoice import InvoiceRecord, LineItem, ExtractionResult

    record = InvoiceRecord.model_validate(raw_dict)
    row    = record.to_storage_dict()           # flat dict for SQLite / CSV
"""

import re
from typing import Optional
from pydantic import BaseModel, field_validator, model_validator, Field


# ─────────────────────────────────────────────────────────────────────────────
# Shared parsing helpers
# ─────────────────────────────────────────────────────────────────────────────

_DATE_FORMATS = [
    "%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y",
    "%Y%m%d", "%d-%m-%Y", "%B %d, %Y",
]

_KNOWN_CURRENCIES = {
    "EUR", "USD", "GBP", "CHF", "JPY", "CNY",
    "SEK", "NOK", "DKK", "PLN", "CZK", "HUF",
}

_IBAN_DE_RE = re.compile(r"^DE\d{2}[A-Z0-9]{18}$")
_VAT_DE_RE  = re.compile(r"^DE\d{9}$")
_BIC_RE     = re.compile(r"^[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}([A-Z0-9]{3})?$")


def _parse_date_str(v: str) -> str:
    """Try all known date formats; return ISO YYYY-MM-DD or the original string."""
    from datetime import datetime
    v = str(v).strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(v, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return v   # return as-is; don't lose the value


def _parse_float_str(v) -> Optional[float]:
    """Parse European or US decimal strings to float."""
    if v is None:
        return None
    s = str(v).strip()
    s = re.sub(r"[€$£\s]", "", s).rstrip("%")
    if re.match(r"^\d{1,3}(\.\d{3})+(,\d+)?$", s):
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


_NULL_STRS = {"null", "none", "n/a", "na", ""}


def _is_null(v) -> bool:
    return v is None or str(v).strip().lower() in _NULL_STRS


# ─────────────────────────────────────────────────────────────────────────────
# LineItem
# ─────────────────────────────────────────────────────────────────────────────

class LineItem(BaseModel):
    """
    One line item from an invoice.
    Maps 1-to-1 with a row in the invoice_line_items SQLite table.
    """

    SourceFile:         Optional[str]   = None
    InvoiceId:          Optional[str]   = None
    LineId:             Optional[str]   = None
    LineNote:           Optional[str]   = None
    SellerProductId:    Optional[str]   = None
    BuyerOrderLineId:   Optional[str]   = None
    ProductName:        Optional[str]   = None
    ProductDescription: Optional[str]   = None
    BilledQuantity:     Optional[float] = None
    BilledUnit:         Optional[str]   = None
    NetPrice:           Optional[float] = None
    DiscountAmount:     Optional[float] = None
    LineTotalAmount:    Optional[float] = None
    TaxRatePercent:     Optional[float] = None
    TaxCategory:        Optional[str]   = None
    TaxType:            Optional[str]   = None
    BillingStartDate:   Optional[str]   = None
    DiscountIndicator:  Optional[str]   = None
    DiscountReason:     Optional[str]   = None

    # Runtime warning — NOT a Pydantic field; set in model_post_init
    line_math_warning: Optional[str] = Field(default=None, exclude=True)

    @field_validator(
        "BilledQuantity", "NetPrice", "DiscountAmount",
        "LineTotalAmount", "TaxRatePercent",
        mode="before",
    )
    @classmethod
    def coerce_float(cls, v: object) -> Optional[float]:
        """Accept raw LLM strings like '22,200.00' or '19%' and coerce to float."""
        if _is_null(v):
            return None
        return _parse_float_str(v)

    @field_validator("BillingStartDate", mode="before")
    @classmethod
    def coerce_date(cls, v: object) -> Optional[str]:
        if _is_null(v):
            return None
        return _parse_date_str(str(v))

    def model_post_init(self, __context) -> None:
        """Soft line-math check after all fields are set."""
        net   = self.NetPrice
        qty   = self.BilledQuantity
        total = self.LineTotalAmount
        if net is not None and qty is not None and total is not None:
            computed = net * qty
            if abs(computed - total) > 0.06:
                # Use object.__setattr__ to bypass Pydantic's frozen guard
                object.__setattr__(
                    self, "line_math_warning",
                    f"LINE_MATH: {net} × {qty} = {computed:.2f} ≠ {total}",
                )

    model_config = {
        "populate_by_name": True,
        "str_strip_whitespace": True,
    }


# ─────────────────────────────────────────────────────────────────────────────
# InvoiceRecord
# ─────────────────────────────────────────────────────────────────────────────

class InvoiceRecord(BaseModel):
    """
    Invoice header — one row in the invoices SQLite table.

    Pipeline quality metadata keys (_confidence_score etc.) arrive in the
    raw dict with leading underscores.  Pydantic v2 forbids leading-underscore
    field names, so they are declared WITHOUT the underscore prefix here and
    mapped via aliases.  to_storage_dict() re-adds the underscores so the
    SQLite writer keeps working without changes.
    """

    # ── Pipeline metadata ─────────────────────────────────────
    SourceFile:         Optional[str] = None
    FileHash:           Optional[str] = None
    RunTimestamp:       Optional[str] = None
    ExtractionMethod:   Optional[str] = None

    # ── Invoice identity ──────────────────────────────────────
    InvoiceId:          Optional[str] = None
    InvoiceTypeCode:    Optional[str] = None
    IssueDate:          Optional[str] = None
    DueDate:            Optional[str] = None
    ReferencedInvoiceId:Optional[str] = None
    Currency:           Optional[str] = None
    BuyerReference:     Optional[str] = None

    # ── Seller ────────────────────────────────────────────────
    SellerName:         Optional[str] = None
    SellerContactName:  Optional[str] = None
    SellerPhone:        Optional[str] = None
    SellerEmail:        Optional[str] = None
    SellerStreet:       Optional[str] = None
    SellerPostcode:     Optional[str] = None
    SellerCity:         Optional[str] = None
    SellerCountry:      Optional[str] = None
    SellerVatId:        Optional[str] = None

    # ── Buyer ─────────────────────────────────────────────────
    BuyerId:            Optional[str] = None
    BuyerName:          Optional[str] = None
    BuyerStreet:        Optional[str] = None
    BuyerPostcode:      Optional[str] = None
    BuyerCity:          Optional[str] = None
    BuyerCountry:       Optional[str] = None
    BuyerEmail:         Optional[str] = None

    # ── Ship-to ───────────────────────────────────────────────
    ShipToName:         Optional[str] = None
    ShipToStreet:       Optional[str] = None
    ShipToPostcode:     Optional[str] = None
    ShipToCity:         Optional[str] = None
    ShipToCountry:      Optional[str] = None

    # ── Invoice totals ────────────────────────────────────────
    InvLineTotal:       Optional[float] = None
    InvChargeTotal:     Optional[float] = None
    InvAllowanceTotal:  Optional[float] = None
    InvTaxBasisTotal:   Optional[float] = None
    InvTaxTotal:        Optional[float] = None
    InvTaxTotalCurrencyId: Optional[str] = None
    InvRounding:        Optional[float] = None
    InvGrandTotal:      Optional[float] = None
    InvPrepaidTotal:    Optional[float] = None
    InvDuePayable:      Optional[float] = None

    # ── Payment ───────────────────────────────────────────────
    PaymentMeansCode:   Optional[str] = None
    Iban:               Optional[str] = None
    BankAccountName:    Optional[str] = None
    Bic:                Optional[str] = None
    PaymentTerms:       Optional[str] = None

    # ── Pipeline quality metadata ─────────────────────────────
    # Declared WITHOUT leading underscore (Pydantic v2 rule).
    # Aliases map the underscore names from the raw pipeline dict.
    # to_storage_dict() re-adds underscores for SQLite compatibility.
    confidence_score:      Optional[str] = Field(default=None, alias="_confidence_score")
    confidence_tier:       Optional[str] = Field(default=None, alias="_confidence_tier")
    action:                Optional[str] = Field(default=None, alias="_action")
    is_valid:              Optional[str] = Field(default=None, alias="_is_valid")
    validation_errors_raw: Optional[str] = Field(default=None, alias="_validation_errors")
    is_duplicate:          Optional[str] = Field(default=None, alias="_is_duplicate")
    conflicts:             Optional[str] = Field(default=None, alias="_conflicts")

    # Runtime warnings — set in model_post_init, excluded from model_dump
    format_warnings_list:  list[str] = Field(default_factory=list, exclude=True)

    # -- Field validators --

    @field_validator("format_warnings_list", mode="before")
    @classmethod
    def coerce_warnings_list(cls, v: object) -> list:
        """Coerce None -> [] so an explicit None never overrides default_factory."""
        if v is None:
            return []
        if isinstance(v, list):
            return v
        return []

    @field_validator("IssueDate", "DueDate", mode="before")
    @classmethod
    def coerce_date(cls, v: object) -> Optional[str]:
        """Accept any supported date format; return ISO YYYY-MM-DD."""
        if _is_null(v):
            return None
        return _parse_date_str(str(v))

    @field_validator(
        "InvLineTotal", "InvChargeTotal", "InvAllowanceTotal",
        "InvTaxBasisTotal", "InvTaxTotal", "InvRounding",
        "InvGrandTotal", "InvPrepaidTotal", "InvDuePayable",
        mode="before",
    )
    @classmethod
    def coerce_float(cls, v: object) -> Optional[float]:
        """Coerce European and US currency strings to float."""
        if _is_null(v):
            return None
        return _parse_float_str(v)

    @field_validator("Iban", mode="before")
    @classmethod
    def normalise_iban(cls, v: object) -> Optional[str]:
        """Strip spaces and uppercase."""
        if _is_null(v):
            return None
        return re.sub(r"\s+", "", str(v)).upper()

    @field_validator("Currency", mode="before")
    @classmethod
    def normalise_currency(cls, v: object) -> Optional[str]:
        if _is_null(v):
            return None
        return str(v).strip().upper()

    # ── Model-level post-init checks ──────────────────────────

    def model_post_init(self, __context) -> None:
        """
        Run soft checks after all fields are validated and coerced.
        Warnings are collected in format_warnings_list — the pipeline
        is never blocked; warnings feed into the review queue logic.
        """
        warnings: list[str] = []

        # Tax math: basis + tax ≈ grand total
        basis = self.InvTaxBasisTotal
        tax   = self.InvTaxTotal
        total = self.InvGrandTotal
        if all(v is not None for v in [basis, tax, total]):
            computed = basis + tax
            if abs(computed - total) > 0.06:
                warnings.append(
                    f"TAX_MATH: {basis} + {tax} = {computed:.2f} ≠ {total}"
                )

        # Date ordering: due must not be before issue
        if self.IssueDate and self.DueDate:
            try:
                if self.DueDate < self.IssueDate:
                    warnings.append(
                        f"DUE_BEFORE_ISSUE: due={self.DueDate} < issue={self.IssueDate}"
                    )
            except TypeError:
                pass

        # IBAN format
        if self.Iban and not _IBAN_DE_RE.match(self.Iban):
            warnings.append(f"INVALID_IBAN: '{self.Iban}'")

        # VAT ID format
        if self.SellerVatId:
            vat = re.sub(r"\s+", "", self.SellerVatId).upper()
            if not _VAT_DE_RE.match(vat):
                warnings.append(f"INVALID_VAT_ID: '{self.SellerVatId}'")

        # BIC format
        if self.Bic:
            bic = self.Bic.strip().upper()
            if not _BIC_RE.match(bic):
                warnings.append(f"INVALID_BIC: '{self.Bic}'")

        # Currency allowlist
        if self.Currency and self.Currency not in _KNOWN_CURRENCIES:
            warnings.append(f"UNKNOWN_CURRENCY: '{self.Currency}'")

        object.__setattr__(self, "format_warnings_list", warnings)

    # ── Serialisation ─────────────────────────────────────────

    def to_storage_dict(self) -> dict:
        """
        Return a flat dict for SQLite / CSV writers.

        Re-adds the leading underscores to the pipeline metadata keys
        so sqlite_writer.py needs no changes.  All values are str.
        """
        # model_dump uses field names (no underscore); exclude runtime-only fields
        base = self.model_dump(exclude={"format_warnings_list"})

        # Rename pipeline metadata keys back to underscore form
        renames = {
            "confidence_score":      "_confidence_score",
            "confidence_tier":       "_confidence_tier",
            "action":                "_action",
            "is_valid":              "_is_valid",
            "validation_errors_raw": "_validation_errors",
            "is_duplicate":          "_is_duplicate",
            "conflicts":             "_conflicts",
        }
        for src, dst in renames.items():
            if src in base:
                base[dst] = base.pop(src)

        return {k: ("" if v is None else str(v)) for k, v in base.items()}

    def get_format_warnings(self) -> list[str]:
        """Return all soft warnings collected during post-init."""
        return self.format_warnings_list or []

    model_config = {
        "populate_by_name": True,       # accept both alias and field name
        "str_strip_whitespace": True,
    }


# ─────────────────────────────────────────────────────────────────────────────
# ExtractionResult
# ─────────────────────────────────────────────────────────────────────────────

class ExtractionResult(BaseModel):
    """
    Complete result for one PDF — wraps invoice header, line items,
    and all pipeline quality signals in a single typed object.

    Used by:
      - main.py           (collect and route results)
      - Streamlit UI      (display results)
      - evaluation script (compare against ground truth)
    """

    source_file:        str
    ok:                 bool
    error:              Optional[str]   = None
    doc_kind:           Optional[str]   = None
    total_pages:        Optional[int]   = None
    invoice:            Optional[InvoiceRecord] = None
    line_items:         list[LineItem]  = Field(default_factory=list)
    avg_ocr_confidence: Optional[float] = None
    composite_score:    Optional[float] = None
    confidence_tier:    Optional[str]   = None
    action:             Optional[str]   = None
    validation_errors:  list[str]       = Field(default_factory=list)
    format_warnings:    list[str]       = Field(default_factory=list)
    queued_for_review:  bool            = False
    review_reasons:     list[str]       = Field(default_factory=list)

    @classmethod
    def from_pipeline_result(cls, result: dict) -> "ExtractionResult":
        """
        Build an ExtractionResult from the raw dict returned by run_pipeline().

        This is the bridge between the dict-based pipeline and the typed
        model layer — no changes to pipeline.py are required.
        """
        record_dict: dict = result.get("record") or {}
        conf: dict        = result.get("confidence") or {}

        invoice: Optional[InvoiceRecord] = None
        if record_dict:
            try:
                invoice = InvoiceRecord.model_validate(record_dict)
            except Exception:
                pass

        raw_records: list[dict] = result.get("records") or []
        line_items: list[LineItem] = []
        for r in raw_records:
            try:
                line_items.append(LineItem.model_validate(r))
            except Exception:
                pass

        page_results = result.get("page_results") or []
        avg_ocr = (
            sum(p.get("avg_confidence", 100) for p in page_results)
            / max(len(page_results), 1)
        ) if page_results else None

        return cls(
            source_file        = result.get("source_file", ""),
            ok                 = result.get("ok", False),
            error              = result.get("error"),
            doc_kind           = result.get("classification", {}).get("doc_kind"),
            total_pages        = result.get("classification", {}).get("total_pages"),
            invoice            = invoice,
            line_items         = line_items,
            avg_ocr_confidence = round(avg_ocr, 2) if avg_ocr is not None else None,
            composite_score    = conf.get("composite_score"),
            confidence_tier    = conf.get("tier"),
            action             = conf.get("action"),
            validation_errors  = result.get("validation_errors") or [],
            format_warnings    = invoice.get_format_warnings() if invoice else [],
            queued_for_review  = result.get("queued_for_review", False),
            review_reasons     = result.get("review_reasons") or [],
        )

    model_config = {"populate_by_name": True}
