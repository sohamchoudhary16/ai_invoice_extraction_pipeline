"""
src/extraction/vision_fallback.py
ADDITIVE ONLY — existing pipeline is NOT changed.

After the main pipeline finishes, any document with composite
confidence < VISION_THRESHOLD is re-processed by sending the raw
PDF page as a base64 image directly to a vision-capable LLM.

This produces a SECOND output set for comparison:
  output/processed/<run_ts>/vision/invoices.json
  output/processed/<run_ts>/vision/line_items.json
  output/processed/<run_ts>/vision/invoices.csv
  output/processed/<run_ts>/vision/line_items.csv
  output/processed/<run_ts>/vision/comparison.json  ← side-by-side diff

The comparison.json shows field-by-field where OCR+LLM and Vision
agreed and disagreed — this is the data you need to decide which
approach is better for your invoices.

Requirements:
  - Ollama must be running the same endpoint
  - A vision-capable model must be available: gemma3:4b is vision-capable
  - The same OLLAMA_BASE_URL is used (no separate config needed)
"""

import io
import os
import json
import base64
import requests
import pymupdf
from PIL import Image
from src.observability.logger import get_logger

log = get_logger(__name__)

# Documents below this composite score trigger vision fallback
VISION_THRESHOLD = 0.80

# Vision extraction prompt — minimal, direct, image-aware
_VISION_SYSTEM = """\
You are an invoice data extraction engine.
You are looking at an image of an invoice page.
Rules:
1. Return ONLY a valid JSON object. No markdown, no explanation, no backticks.
2. If a field is not visible in the image set it to null. Never guess.
3. Ignore stamps, signatures, diagonal overlays, and handwritten annotations.
4. Dates must be YYYY-MM-DD. Numbers: European 1.234,56 means 1234.56.
5. line_items is an array — one object per line item.
"""

_VISION_PROMPT = """\
Extract all invoice fields visible in this image.
Return ONLY the JSON object below with values filled in.

{
  "InvoiceId": null,
  "InvoiceTypeCode": null,
  "IssueDate": null,
  "DueDate": null,
  "Currency": null,
  "BuyerReference": null,
  "SellerName": null,
  "SellerContactName": null,
  "SellerPhone": null,
  "SellerEmail": null,
  "SellerStreet": null,
  "SellerPostcode": null,
  "SellerCity": null,
  "SellerCountry": null,
  "SellerVatId": null,
  "BuyerId": null,
  "BuyerName": null,
  "BuyerStreet": null,
  "BuyerPostcode": null,
  "BuyerCity": null,
  "BuyerCountry": null,
  "BuyerEmail": null,
  "ShipToName": null,
  "ShipToStreet": null,
  "ShipToPostcode": null,
  "ShipToCity": null,
  "ShipToCountry": null,
  "InvLineTotal": null,
  "InvAllowanceTotal": null,
  "InvTaxBasisTotal": null,
  "InvTaxTotal": null,
  "InvGrandTotal": null,
  "InvDuePayable": null,
  "PaymentMeansCode": null,
  "Iban": null,
  "BankAccountName": null,
  "Bic": null,
  "PaymentTerms": null,
  "line_items": [
    {
      "LineId": null,
      "ProductName": null,
      "ProductDescription": null,
      "BilledQuantity": null,
      "BilledUnit": null,
      "NetPrice": null,
      "DiscountAmount": null,
      "LineTotalAmount": null,
      "TaxRatePercent": null,
      "DiscountReason": null
    }
  ]
}
"""


def _render_pdf_to_base64(pdf_path: str, dpi: int = 150) -> list[str]:
    """
    Render each page of a PDF to a base64-encoded PNG string.
    150 DPI is sufficient for vision models — lower than OCR's 300 DPI
    because the model reads the full image, not individual characters.
    Returns list of base64 strings, one per page.
    """
    doc = pymupdf.open(pdf_path)
    pages_b64 = []
    scale = dpi / 72
    mat = pymupdf.Matrix(scale, scale)
    for page in doc:
        pix = page.get_pixmap(matrix=mat)
        img_bytes = pix.tobytes("png")
        b64 = base64.b64encode(img_bytes).decode("utf-8")
        pages_b64.append(b64)
    doc.close()
    return pages_b64


