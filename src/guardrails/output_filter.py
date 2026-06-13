"""
src/guardrails/output_filter.py
Post-LLM output guardrails.

Fixes applied:
  - line_items whitelisted — it is a valid nested structure, not a hallucination
  - String "None" / "null" / "" treated as null for evidence checks
  - Evidence check only fires when value is genuinely present (not null/None)
  - extracted_fields key is optional — lean prompt returns flat JSON directly
"""

import json
import re
from src.models.schema import COLUMNS
from src.observability.logger import get_logger

log = get_logger(__name__)

_SCHEMA_FIELDS_LOWER = {c.lower() for c in COLUMNS}

# These are valid nested/structural keys — never flag as hallucinated
_WHITELISTED_KEYS = {"line_items", "extracted_fields", "_extraction_notes"}

# Fields that should have source_text evidence when their value is real
_EVIDENCE_REQUIRED = {
    "invgrandtotal", "iban", "sellervatid", "invoiceid", "issuedate"
}

# Values that mean "not found" — treat as null
_NULL_SENTINELS = {None, "null", "none", "n/a", "na", "", "unknown"}


def _is_null_value(val) -> bool:
    """Return True if the value is effectively null/missing."""
    if val is None:
        return True
    return str(val).strip().lower() in _NULL_SENTINELS


def clean_llm_response(raw: str) -> str:
    """Strip markdown fences and common LLM formatting noise."""
    cleaned = raw.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"\s*```$",           "", cleaned, flags=re.MULTILINE)
    # Trailing commas before } or ] — common small-model mistake
    cleaned = re.sub(r",\s*([}\]])", r"\1", cleaned)
    return cleaned.strip()


def parse_and_filter(raw_response: str) -> tuple[dict | None, list[str]]:
    """
    Parse LLM JSON output and apply output guardrails.

    Handles two response shapes:
      1. Lean prompt → flat JSON:  {"InvoiceId": "INV-001", ...}
      2. Full prompt → nested:     {"extracted_fields": {"InvoiceId": {...}}}

    Returns (parsed_dict_or_None, issues_list)
    """
    issues: list[str] = []

    if not raw_response or not raw_response.strip():
        issues.append("EMPTY_RESPONSE")
        log.warning("llm_empty_response")
        return None, issues

    cleaned = clean_llm_response(raw_response)

    # 1. JSON parse
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as e:
        issues.append(f"INVALID_JSON: {e}")
        log.warning("llm_json_parse_failed", error=str(e))
        return None, issues

    if not isinstance(parsed, dict):
        issues.append(f"NOT_A_DICT: got {type(parsed).__name__}")
        return None, issues

    # 2. Normalise shape — work with whichever layer has the actual fields
    # If response has "extracted_fields" wrapper use that, otherwise use root
    fields_to_check = parsed.get("extracted_fields", parsed)

    # 3. Evidence check — only for critical fields with real (non-null) values
    for field_key, field_val in fields_to_check.items():
        if field_key.lower() not in _EVIDENCE_REQUIRED:
            continue
        if isinstance(field_val, dict):
            value = field_val.get("value")
            source = field_val.get("source_text", "")
            if not _is_null_value(value) and not source:
                issues.append(
                    f"MISSING_EVIDENCE: {field_key} = '{value}' has no source_text"
                )
        # Flat response (no source_text key at all) — skip evidence check,
        # flat prompt intentionally omits source_text to save tokens

    # 4. Flag genuinely unknown fields (not in schema and not whitelisted)
    unknown = [
        k for k in fields_to_check
        if k.lower() not in _SCHEMA_FIELDS_LOWER
        and k.lower() not in _WHITELISTED_KEYS
    ]
    if unknown:
        issues.append(f"UNKNOWN_FIELDS: {unknown}")
        log.warning("llm_unknown_fields", fields=unknown)

    if issues:
        log.warning("output_filter_issues", count=len(issues), issues=issues)
    else:
        log.debug("output_filter_clean")

    return parsed, issues
