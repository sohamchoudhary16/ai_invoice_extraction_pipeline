"""
src/main.py
Entry point — unchanged pipeline, production output layer added.

Changes vs previous version:
  1. File hash + processing_status table  → incremental, skip already-done
  2. Quarantine corrupt/bad files         → output/quarantine/
  3. Normalised two-table output          → invoices + invoice_line_items
  4. Timestamped outputs                  → runs never overwrite each other

pipeline.py and all OCR/LLM modules are NOT touched.

app: # Start the API
uvicorn app.api:app --reload --port 8000
"""

import os
import json
import yaml
import pandas as pd
import pytesseract
from datetime import datetime, timezone
from dotenv import load_dotenv
from tqdm import tqdm
import shutil

from src.pipeline import run_pipeline
from src.ingestion.file_tracker import FileTracker, compute_file_hash
from src.ingestion.loader import load_pdf
from src.quarantine.quarantine import quarantine_file
from src.storage.record_splitter import split_records
from src.storage.json_writer import write_json
from src.storage.sqlite_writer import write_sqlite
from src.observability.logger import setup_logging, get_logger
from src.observability.metrics import metrics
from src.extraction.vision_fallback import run_vision_fallback, VISION_THRESHOLD

load_dotenv()


# ─────────────────────────────────────────────────────────────
#  Config — unchanged
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
        "tesseract_lang":   y["ocr"].get("lang", "deu+eng"),
        "ollama_base_url":  os.getenv("OLLAMA_BASE_URL",  y["ollama"]["base_url"]),
        "ollama_model":     os.getenv("OLLAMA_MODEL",     y["ollama"]["model"]),
        "ollama_temperature": float(y["ollama"]["temperature"]),
        "ollama_timeout":   int(os.getenv("OLLAMA_TIMEOUT", y["ollama"]["timeout"])),
        "skip_digital_pdf_ocr": y["pipeline"]["skip_digital_pdf_ocr"],
        "max_pages":        y["pipeline"]["max_pages_per_doc"],
        "input_dir":  os.getenv("INPUT_DIR",  y.get("input_dir", "sample_pdf/scan")),
        "output_dir": os.getenv("OUTPUT_DIR", "output"),
        "log_file":   os.getenv("LOG_FILE",   y["logging"]["log_file"]),
        "log_level":  y["logging"]["level"],
        "tesseract_path": os.getenv(
            "TESSERACT_PATH",
            r"C:\Users\SohamChoudhary\Downloads\Documents\tesseract\tesseract.exe"
        ),
        "json_indent": y["output"]["json_indent"],
        "sqlite_db":   y["output"]["sqlite_db"],
        "vision_model": os.getenv("VISION_MODEL", y["ollama"].get("vision_model", y["ollama"]["model"])),
    }


def _set_tesseract(path: str) -> None:
    if os.path.exists(path):
        pytesseract.pytesseract.tesseract_cmd = path
        print(f"Tesseract set: {path}")
    else:
        print(f"WARNING: Tesseract NOT found at '{path}'")


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


