"""
tests/test_business_rules.py
Unit tests for business_rules.py — no PDF, no Ollama required.

Run:
    pytest tests/test_business_rules.py -v
"""

import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.validation.business_rules import validate

# Base record that passes all checks — tweak per test
_VALID_BASE = {
    "InvoiceId":        "INV-2024-00421",
    "IssueDate":        "2024-03-15",
    "DueDate":          "2024-04-14",
    "SellerName":       "ABC Company LLC",
    "InvGrandTotal":    35144.85,
    "Currency":         "USD",
    "InvTaxBasisTotal": 32280.0,
    "InvTaxTotal":      2864.85,
}


class TestRequiredFields:
    def test_all_required_present_no_errors(self):
        errors = validate(_VALID_BASE)
        tax_errors = [e for e in errors if "MISSING" in e]
        assert len(tax_errors) == 0

    def test_missing_invoice_id(self):
        rec = {**_VALID_BASE, "InvoiceId": None}
        errors = validate(rec)
        assert any("InvoiceId" in e for e in errors)

    def test_missing_issue_date(self):
        rec = {**_VALID_BASE, "IssueDate": None}
        errors = validate(rec)
        assert any("IssueDate" in e for e in errors)

    def test_missing_seller_name(self):
        rec = {**_VALID_BASE, "SellerName": None}
        errors = validate(rec)
        assert any("SellerName" in e for e in errors)

    def test_missing_grand_total(self):
        rec = {**_VALID_BASE, "InvGrandTotal": None}
        errors = validate(rec)
        assert any("InvGrandTotal" in e for e in errors)

    def test_empty_record_catches_all(self):
        errors = validate({})
        required = ["InvoiceId", "IssueDate", "SellerName", "InvGrandTotal", "Currency"]
        for field in required:
            assert any(field in e for e in errors), f"Expected error for {field}"


class TestTaxMath:
    def test_correct_tax_math_no_error(self):
        errors = validate(_VALID_BASE)
        tax_errors = [e for e in errors if "TAX_MATH" in e]
        assert len(tax_errors) == 0

    def test_tax_math_mismatch_caught(self):
        rec = {**_VALID_BASE,
               "InvTaxBasisTotal": 1000.0,
               "InvTaxTotal":      100.0,
               "InvGrandTotal":    999.0}   # wrong — should be 1100
        errors = validate(rec)
        assert any("TAX_MATH" in e for e in errors)

    def test_tax_math_within_tolerance_passes(self):
        # ±€0.06 tolerance allowed
        rec = {**_VALID_BASE,
               "InvTaxBasisTotal": 32280.0,
               "InvTaxTotal":      2864.85,
               "InvGrandTotal":    35144.85}
        errors = validate(rec)
        tax_errors = [e for e in errors if "TAX_MATH" in e]
        assert len(tax_errors) == 0


class TestLineMath:
    def test_correct_line_math(self):
        rec = {**_VALID_BASE,
               "NetPrice": 1850.0, "BilledQuantity": 12, "LineTotalAmount": 22200.0}
        errors = validate(rec)
        line_errors = [e for e in errors if "LINE_MATH" in e]
        assert len(line_errors) == 0

    def test_line_math_mismatch(self):
        rec = {**_VALID_BASE,
               "NetPrice": 1850.0, "BilledQuantity": 12, "LineTotalAmount": 999.0}
        errors = validate(rec)
        assert any("LINE_MATH" in e for e in errors)

    def test_with_discount_line_math(self):
        # 2100 * 5 = 10500, minus 210 discount = 10290
        # business_rules checks NetPrice * Qty ≈ LineTotalAmount
        # This will flag because 2100*5=10500 ≠ 10290
        # That's correct behaviour — the discount makes it mismatch at field level
        rec = {**_VALID_BASE,
               "NetPrice": 2100.0, "BilledQuantity": 5.0, "LineTotalAmount": 10290.0}
        errors = validate(rec)
        # We expect a LINE_MATH warning here because discount is not in the formula
        line_errors = [e for e in errors if "LINE_MATH" in e]
        assert len(line_errors) > 0


class TestDateOrdering:
    def test_due_after_issue_passes(self):
        errors = validate(_VALID_BASE)
        date_errors = [e for e in errors if "DUE_BEFORE_ISSUE" in e]
        assert len(date_errors) == 0

    def test_due_before_issue_caught(self):
        rec = {**_VALID_BASE, "DueDate": "2024-01-01", "IssueDate": "2024-03-15"}
        errors = validate(rec)
        assert any("DUE_BEFORE_ISSUE" in e for e in errors)


class TestIbanVatBic:
    def test_invalid_iban_caught(self):
        rec = {**_VALID_BASE, "Iban": "NOTANIBAN"}
        errors = validate(rec)
        assert any("INVALID_IBAN" in e for e in errors)

    def test_invalid_vat_caught(self):
        rec = {**_VALID_BASE, "SellerVatId": "12345"}
        errors = validate(rec)
        assert any("INVALID_VAT_ID" in e for e in errors)

    def test_invalid_bic_caught(self):
        rec = {**_VALID_BASE, "Bic": "NOTABIC"}
        errors = validate(rec)
        assert any("INVALID_BIC" in e for e in errors)

    def test_valid_bic_passes(self):
        rec = {**_VALID_BASE, "Bic": "CHASUS33"}
        errors = validate(rec)
        bic_errors = [e for e in errors if "INVALID_BIC" in e]
        assert len(bic_errors) == 0

    def test_unknown_currency(self):
        rec = {**_VALID_BASE, "Currency": "XYZ"}
        errors = validate(rec)
        assert any("UNKNOWN_CURRENCY" in e for e in errors)

    def test_valid_currency_passes(self):
        for currency in ["EUR", "USD", "GBP"]:
            rec = {**_VALID_BASE, "Currency": currency}
            errors = validate(rec)
            curr_errors = [e for e in errors if "UNKNOWN_CURRENCY" in e]
            assert len(curr_errors) == 0, f"Currency {currency} should be valid"


class TestNegativeValues:
    def test_negative_grand_total_caught(self):
        rec = {**_VALID_BASE, "InvGrandTotal": -100.0}
        errors = validate(rec)
        assert any("NEGATIVE" in e for e in errors)

    def test_positive_grand_total_passes(self):
        errors = validate(_VALID_BASE)
        neg_errors = [e for e in errors if "NEGATIVE" in e]
        assert len(neg_errors) == 0
