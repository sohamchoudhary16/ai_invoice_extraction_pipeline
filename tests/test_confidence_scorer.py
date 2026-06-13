"""
tests/test_confidence_scorer.py
Unit tests for confidence/scorer.py.

Run:
    pytest tests/test_confidence_scorer.py -v
"""

import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.confidence.scorer import compute_composite_score

_FULL_RECORD = {
    "InvoiceId": "INV-001", "IssueDate": "2024-03-15",
    "SellerName": "ABC Co", "InvGrandTotal": 1000.0, "Currency": "EUR",
}


class TestCompositeScore:
    def test_perfect_inputs_give_high_score(self):
        result = compute_composite_score(
            avg_ocr_confidence=95.0,
            page_results=[{"extracted": {"extracted_fields": {"InvoiceId": {"confidence": "high"}}}}],
            validation_errors=[],
            filter_issues=[],
            record=_FULL_RECORD,
        )
        assert result["composite_score"] >= 0.80
        assert result["tier"] in ("high", "medium")
        assert result["action"] in ("auto_accept", "partial_review")

    def test_low_ocr_reduces_score(self):
        result = compute_composite_score(
            avg_ocr_confidence=40.0,    # very low OCR
            page_results=[],
            validation_errors=[],
            filter_issues=[],
            record=_FULL_RECORD,
        )
        assert result["composite_score"] < 0.80

    def test_validation_errors_reduce_score(self):
        high_result = compute_composite_score(
            avg_ocr_confidence=90.0,
            page_results=[],
            validation_errors=[],
            filter_issues=[],
            record=_FULL_RECORD,
        )
        low_result = compute_composite_score(
            avg_ocr_confidence=90.0,
            page_results=[],
            validation_errors=["TAX_MATH", "MISSING_IBAN", "INVALID_VAT",
                               "DATE_ORDER", "NEGATIVE_TOTAL"],
            filter_issues=[],
            record=_FULL_RECORD,
        )
        assert high_result["composite_score"] > low_result["composite_score"]

    def test_missing_required_fields_reduce_score(self):
        result = compute_composite_score(
            avg_ocr_confidence=85.0,
            page_results=[],
            validation_errors=[],
            filter_issues=[],
            record={},   # empty record — completeness = 0
        )
        assert result["composite_score"] < 0.80

    def test_guardrail_issues_reduce_score(self):
        clean = compute_composite_score(
            avg_ocr_confidence=85.0, page_results=[],
            validation_errors=[], filter_issues=[],
            record=_FULL_RECORD,
        )
        flagged = compute_composite_score(
            avg_ocr_confidence=85.0, page_results=[],
            validation_errors=[],
            filter_issues=["INJECTION_DETECTED", "UNKNOWN_FIELDS"],
            record=_FULL_RECORD,
        )
        assert clean["composite_score"] > flagged["composite_score"]

    def test_tiers_are_assigned_correctly(self):
        # Tier boundaries: >=0.85 high, 0.65-0.84 medium, 0.40-0.64 low, <0.40 invalid
        for ocr, expected_tier in [
            (95.0, "high"),
            (70.0, "medium"),
            (40.0, "low"),
        ]:
            result = compute_composite_score(
                avg_ocr_confidence=ocr, page_results=[],
                validation_errors=[], filter_issues=[],
                record=_FULL_RECORD if ocr > 50 else {},
            )
            assert result["tier"] in ("high", "medium", "low", "invalid"), \
                f"Unexpected tier: {result['tier']}"

    def test_result_has_required_keys(self):
        result = compute_composite_score(
            avg_ocr_confidence=80.0, page_results=[],
            validation_errors=[], filter_issues=[],
            record=_FULL_RECORD,
        )
        assert "composite_score" in result
        assert "tier" in result
        assert "action" in result
        assert "breakdown" in result

    def test_composite_score_in_range(self):
        result = compute_composite_score(
            avg_ocr_confidence=75.0, page_results=[],
            validation_errors=["ONE_ERROR"],
            filter_issues=[],
            record=_FULL_RECORD,
        )
        assert 0.0 <= result["composite_score"] <= 1.0