def _call_vision_llm(
    image_b64: str,
    base_url: str,
    model: str,
    timeout: int,
) -> dict:
    """
    Send a single page image to Ollama vision model.
    Returns parsed JSON dict or empty dict on failure.
    """
    url = f"{base_url.rstrip('/')}/api/chat"
    payload = {
        "model": model,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0, "seed": 42},
        "messages": [
            {
                "role": "system",
                "content": _VISION_SYSTEM,
            },
            {
                "role": "user",
                "content": _VISION_PROMPT,
                "images": [image_b64],   # Ollama vision API format
            },
        ],
    }

    try:
        resp = requests.post(url, json=payload, timeout=timeout)
        resp.raise_for_status()
        raw = resp.json().get("message", {}).get("content", "")

        # Strip markdown fences if model adds them
        clean = raw.strip()
        for fence in ["```json", "```"]:
            clean = clean.replace(fence, "")
        clean = clean.strip()

        return json.loads(clean)

    except json.JSONDecodeError as e:
        log.warning("vision_json_parse_failed", error=str(e), raw_chars=len(raw or ""))
        return {}
    except Exception as e:
        log.error("vision_llm_failed", error=str(e))
        return {}


def _compare_records(ocr_record: dict, vision_record: dict) -> dict:
    """
    Field-by-field comparison of OCR+LLM vs Vision extraction.
    Returns comparison dict with agree/disagree/missing per field.
    """
    all_keys = set(ocr_record.keys()) | set(vision_record.keys())
    skip = {"_confidence_score", "_confidence_tier", "_action",
            "_is_valid", "_validation_errors", "_conflicts",
            "_is_duplicate", "_line_items_raw", "_field_metadata",
            "FileHash", "RunTimestamp", "ExtractionMethod"}

    comparison = {
        "agreed": {},
        "disagreed": {},
        "ocr_only": {},
        "vision_only": {},
    }

    for key in sorted(all_keys):
        if key in skip:
            continue
        ocr_val    = ocr_record.get(key)
        vision_val = vision_record.get(key)

        if ocr_val is None and vision_val is None:
            continue
        elif ocr_val == vision_val:
            comparison["agreed"][key] = ocr_val
        elif ocr_val is not None and vision_val is None:
            comparison["ocr_only"][key] = ocr_val
        elif ocr_val is None and vision_val is not None:
            comparison["vision_only"][key] = vision_val
        else:
            comparison["disagreed"][key] = {
                "ocr_llm": ocr_val,
                "vision":  vision_val,
            }

    return comparison


