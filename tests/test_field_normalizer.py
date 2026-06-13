"""
tests/test_field_normalizer.py
Unit tests for field_normalizer.py — pure deterministic logic,
no PDF, no Ollama, no Tesseract required.

Run:
    pytest tests/test_field_normalizer.py -v
"""

import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.validation.field_normalizer import parse_date, parse_float, normalize_iban, normalize_record


class TestParseDate:
    def test_iso_passthrough(self):
        assert parse_date("2024-03-15") == "2024-03-15"

    def test_german_format(self):
        assert parse_date("15.03.2024") == "2024-03-15"

    def test_slash_format(self):
        assert parse_date("15/03/2024") == "2024-03-15"

    def test_compact_format(self):
        assert parse_date("20240315") == "2024-03-15"

    def test_dash_dmy(self):
        assert parse_date("15-03-2024") == "2024-03-15"

    def test_none_returns_none(self):
        assert parse_date(None) is None

    def test_empty_string_returns_none(self):
        assert parse_date("") is None

    def test_unparseable_returns_as_is(self):
        # Don't lose unparseable values — return them unchanged
        result = parse_date("not-a-date")
        assert result == "not-a-date"


class TestParseFloat:
    def test_plain_float(self):
        assert parse_float("1234.56") == 1234.56

    def test_european_comma_decimal(self):
        assert parse_float("1234,56") == 1234.56

    def test_european_thousand_separator(self):
        assert parse_float("1.234,56") == 1234.56

    def test_us_thousand_separator(self):
        # "1,234.56" — comma as thousand sep
        assert parse_float("1,234.56") == 1234.56

    def test_euro_symbol_stripped(self):
        assert parse_float("€ 38.413,20") == 38413.20

    def test_dollar_symbol_stripped(self):
        assert parse_float("$35,144.85") == 35144.85

    def test_percent_stripped(self):
        assert parse_float("19%") == 19.0

    def test_tax_rate_decimal(self):
        assert parse_float("8.875") == 8.875

    def test_none_returns_none(self):
        assert parse_float(None) is None

    def test_zero(self):
        assert parse_float("0") == 0.0

    def test_integer_string(self):
        assert parse_float("1850") == 1850.0

    def test_large_european_number(self):
        # InvGrandTotal from invoice_v3_01
        assert parse_float("35.144,85") == 35144.85

    def test_negative_value(self):
        assert parse_float("-210.0") == -210.0


class TestNormaliseIban:
    def test_spaces_stripped(self):
        assert normalize_iban("DE89 3704 0044 0532 0130 00") == "DE89370400440532013000"

    def test_uppercased(self):
        assert normalize_iban("de89370400440532013000") == "DE89370400440532013000"

    def test_none_returns_none(self):
        assert normalize_iban(None) is None

    def test_empty_returns_none(self):
        assert normalize_iban("") is None


class TestNormalizeRecord:
    def test_date_fields_coerced(self):
        rec = {"IssueDate": "15.03.2024", "DueDate": "14.04.2024"}
        out = normalize_record(rec)
        assert out["IssueDate"] == "2024-03-15"
        assert out["DueDate"] == "2024-04-14"

    def test_float_fields_coerced(self):
        rec = {"InvGrandTotal": "35.144,85", "InvTaxTotal": "2.864,85"}
        out = normalize_record(rec)
        assert out["InvGrandTotal"] == 35144.85
        assert out["InvTaxTotal"] == 2864.85

    def test_iban_normalised(self):
        rec = {"Iban": "DE89 3704 0044 0532 0130 00"}
        out = normalize_record(rec)
        assert out["Iban"] == "DE89370400440532013000"

    def test_null_values_pass_through(self):
        rec = {"IssueDate": None, "InvGrandTotal": None, "Iban": None}
        out = normalize_record(rec)
        assert out["IssueDate"] is None
        assert out["InvGrandTotal"] is None

    def test_unknown_fields_preserved(self):
        rec = {"SomeUnknownField": "value", "InvGrandTotal": "100.0"}
        out = normalize_record(rec)
        assert out["SomeUnknownField"] == "value"

    def test_does_not_raise_on_garbage(self):
        # The normalizer must never crash the pipeline
        rec = {
            "IssueDate": "garbage-date",
            "InvGrandTotal": "not-a-number",
            "Iban": None,
        }
        out = normalize_record(rec)
        # Unparseable date comes back as-is
        assert out["IssueDate"] == "garbage-date"
        # Unparseable float comes back as None
        assert out["InvGrandTotal"] is None
