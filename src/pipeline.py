"""
src/pipeline.py  (fully rewritten)
Orchestrates the complete pipeline for ONE PDF:

  load → classify → [per-page: preprocess → extract] →
  merge → normalize → validate → score → route → output
"""

import os
import logging

from src.ingestion.loader import load_pdf
from src.ingestion.pdf_classifier import classify_pdf
from src.extraction.extractor import extract_digital_page, extract_scanned_page
from src.extraction.merger import merge_page_results
from src.validation.field_normalizer import normalize_record, normalize_line_items
from src.validation.business_rules import validate
from src.validation.duplicate_checker import check_duplicate
from src.confidence.scorer import compute_composite_score
from src.review.review_queue import queue_for_review
from src.observability.logger import get_logger
from src.observability.metrics import metrics

log = get_logger(__name__)


def run_pipeline(pdf_path: str, cfg: dict) -> dict:
    """
    Process one PDF end-to-end.

    Returns
    -------
    {
        "source_file": str,
        "ok": bool,
        "error": str | None,
        "classification": dict,
        "page_results": list[dict],
        "record": dict,
        "validation_errors": list[str],
        "confidence": dict,
        "queued_for_review": bool,
        "review_reasons": list[str],
    }
    """
    source_file = os.path.basename(pdf_path)
    metrics.inc("docs_attempted")
    log.info("pipeline_start", file=source_file)

    result = {
        "source_file": source_file,
        "ok": False,
        "error": None,
        "classification": {},
        "page_results": [],
        "record": {},
        "validation_errors": [],
        "confidence": {},
        "queued_for_review": False,
        "review_reasons": [],
    }

    try:
        # ── 1. Pre-flight ─────────────────────────────────────
        load_result = load_pdf(pdf_path)
        if not load_result.ok:
            result["error"] = load_result.error
            metrics.inc("docs_load_failed")
            log.error("load_failed", file=source_file, reason=load_result.error)
            return result

        # ── 2. Classify ───────────────────────────────────────
        classification = classify_pdf(pdf_path)
        result["classification"] = {
            "doc_kind": classification.doc_kind,
            "total_pages": classification.total_pages,
            "has_zugferd_xml": classification.has_zugferd_xml,
        }
        metrics.inc(f"docs_{classification.doc_kind}")

        # ── 3. Extract per page ───────────────────────────────
        page_results = []
        all_filter_issues = []

        for page_cls in classification.page_classes:
            page_index = page_cls.page_number - 1
            metrics.start_timer(f"page_{page_cls.page_number}")

            if page_cls.kind == "digital" and cfg.get("skip_digital_pdf_ocr", True):
                page_res = extract_digital_page(pdf_path, page_index, cfg)
            else:
                page_res = extract_scanned_page(pdf_path, page_index, cfg)

            metrics.stop_timer(f"page_{page_cls.page_number}")
            page_results.append(page_res)
            all_filter_issues.extend(page_res.get("filter_issues", []))

        result["page_results"] = page_results

        # ── 4. Merge pages → one record per line item ────────
        records = merge_page_results(page_results)
        for r in records:
            r["SourceFile"] = source_file

        # ── 5. Normalize each record ──────────────────────────
        records = [normalize_record(r) for r in records]

        # ── 6. Validate first record (header validation) ──────
        # Use first record for header-level validation and scoring
        primary = records[0]
        validation_errors = validate(primary)
        for r in records:
            r["_is_valid"] = len(validation_errors) == 0
            r["_validation_errors"] = str(validation_errors)
        result["validation_errors"] = validation_errors

        # ── 7. Duplicate check (on invoice ID) ───────────────
        is_dup = check_duplicate(primary, cfg.get("sqlite_db", "output/extractions.db"))
        for r in records:
            r["_is_duplicate"] = str(is_dup)
        if is_dup:
            validation_errors.append("DUPLICATE_INVOICE")

        # ── 8. Composite confidence scoring ───────────────────
        avg_ocr = sum(
            p.get("avg_confidence", 100) for p in page_results
        ) / max(len(page_results), 1)

        conf_result = compute_composite_score(
            avg_ocr_confidence=avg_ocr,
            page_results=page_results,
            validation_errors=validation_errors,
            filter_issues=all_filter_issues,
            record=primary,
        )
        for r in records:
            r["_confidence_score"] = str(conf_result["composite_score"])
            r["_confidence_tier"]  = conf_result["tier"]
            r["_action"]           = conf_result["action"]
        result["confidence"] = conf_result

        # ── 9. Routing ────────────────────────────────────────
        review_reasons = []
        action = conf_result["action"]

        if action in ("human_review", "reject"):
            review_reasons.append(f"confidence_action={action}")
        if validation_errors:
            review_reasons.append(f"validation_errors={len(validation_errors)}")
        if is_dup:
            review_reasons.append("duplicate_suspected")
        if all_filter_issues:
            review_reasons.append(f"guardrail_issues={len(all_filter_issues)}")

        if review_reasons:
            queue_for_review(pdf_path, primary, review_reasons, conf_result)
            result["queued_for_review"] = True
            result["review_reasons"] = review_reasons
            metrics.inc("docs_queued_review")

        # Pipeline returns list of records (one per line item)
        result["records"] = records
        result["record"]  = primary   # backward compat for main.py logging
        result["ok"] = True
        metrics.inc("docs_processed_ok")
        log.info("pipeline_done",
                 file=source_file,
                 action=action,
                 valid=primary["_is_valid"],
                 review=bool(review_reasons))

    except Exception as e:
        result["error"] = str(e)
        metrics.inc("docs_pipeline_error")
        log.exception("pipeline_error", file=source_file, error=str(e))

    return result
