"""
src/main.py
Entry point. Loads config, discovers PDFs, runs the pipeline on each,
writes merged outputs.

Outputs:
  output/processed/file.json
  output/processed/file.csv
  output/processed/file.sql
  output/extractions.db
  output/run_summary.json
  output/metrics.json
  output/review_queue/
  logs/logs.txt
"""

import os
import json
import logging
import yaml
import pandas as pd
import pytesseract
from dotenv import load_dotenv
from tqdm import tqdm

from src.pipeline import run_pipeline
from src.storage.json_writer import write_json
from src.storage.sqlite_writer import write_sqlite
from src.models.schema import COLUMNS
from src.observability.logger import setup_logging, get_logger
from src.observability.metrics import metrics

load_dotenv()


# ─────────────────────────────────────────────────────────────
#  Config loader
# ─────────────────────────────────────────────────────────────

def _load_config() -> dict:
    # config_path = os.path.join(os.path.dirname(__file__), "..", "configs", "config.yaml")
    config_path = os.path.join(os.getcwd(), "configs", "config.yaml")
    with open(config_path, "r") as f:
        y = yaml.safe_load(f)

    return {
        "confidence_threshold": int(os.getenv("CONFIDENCE_THRESHOLD",
                                               y["ocr"]["confidence_threshold"])),
        "dpi":              int(y["ocr"]["dpi"]),
        "tesseract_psm":    int(y["ocr"]["tesseract_psm"]),
        "tesseract_lang":   y["ocr"].get("lang", "eng"),

        "ollama_base_url":  os.getenv("OLLAMA_BASE_URL",  y["ollama"]["base_url"]),
        "ollama_model":     os.getenv("OLLAMA_MODEL",     y["ollama"]["model"]),
        "ollama_temperature": float(y["ollama"]["temperature"]),
        "ollama_timeout":   int(os.getenv("OLLAMA_TIMEOUT", y["ollama"]["timeout"])),

        "skip_digital_pdf_ocr": y["pipeline"]["skip_digital_pdf_ocr"],
        "max_pages":        y["pipeline"]["max_pages_per_doc"],

        "input_dir":  os.getenv("INPUT_DIR",  y.get("input_dir",  "sample_pdf/scan")),
        "output_dir": os.getenv("OUTPUT_DIR", "output"),
        "log_file":   os.getenv("LOG_FILE",   y["logging"]["log_file"]),
        "log_level":  y["logging"]["level"],

        "tesseract_path": os.getenv(
            "TESSERACT_PATH",
            r"D:\tesseract\tesseract.exe"
        ),
        "json_indent": y["output"]["json_indent"],
        "sqlite_db":   y["output"]["sqlite_db"],
    }


def _set_tesseract(path: str) -> None:
    if os.path.exists(path):
        pytesseract.pytesseract.tesseract_cmd = path
        print(f"Tesseract set: {path}")
    else:
        print(f"WARNING: Tesseract NOT found at '{path}'. Check TESSERACT_PATH in .env")


def _discover_pdfs(input_dir: str) -> list[str]:
    if not os.path.isdir(input_dir):
        print(f"WARNING: Input dir not found: '{input_dir}'")
        return []
    pdfs = [
        os.path.join(input_dir, f)
        for f in sorted(os.listdir(input_dir))
        if f.lower().endswith(".pdf")
    ]
    print(f"Found {len(pdfs)} PDF(s) in '{input_dir}'")
    return pdfs


# ─────────────────────────────────────────────────────────────
#  Writers
# ─────────────────────────────────────────────────────────────

