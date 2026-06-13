"""
src/extraction/extractor.py
Top-level extraction orchestrator for one PDF page.

For digital PDFs:
  PyMuPDF text → sanitize → LLM extract

For scanned PDFs:
  render image → preprocess (incl. colour overlay suppression) →
  OCR → confidence split → zone-map → sanitize → LLM extract

Key fix: colour overlay suppression in preprocess_image now handles
the diagonal stamp problem that caused 52% OCR confidence on invoices
02 and 03.
"""

import io
import json
import pymupdf
from PIL import Image

from src.ocr.tesseract_runner import run_ocr
from src.ocr.confidence import split_by_confidence
from src.ocr.bbox_mapper import group_words_by_zone, zone_text
from src.preprocessing.image_cleanup import preprocess_image
from src.preprocessing.language_detector import detect_language
from src.guardrails.input_sanitizer import sanitize
from src.guardrails.output_filter import parse_and_filter
from src.extraction.prompt_builder import build_extraction_prompt
from src.extraction.model_client_ollama import call_ollama
from src.observability.logger import get_logger
from src.observability.metrics import metrics

log = get_logger(__name__)


def render_page_to_image(pdf_path: str, page_index: int, dpi: int = 300) -> Image.Image:
    doc = pymupdf.open(pdf_path)
    page = doc[page_index]
    scale = dpi / 72
    mat = pymupdf.Matrix(scale, scale)
    pix = page.get_pixmap(matrix=mat)
    doc.close()
    return Image.open(io.BytesIO(pix.tobytes("png")))


# ─────────────────────────────────────────────────────────────
#  Digital page extraction
# ─────────────────────────────────────────────────────────────

def extract_digital_page(pdf_path: str, page_index: int, cfg: dict) -> dict:
    doc = pymupdf.open(pdf_path)
    page = doc[page_index]
    raw_text = page.get_text("text").strip()
    doc.close()

    lang = detect_language(raw_text)
    cleaned_text, san_warnings = sanitize(raw_text, source=f"digital_p{page_index+1}")

    zone_texts = {
        "header":  cleaned_text[:1500],
        "seller":  cleaned_text[:1500],
        "buyer":   cleaned_text[:1500],
        "items":   cleaned_text[500:3000],
        "totals":  cleaned_text[-1500:],
        "payment": cleaned_text[-1000:],
    }

    sys_prompt, user_prompt = build_extraction_prompt(
        zone_texts=zone_texts,
        language=lang,
        noise_hints=san_warnings,
    )

    raw_response, llm_meta = call_ollama(
        system_prompt=sys_prompt,
        user_prompt=user_prompt,
        base_url=cfg["ollama_base_url"],
        model=cfg["ollama_model"],
        temperature=cfg["ollama_temperature"],
        timeout=cfg["ollama_timeout"],
    )

    parsed, filter_issues = parse_and_filter(raw_response or "")

    if not parsed:
        log.warning("extraction_empty",
                    method="digital", page=page_index+1,
                    llm_error=llm_meta.get("error"),
                    response_chars=len(raw_response or ""))

    return {
        "page_number":        page_index + 1,
        "extraction_method":  "digital",
        "raw_text":           raw_text,
        "language":           lang,
        "avg_confidence":     100.0,
        "sanitizer_warnings": san_warnings,
        "llm_meta":           llm_meta,
        "llm_raw_response":   raw_response,
        "filter_issues":      filter_issues,
        "extracted":          parsed or {},
    }


# ─────────────────────────────────────────────────────────────
#  Scanned page extraction
# ─────────────────────────────────────────────────────────────

def extract_scanned_page(pdf_path: str, page_index: int, cfg: dict) -> dict:
    metrics.inc("ocr_pages_total")

    # 1. Render page to image
    img = render_page_to_image(pdf_path, page_index, dpi=cfg["dpi"])
    page_width, page_height = img.size

    # 2. Preprocess — includes colour overlay suppression
    #    This is the fix for invoices with diagonal stamp annotations
    img = preprocess_image(img, dpi=cfg["dpi"])

    # 3. OCR — after preprocessing, confidence should be significantly higher
    ocr_result = run_ocr(
        image=img,
        page_number=page_index + 1,
        psm=cfg["tesseract_psm"],
        lang=cfg.get("tesseract_lang", "deu+eng"),
    )
    metrics.record("ocr_avg_confidence", ocr_result.avg_confidence)

    log.info("ocr_post_preprocess",
             page=page_index+1,
             avg_conf=ocr_result.avg_confidence,
             words=len(ocr_result.words))

    # 4. Confidence split
    conf_split = split_by_confidence(ocr_result, cfg["confidence_threshold"])

    # 5. Zone mapping using bounding boxes
    groups = group_words_by_zone(ocr_result.words, page_width, page_height)
    zones  = zone_text(groups)

    # 6. Language detection
    lang = detect_language(conf_split["high_conf_text"])

    # 7. Input sanitization + injection guard
    cleaned_text, san_warnings = sanitize(
        ocr_result.full_text,
        source=f"scanned_p{page_index+1}"
    )
    if conf_split["corruption_hints"]:
        san_warnings.extend(conf_split["corruption_hints"])

    # 8. Build prompt
    sys_prompt, user_prompt = build_extraction_prompt(
        zone_texts=zones,
        language=lang,
        noise_hints=san_warnings,
    )

    log.info("sending_to_llm",
             page=page_index+1,
             ocr_conf=ocr_result.avg_confidence,
             text_chars=len(cleaned_text),
             low_conf_words=len(conf_split["low_conf_words"]))

    # 9. LLM call
    raw_response, llm_meta = call_ollama(
        system_prompt=sys_prompt,
        user_prompt=user_prompt,
        base_url=cfg["ollama_base_url"],
        model=cfg["ollama_model"],
        temperature=cfg["ollama_temperature"],
        timeout=cfg["ollama_timeout"],
    )

    # 10. Parse and filter output
    parsed, filter_issues = parse_and_filter(raw_response or "")

    if not parsed:
        log.error("extraction_empty",
                  method="scanned_ocr", page=page_index+1,
                  llm_error=llm_meta.get("error"),
                  response_chars=len(raw_response or ""),
                  ocr_conf=ocr_result.avg_confidence,
                  hint="Check OCR quality and overlay suppression")

    return {
        "page_number":         page_index + 1,
        "extraction_method":   "scanned_ocr",
        "raw_text":            ocr_result.full_text,
        "language":            lang,
        "avg_confidence":      ocr_result.avg_confidence,
        "low_conf_word_count": len(conf_split["low_conf_words"]),
        "corruption_hints":    conf_split["corruption_hints"],
        "sanitizer_warnings":  san_warnings,
        "llm_meta":            llm_meta,
        "llm_raw_response":    raw_response,
        "filter_issues":       filter_issues,
        "extracted":           parsed or {},
    }