def _write_csv(records: list[dict], path: str, columns: list[str]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    rows = [{col: r.get(col) for col in columns} for r in records]
    df = pd.DataFrame(rows, columns=columns)
    df.to_csv(path, index=False, encoding="utf-8-sig")
    print(f"[CSV] {len(rows)} row(s) → {path}")


# ─────────────────────────────────────────────────────────────
#  Main
# ─────────────────────────────────────────────────────────────

def main() -> None:
    cfg = _load_config()
    setup_logging(cfg["log_file"], cfg["log_level"])
    log = get_logger(__name__)

    # Run timestamp — one value shared across all outputs this run
    run_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    log.info("pipeline_startup",
             model=cfg["ollama_model"],
             vision_model=cfg["vision_model"],
             threshold=cfg["confidence_threshold"],
             vision_threshold=VISION_THRESHOLD,
             input_dir=cfg["input_dir"],
             run_timestamp=run_ts)

    _set_tesseract(cfg["tesseract_path"])

    pdf_files = _discover_pdfs(cfg["input_dir"])
    if not pdf_files:
        log.warning("no_pdfs_found", dir=cfg["input_dir"])
        return

    # FIX 1 + 2: file tracker for incremental processing + quarantine
    tracker = FileTracker(cfg["sqlite_db"])

    all_flat_records = []   # raw flat dicts from pipeline (one per line item)
    all_file_hashes  = {}   # source_file → file_hash
    all_results      = []   # full result dicts — used by vision fallback
    low_conf_results = []   # results where composite score < VISION_THRESHOLD
    stats = {"processed": 0, "skipped": 0, "quarantined": 0, "failed": 0}

    for pdf_path in tqdm(pdf_files, desc="Processing PDFs"):
        source_file = os.path.basename(pdf_path)

        # ── Compute hash ──────────────────────────────────────
        try:
            file_hash = compute_file_hash(pdf_path)
        except Exception as e:
            log.error("hash_failed", file=source_file, error=str(e))
            stats["failed"] += 1
            continue

        # ── Skip already-processed files (incremental) ────────
        if tracker.already_processed(file_hash):
            log.info("skipped_already_processed",
                     file=source_file, hash=file_hash[:12])
            stats["skipped"] += 1
            continue

        # ── Quarantine check before pipeline ─────────────────
        load_result = load_pdf(pdf_path)
        if not load_result.ok:
            quarantine_file(
                pdf_path,
                reason=load_result.error or "LOAD_FAILED",
                detail=f"hash={file_hash[:12]}"
            )
            tracker.mark(file_hash, source_file, "quarantined", run_ts,
                         error=load_result.error or "LOAD_FAILED")
            stats["quarantined"] += 1
            log.warning("quarantined", file=source_file, reason=load_result.error)
            continue

        # ── Run pipeline (unchanged) ──────────────────────────
        metrics.start_timer("doc_total")
        result = run_pipeline(pdf_path, cfg)
        metrics.stop_timer("doc_total")
        result["_pdf_path"] = os.path.abspath(pdf_path)   # absolute path for vision fallback

        if result.get("error"):
            tracker.mark(file_hash, source_file, "failed", run_ts,
                         error=result["error"])
            stats["failed"] += 1
            log.error("doc_failed", file=source_file, error=result["error"])
            continue

        record  = result.get("record")
        records = result.get("records", [record] if record else [])

        if not records or not (record and record.get("SourceFile")):
            tracker.mark(file_hash, source_file, "failed", run_ts,
                         error="no_records_returned")
            stats["failed"] += 1
            log.warning("doc_no_record", file=source_file)
            continue

        # Attach hash to each record for lineage
        for r in records:
            r["FileHash"] = file_hash

        all_flat_records.extend(records)
        all_file_hashes[source_file] = file_hash
        all_results.append(result)

        # Track low-confidence docs for vision fallback
        comp_score = result.get("confidence", {}).get("composite_score", 1.0)
        if comp_score < VISION_THRESHOLD:
            low_conf_results.append(result)
            log.info("low_confidence_flagged_for_vision",
                     file=source_file,
                     composite_score=comp_score,
                     vision_model=cfg["vision_model"])

        tracker.mark(
            file_hash, source_file, "success", run_ts,
            invoice_id=record.get("InvoiceId", "")
        )
        stats["processed"] += 1
        log.info("doc_done",
                 file=source_file,
                 invoice_id=record.get("InvoiceId"),
                 grand_total=record.get("InvGrandTotal"),
                 rows=len(records),
                 model=cfg["ollama_model"],
                 action=result.get("confidence", {}).get("action"),
                 composite_score=result.get("confidence", {}).get("composite_score"))

    log.info("all_docs_done",
             run_timestamp=run_ts,
             stats=stats,
             total_flat_records=len(all_flat_records))

    # ── FIX 3: Split flat records into two normalised tables ──
    invoice_records, line_item_records = split_records(
        all_flat_records,
        run_timestamp=run_ts,
    )

    # ── FIX 4: Write timestamped outputs ──────────────────────
    out_dir = cfg["output_dir"]
    run_dir = os.path.join(out_dir, "processed", run_ts)
    os.makedirs(run_dir, exist_ok=True)

    # JSON (timestamped + latest/)
    inv_json, line_json = write_json(
        invoice_records,
        line_item_records,
        out_dir,
        run_ts,
        cfg["json_indent"],
    )

    # CSV (timestamped)
    from src.storage.sqlite_writer import _INV_COLS, _LINE_COLS
    _write_csv(invoice_records,   os.path.join(run_dir, "invoices.csv"),   _INV_COLS)
    _write_csv(line_item_records, os.path.join(run_dir, "line_items.csv"), _LINE_COLS)

    # SQLite (two normalised tables)
    sql_path = os.path.join(run_dir, "dump.sql")
    if invoice_records or line_item_records:
        write_sqlite(invoice_records, line_item_records, cfg["sqlite_db"], sql_path)

    # ── ADD THIS ──────────────────────────────────────────────
    if low_conf_results:
        run_vision_fallback(
            low_confidence_results=low_conf_results,
            all_flat_records=all_flat_records,
            cfg=cfg,
            run_ts=run_ts,
            run_dir=run_dir,
        )
        # Copy vision outputs to latest/vision/ for evaluation --no-run
        vision_src = os.path.join(run_dir, "vision")
        if os.path.isdir(vision_src):
            latest_vision = os.path.join(out_dir, "processed", "latest", "vision")
            if os.path.isdir(latest_vision):
                shutil.rmtree(latest_vision)
            shutil.copytree(vision_src, latest_vision)
            log.info("vision_copied_to_latest", dest=latest_vision)
    # ─────────────────────────────────────────────────────────

    # Run summary
    summary_path = os.path.join(run_dir, "run_summary.json")
    summary = {
        "run_timestamp":    run_ts,
        "stats":            stats,
        "invoices":         len(invoice_records),
        "line_items":       len(line_item_records),
        "tracker_totals":   tracker.status_summary(),
    }
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)

    metrics.flush(os.path.join(out_dir, "metrics.json"))

    print(f"\n✅ Done  [{run_ts}]")
    print(f"   Processed   : {stats['processed']} new")
    print(f"   Skipped     : {stats['skipped']} (already done)")
    print(f"   Quarantined : {stats['quarantined']}")
    print(f"   Failed      : {stats['failed']}")
    print(f"   Invoices    : {len(invoice_records)}")
    print(f"   Line items  : {len(line_item_records)}")
    print(f"   JSON latest : {out_dir}/processed/latest/")
    print(f"   DB          : {cfg['sqlite_db']}")
    print(f"   Summary     : {summary_path}")
    if low_conf_results:
        print(f"   Vision docs : {len(low_conf_results)} re-processed")
        print(f"   Vision out  : {run_dir}/vision/")
        print(f"   Comparison  : {run_dir}/vision/comparison.json")


if __name__ == "__main__":
    main()