def _write_csv(records: list[dict], path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    rows = [{col: r.get(col) for col in COLUMNS} for r in records]
    df = pd.DataFrame(rows, columns=COLUMNS)
    df.to_csv(path, index=False, encoding="utf-8-sig")
    print(f"[CSV] Written {len(rows)} row(s) → {path}")


# ─────────────────────────────────────────────────────────────
#  Main
# ─────────────────────────────────────────────────────────────

def main() -> None:
    cfg = _load_config()
    setup_logging(cfg["log_file"], cfg["log_level"])
    log = get_logger(__name__)

    log.info("pipeline_startup",
             model=cfg["ollama_model"],
             threshold=cfg["confidence_threshold"],
             input_dir=cfg["input_dir"],
             timeout=cfg["ollama_timeout"])

    _set_tesseract(cfg["tesseract_path"])

    pdf_files = _discover_pdfs(cfg["input_dir"])
    if not pdf_files:
        log.warning("no_pdfs_found", dir=cfg["input_dir"])
        return

    all_records = []
    all_raw_results = []

    for pdf_path in tqdm(pdf_files, desc="Processing PDFs"):
        metrics.start_timer("doc_total")
        result = run_pipeline(pdf_path, cfg)
        metrics.stop_timer("doc_total")
        all_raw_results.append(result)

        # ── Debug: log exactly what came back from each doc ──
        record = result.get("record")
        if result.get("error"):
            log.error("doc_failed",
                      file=result["source_file"],
                      error=result["error"])
        elif not record:
            log.warning("doc_no_record",
                        file=result["source_file"],
                        ok=result.get("ok"))
        elif not record.get("SourceFile"):
            log.warning("doc_record_missing_sourcefile",
                        file=result["source_file"])
        else:
            # Collect all line item rows (merger returns one row per line item)
            line_records = result.get("records", [record])
            all_records.extend(line_records)
            log.info("doc_record_collected",
                     file=result["source_file"],
                     invoice_id=record.get("InvoiceId"),
                     grand_total=record.get("InvGrandTotal"),
                     seller=record.get("SellerName"),
                     line_item_rows=len(line_records),
                     action=result.get("confidence", {}).get("action"))

    log.info("all_docs_done",
             total_pdfs=len(pdf_files),
             records_collected=len(all_records),
             failed=[r["source_file"] for r in all_raw_results if r.get("error")])

    # ── Always write outputs — even empty — so run is traceable ──
    out_dir  = os.path.join(cfg["output_dir"], "processed")
    os.makedirs(out_dir, exist_ok=True)

    json_path = os.path.join(out_dir, "file.json")
    csv_path  = os.path.join(out_dir, "file.csv")
    sql_path  = os.path.join(out_dir, "file.sql")

    write_json(all_records, json_path, cfg["json_indent"])
    _write_csv(all_records, csv_path)

    if all_records:
        write_sqlite(all_records, cfg["sqlite_db"], sql_path)
    else:
        log.warning("no_records_extracted",
                    hint="All documents failed or returned empty records. "
                         "Check logs for doc_failed / doc_no_record entries.")

    # ── Run summary ───────────────────────────────────────────
    summary_path = os.path.join(cfg["output_dir"], "run_summary.json")
    summary = {
        "total_pdfs":        len(pdf_files),
        "records_extracted": len(all_records),
        "queued_for_review": sum(1 for r in all_raw_results if r.get("queued_for_review")),
        "errors":            [r["source_file"] for r in all_raw_results if r.get("error")],
        "per_doc": [
            {
                "file":       r["source_file"],
                "ok":         r.get("ok"),
                "action":     r.get("confidence", {}).get("action"),
                "invoice_id": r.get("record", {}).get("InvoiceId"),
                "error":      r.get("error"),
            }
            for r in all_raw_results
        ],
    }
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)

    metrics.flush(os.path.join(cfg["output_dir"], "metrics.json"))

    log.info("outputs_written",
             json=json_path, csv=csv_path,
             summary=summary_path,
             records=len(all_records))
    print(f"\n✅ Done. {len(all_records)}/{len(pdf_files)} records extracted.")
    print(f"   JSON  → {json_path}")
    print(f"   CSV   → {csv_path}")
    print(f"   Summary → {summary_path}")


if __name__ == "__main__":
    main()