def run_vision_fallback(
    low_confidence_results: list[dict],
    all_flat_records: list[dict],
    cfg: dict,
    run_ts: str,
    run_dir: str,
) -> None:
    """
    Entry point called from main.py after primary pipeline completes.

    Parameters
    ----------
    low_confidence_results : list of result dicts from run_pipeline()
                             where composite_score < VISION_THRESHOLD
    all_flat_records       : the primary pipeline's flat records (for comparison)
    cfg                    : pipeline config dict
    run_ts                 : run timestamp string
    run_dir                : timestamped output directory path
    """
    if not low_confidence_results:
        log.info("vision_fallback_skipped",
                 reason="no documents below confidence threshold",
                 threshold=VISION_THRESHOLD)
        return

    vision_model = cfg.get("vision_model", cfg["ollama_model"])
    log.info("vision_fallback_start",
             documents=len(low_confidence_results),
             model=vision_model,
             threshold=VISION_THRESHOLD)

    vision_out_dir = os.path.join(run_dir, "vision")
    os.makedirs(vision_out_dir, exist_ok=True)

    vision_invoices   = []
    vision_line_items = []
    comparisons       = []

    for result in low_confidence_results:
        pdf_path    = result.get("_pdf_path", "")
        source_file = result.get("source_file", "")
        ocr_score   = result.get("confidence", {}).get("composite_score", 0)

        # Try absolute path first, then relative fallback
        if not pdf_path:
            log.error("vision_pdf_path_empty", file=source_file)
            continue

        # Resolve path — handle both absolute and relative
        resolved_path = pdf_path
        if not os.path.exists(resolved_path):
            # Try relative to cwd
            resolved_path = os.path.join(os.getcwd(), os.path.basename(pdf_path))
        if not os.path.exists(resolved_path):
            # Try in sample_pdf/scan
            resolved_path = os.path.join("sample_pdf", "scan", os.path.basename(pdf_path))
        if not os.path.exists(resolved_path):
            log.error("vision_pdf_not_found",
                      file=source_file,
                      tried=[pdf_path, resolved_path],
                      cwd=os.getcwd())
            continue
        pdf_path = resolved_path
        log.info("vision_pdf_resolved", file=source_file, path=pdf_path)

        log.info("vision_processing",
                 file=source_file,
                 ocr_confidence=ocr_score,
                 model=vision_model)

        try:
            # Render PDF pages to base64 images
            pages_b64 = _render_pdf_to_base64(pdf_path, dpi=150)

            # Extract via vision LLM (one call per page, merge results)
            merged_vision: dict = {}
            vision_line_items_raw = []

            for page_num, image_b64 in enumerate(pages_b64):
                log.info("vision_page_call",
                         file=source_file,
                         page=page_num + 1,
                         model=vision_model)

                extracted = _call_vision_llm(
                    image_b64=image_b64,
                    base_url=cfg["ollama_base_url"],
                    model=vision_model,
                    timeout=cfg["ollama_timeout"],
                )

                # Collect line items from this page
                items = extracted.pop("line_items", []) or []
                if isinstance(items, list):
                    vision_line_items_raw.extend(items)

                # Merge header fields — first non-null wins
                for field, val in extracted.items():
                    if val is not None and field not in merged_vision:
                        merged_vision[field] = val

            # Build vision invoice record
            vision_inv = {
                "SourceFile":        source_file,
                "ExtractionMethod":  f"vision_{vision_model}",
                "RunTimestamp":      run_ts,
                "VisionModel":       vision_model,
                "OcrCompositeScore": ocr_score,
                **merged_vision,
            }
            vision_invoices.append(vision_inv)

            # Build vision line item records
            for item in vision_line_items_raw:
                if isinstance(item, dict) and any(v is not None for v in item.values()):
                    vision_line_items.append({
                        "SourceFile": source_file,
                        "InvoiceId":  merged_vision.get("InvoiceId"),
                        **item,
                    })

            # Build comparison against OCR+LLM primary record
            primary_records = [r for r in all_flat_records
                               if r.get("SourceFile") == source_file]
            primary_record  = primary_records[0] if primary_records else {}

            comparison = {
                "source_file":         source_file,
                "ocr_composite_score": ocr_score,
                "ocr_llm_model":       cfg["ollama_model"],
                "vision_model":        vision_model,
                "field_comparison":    _compare_records(primary_record, vision_inv),
                "summary": {}
            }
            fc = comparison["field_comparison"]
            comparison["summary"] = {
                "agreed":      len(fc.get("agreed", {})),
                "disagreed":   len(fc.get("disagreed", {})),
                "ocr_only":    len(fc.get("ocr_only", {})),
                "vision_only": len(fc.get("vision_only", {})),
            }
            comparisons.append(comparison)

            log.info("vision_complete",
                     file=source_file,
                     vision_fields_extracted=len(merged_vision),
                     vision_line_items=len(vision_line_items_raw),
                     agreed=comparison["summary"]["agreed"],
                     disagreed=comparison["summary"]["disagreed"],
                     model=vision_model)

        except Exception as e:
            import traceback
            log.error("vision_doc_failed",
                      file=source_file,
                      error=str(e),
                      traceback=traceback.format_exc())
            continue

    # Write vision outputs
    _write_vision_json(vision_invoices,   os.path.join(vision_out_dir, "invoices.json"))
    _write_vision_json(vision_line_items, os.path.join(vision_out_dir, "line_items.json"))
    _write_vision_json(comparisons,       os.path.join(vision_out_dir, "comparison.json"))

    # Write CSVs
    _write_vision_csv(vision_invoices,
                      os.path.join(vision_out_dir, "invoices.csv"))
    _write_vision_csv(vision_line_items,
                      os.path.join(vision_out_dir, "line_items.csv"))

    log.info("vision_fallback_done",
             vision_invoices=len(vision_invoices),
             vision_line_items=len(vision_line_items),
             comparisons=len(comparisons),
             output_dir=vision_out_dir)

    print(f"\n🔍 Vision fallback complete [{vision_model}]")
    print(f"   Documents re-processed : {len(low_confidence_results)}")
    print(f"   Vision invoices        : {len(vision_invoices)}")
    print(f"   Vision line items      : {len(vision_line_items)}")
    print(f"   Comparison report      : {vision_out_dir}/comparison.json")


def _write_vision_json(records: list, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2, ensure_ascii=False, default=str)
    print(f"[Vision JSON] {len(records)} record(s) → {path}")


def _write_vision_csv(records: list, path: str) -> None:
    if not records:
        return
    import pandas as pd
    pd.DataFrame(records).to_csv(path, index=False, encoding="utf-8-sig")
    print(f"[Vision CSV] {len(records)} row(s) → {path}")
