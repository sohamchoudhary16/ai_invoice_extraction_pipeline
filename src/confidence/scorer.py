"""
src/confidence/scorer.py
Composite confidence scoring for a fully extracted + validated record.

Inputs to composite score:
  1. avg OCR confidence across pages
  2. field extraction confidence (from LLM per-field)
  3. validation pass rate (% of checks that passed)
  4. required field completeness
  5. filter/guardrail issues count

Output:
  composite_score  : 0.0 – 1.0
  tier             : "high" | "medium" | "low" | "invalid"
  action           : "auto_accept" | "partial_review" | "human_review" | "reject"
"""

from src.observability.logger import get_logger

log = get_logger(__name__)

# Required fields for completeness score
_REQUIRED = ["InvoiceId", "IssueDate", "SellerName", "InvGrandTotal", "Currency"]


def compute_composite_score(
    avg_ocr_confidence: float,           # 0–100
    page_results: list[dict],            # raw page extraction results
    validation_errors: list[str],        # from validators.py
    filter_issues: list[str],            # from output_filter.py
    record: dict,                        # normalized flat record
) -> dict:
    """
    Compute composite confidence score and routing decision.

    Returns
    -------
    {
        "composite_score": float,
        "tier": str,
        "action": str,
        "breakdown": dict
    }
    """
    scores = {}

    # 1. OCR confidence component (0–1)
    scores["ocr"] = min(avg_ocr_confidence, 100) / 100

    # 2. Field-level confidence (average across extracted fields)
    conf_vals = []
    for page in page_results:
        fields = page.get("extracted", {}).get("extracted_fields", {}) or {}
        for field_data in fields.values():
            if isinstance(field_data, dict):
                c = field_data.get("confidence", "low")
                conf_vals.append({"high": 1.0, "medium": 0.6, "low": 0.3}.get(c, 0.3))
    scores["field_confidence"] = sum(conf_vals) / len(conf_vals) if conf_vals else 0.0

    # 3. Validation pass rate
    # We penalise by number of errors relative to a baseline of 10 checks
    error_count = len(validation_errors)
    scores["validation"] = max(0.0, 1.0 - (error_count / 10))

    # 4. Required field completeness
    present = sum(1 for f in _REQUIRED if record.get(f))
    scores["completeness"] = present / len(_REQUIRED)

    # 5. Guardrail penalty
    issue_count = len(filter_issues)
    scores["guardrails"] = max(0.0, 1.0 - (issue_count * 0.2))

    # Weighted composite
    weights = {
        "ocr": 0.20,
        "field_confidence": 0.30,
        "validation": 0.25,
        "completeness": 0.15,
        "guardrails": 0.10,
    }
    composite = sum(scores[k] * weights[k] for k in weights)

    # Tier + action
    if composite >= 0.85:
        tier, action = "high",    "auto_accept"
    elif composite >= 0.65:
        tier, action = "medium",  "partial_review"
    elif composite >= 0.40:
        tier, action = "low",     "human_review"
    else:
        tier, action = "invalid", "reject"

    log.info("confidence_scored",
             composite=round(composite, 3),
             tier=tier, action=action,
             breakdown=scores)

    return {
        "composite_score": round(composite, 4),
        "tier": tier,
        "action": action,
        "breakdown": {k: round(v, 3) for k, v in scores.items()},
    }
