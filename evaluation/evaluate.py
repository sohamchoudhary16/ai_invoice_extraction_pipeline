"""
evaluation/evaluate.py
Systematic assessment of extraction quality against manually verified ground truth.

What this is
------------
This is an EVALUATION script — it measures how accurately the pipeline
extracted fields compared to human-verified correct values.

This is NOT a unit test. It runs the full pipeline on real PDFs and
compares results to ground truth JSON files.

Usage
-----
    python -m evaluation.evaluate

    # with options:
    python -m evaluation.evaluate --invoice v3_01 --verbose
    python -m evaluation.evaluate --no-run   # use existing output/processed/latest/

Output
------
    evaluation/results/evaluation_report_TIMESTAMP.json
    evaluation/results/evaluation_report_TIMESTAMP.csv
    Console summary table (printed always)

Ground truth files
------------------
    test_ground_truth/gt_invoices_invoice_v3_01.json
    test_ground_truth/gt_invoices_invoice_v3_02.json
    test_ground_truth/gt_invoices_invoice_v3_03.json
    test_ground_truth/gt_line_items_invoice_v3_01.json
    test_ground_truth/gt_line_items_invoice_v3_02.json
    test_ground_truth/gt_line_items_invoice_v3_03.json
"""

import os
import sys
import json
import argparse
import yaml
import pytesseract
import pandas as pd
from datetime import datetime, timezone
from pathlib import Path

# ── Make sure project root is on sys.path ────────────────────────────────────
_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

from src.pipeline import run_pipeline
from dotenv import load_dotenv

load_dotenv()


# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

GT_DIR        = _ROOT / "test_ground_truth"
RESULTS_DIR   = _ROOT / "evaluation" / "results"
SCAN_DIR      = _ROOT / "sample_pdf" / "scan"
LATEST_DIR    = _ROOT / "output" / "processed" / "latest"
CONFIG_PATH   = _ROOT / "configs" / "config.yaml"

# Fields evaluated for INVOICE header accuracy
INVOICE_EVAL_FIELDS = [
    "InvoiceId", "IssueDate", "DueDate", "Currency",
    "SellerName", "SellerVatId", "SellerStreet", "SellerCity", "SellerCountry",
    "BuyerName", "BuyerStreet", "BuyerCity", "BuyerCountry", "BuyerEmail",
    "InvLineTotal", "InvTaxBasisTotal", "InvTaxTotal", "InvGrandTotal",
    "InvDuePayable", "PaymentMeansCode", "Bic", "PaymentTerms",
]

# Fields evaluated for LINE ITEM accuracy
LINE_ITEM_EVAL_FIELDS = [
    "LineId", "ProductName", "BilledQuantity", "BilledUnit",
    "NetPrice", "LineTotalAmount", "TaxRatePercent", "DiscountAmount",
]

# Numeric fields — use tolerance comparison, not string equality
NUMERIC_FIELDS = {
    "InvLineTotal", "InvTaxBasisTotal", "InvTaxTotal",
    "InvGrandTotal", "InvDuePayable", "InvAllowanceTotal",
    "NetPrice", "LineTotalAmount", "TaxRatePercent",
    "DiscountAmount", "BilledQuantity",
}
NUMERIC_TOLERANCE = 0.10  # ±€/$ 0.10 for financial fields


# ─────────────────────────────────────────────────────────────────────────────
# Config loader (same as main.py)
# ─────────────────────────────────────────────────────────────────────────────

def _load_cfg() -> dict:
    with open(CONFIG_PATH) as f:
        y = yaml.safe_load(f)
    cfg = {
        "confidence_threshold": int(os.getenv("CONFIDENCE_THRESHOLD",
                                               y["ocr"]["confidence_threshold"])),
        "dpi":                  int(y["ocr"]["dpi"]),
        "tesseract_psm":        int(y["ocr"]["tesseract_psm"]),
        "tesseract_lang":       y["ocr"].get("lang", "deu+eng"),
        "ollama_base_url":      os.getenv("OLLAMA_BASE_URL",  y["ollama"]["base_url"]),
        "ollama_model":         os.getenv("OLLAMA_MODEL",     y["ollama"]["model"]),
        "ollama_temperature":   float(y["ollama"]["temperature"]),
        "ollama_timeout":       int(os.getenv("OLLAMA_TIMEOUT", y["ollama"]["timeout"])),
        "skip_digital_pdf_ocr": y["pipeline"]["skip_digital_pdf_ocr"],
        "max_pages":            y["pipeline"]["max_pages_per_doc"],
        "output_dir":           "output",
        "sqlite_db":            y["output"]["sqlite_db"],
        "vision_model":         os.getenv("VISION_MODEL",
                                          y["ollama"].get("vision_model",
                                                          y["ollama"]["model"])),
        "json_indent":          y["output"]["json_indent"],
        "log_file":             y["logging"]["log_file"],
        "log_level":            y["logging"]["level"],
        "tesseract_path":       os.getenv("TESSERACT_PATH", "/usr/bin/tesseract"),
    }
    tess = cfg["tesseract_path"]
    if os.path.exists(tess):
        pytesseract.pytesseract.tesseract_cmd = tess
    return cfg


# ─────────────────────────────────────────────────────────────────────────────
# Ground truth loader
# ─────────────────────────────────────────────────────────────────────────────

def _load_ground_truth() -> dict[str, dict]:
    """
    Returns {invoice_id: {"invoice": {...}, "line_items": [...]}}
    for every ground truth file found in GT_DIR.
    """
    gt: dict[str, dict] = {}

    for inv_file in sorted(GT_DIR.glob("gt_invoices_*.json")):
        invoices = json.loads(inv_file.read_text(encoding="utf-8"))
        for inv in invoices:
            inv_id = inv["InvoiceId"]
            gt.setdefault(inv_id, {"invoice": {}, "line_items": []})
            gt[inv_id]["invoice"] = inv
            gt[inv_id]["source_file"] = inv.get("SourceFile", "")

    for li_file in sorted(GT_DIR.glob("gt_line_items_*.json")):
        items = json.loads(li_file.read_text(encoding="utf-8"))
        for item in items:
            inv_id = item["InvoiceId"]
            gt.setdefault(inv_id, {"invoice": {}, "line_items": []})
            gt[inv_id]["line_items"].append(item)

    return gt


# ─────────────────────────────────────────────────────────────────────────────
# Comparison helpers
# ─────────────────────────────────────────────────────────────────────────────

def _normalise(value, field: str):
    """Normalise a value for comparison — strip whitespace, lowercase strings."""
    if value is None or value == "" or str(value).lower() in {"null", "none", "n/a"}:
        return None
    if field in NUMERIC_FIELDS:
        try:
            return float(str(value).replace(",", ".").strip())
        except (ValueError, TypeError):
            return None
    return str(value).strip()


def _match(extracted, expected, field: str) -> str:
    """
    Return 'exact', 'close' (numeric within tolerance), or 'wrong'.
    'close' only applies to numeric fields.
    """
    e = _normalise(extracted, field)
    x = _normalise(expected, field)

    if x is None:
        return "skip"       # GT doesn't have this field — don't penalise
    if e is None:
        return "missing"    # pipeline returned null, GT has a value

    if field in NUMERIC_FIELDS:
        try:
            if abs(float(e) - float(x)) <= NUMERIC_TOLERANCE:
                return "exact"
            return "wrong"
        except (TypeError, ValueError):
            return "wrong"

    # String comparison — case-insensitive, strip whitespace
    if str(e).lower().strip() == str(x).lower().strip():
        return "exact"
    return "wrong"


def _compare_invoice(extracted: dict, gt_invoice: dict, fields: list[str]) -> list[dict]:
    """Field-by-field comparison for invoice header. Returns list of result rows."""
    rows = []
    for field in fields:
        result = _match(extracted.get(field), gt_invoice.get(field), field)
        if result == "skip":
            continue
        rows.append({
            "field":     field,
            "result":    result,
            "extracted": extracted.get(field),
            "expected":  gt_invoice.get(field),
        })
    return rows


def _compare_line_items(
    extracted_items: list[dict],
    gt_items: list[dict],
    fields: list[str],
) -> list[dict]:
    """
    Match extracted line items to GT items by LineId.
    Returns per-field comparison rows with line item context.
    """
    rows = []
    gt_by_line = {str(item.get("LineId", "")): item for item in gt_items}

    for ex_item in extracted_items:
        line_id = str(ex_item.get("LineId", ""))
        gt_item = gt_by_line.get(line_id)
        if not gt_item:
            # Try to match by product name if LineId missing
            for g in gt_items:
                if (str(g.get("ProductName", "")).lower()
                        in str(ex_item.get("ProductName", "")).lower()):
                    gt_item = g
                    break

        if not gt_item:
            rows.append({
                "field": "LineId",
                "result": "wrong",
                "extracted": line_id,
                "expected": "no matching GT line item",
                "line_id": line_id,
            })
            continue

        for field in fields:
            result = _match(ex_item.get(field), gt_item.get(field), field)
            if result == "skip":
                continue
            rows.append({
                "field":     field,
                "result":    result,
                "extracted": ex_item.get(field),
                "expected":  gt_item.get(field),
                "line_id":   line_id,
            })

    # Check for GT items not extracted at all
    extracted_line_ids = {str(i.get("LineId", "")) for i in extracted_items}
    for gt_item in gt_items:
        if str(gt_item.get("LineId", "")) not in extracted_line_ids:
            rows.append({
                "field":     "LineId",
                "result":    "missing",
                "extracted": None,
                "expected":  gt_item.get("LineId"),
                "line_id":   gt_item.get("LineId"),
            })

    return rows


# ─────────────────────────────────────────────────────────────────────────────
# Run pipeline on one PDF
# ─────────────────────────────────────────────────────────────────────────────

def _load_from_vision(source_file: str, run_ts: str | None = None) -> tuple[dict, list[dict]]:
    """
    Load vision-fallback extracted records from output/processed/.

    Looks in:
      1. output/processed/latest/vision/  (most recent run)
      2. output/processed/{run_ts}/vision/ (specific run if provided)

    Returns (vision_invoice_record, vision_line_item_records)
    or ({}, []) if no vision output exists for this file.
    """
    search_dirs = []
    if run_ts:
        search_dirs.append(_ROOT / "output" / "processed" / run_ts / "vision")
    search_dirs.append(_ROOT / "output" / "processed" / "latest" / "vision")

    for vision_dir in search_dirs:
        inv_file  = vision_dir / "invoices.json"
        line_file = vision_dir / "line_items.json"
        if not inv_file.exists():
            continue
        invoices   = json.loads(inv_file.read_text(encoding="utf-8"))
        line_items = json.loads(line_file.read_text(encoding="utf-8")) if line_file.exists() else []
        inv_record = next(
            (r for r in invoices if r.get("SourceFile") == source_file), {}
        )
        li_records = [r for r in line_items if r.get("SourceFile") == source_file]
        if inv_record:
            return inv_record, li_records

    return {}, []


def _run_one(source_file: str, cfg: dict) -> tuple[dict, list[dict]]:
    """
    Run run_pipeline() on a PDF and return (invoice_record, line_item_records).
    Falls back to output/processed/latest/ if the PDF is not found in scan dir.
    """
    pdf_path = SCAN_DIR / source_file
    if not pdf_path.exists():
        raise FileNotFoundError(
            f"PDF not found: {pdf_path}\n"
            f"Drop {source_file} into sample_pdf/scan/ and re-run."
        )

    print(f"  Running pipeline on {source_file}...")
    raw = run_pipeline(str(pdf_path), cfg)

    if raw.get("error"):
        raise RuntimeError(f"Pipeline error on {source_file}: {raw['error']}")

    record    = raw.get("record") or {}
    records   = raw.get("records") or ([record] if record else [])

    # Invoice header = first record (they all share header fields)
    inv_record = records[0] if records else {}

    # Line items = all records (one per line item in the flat pipeline output)
    # A record is a line item if it has ProductName or LineId
    line_items = [
        r for r in records
        if r.get("ProductName") or r.get("LineId")
    ]

    return inv_record, line_items


def _load_from_latest(source_file: str) -> tuple[dict, list[dict]]:
    """
    Load already-extracted records from output/processed/latest/
    instead of re-running the pipeline. Used with --no-run flag.
    """
    inv_file  = LATEST_DIR / "invoices.json"
    line_file = LATEST_DIR / "line_items.json"

    if not inv_file.exists():
        raise FileNotFoundError(
            f"No invoices.json in {LATEST_DIR}. Run the pipeline first."
        )

    invoices   = json.loads(inv_file.read_text())
    line_items = json.loads(line_file.read_text()) if line_file.exists() else []

    inv_record = next(
        (r for r in invoices if r.get("SourceFile") == source_file), {}
    )
    li_records = [r for r in line_items if r.get("SourceFile") == source_file]

    return inv_record, li_records


# ─────────────────────────────────────────────────────────────────────────────
# Accuracy summary
# ─────────────────────────────────────────────────────────────────────────────

def _accuracy_summary(rows: list[dict]) -> dict:
    """Compute accuracy metrics from a list of comparison rows."""
    countable = [r for r in rows if r["result"] != "skip"]
    if not countable:
        return {"total": 0, "exact": 0, "missing": 0, "wrong": 0, "accuracy_pct": 0.0}

    exact   = sum(1 for r in countable if r["result"] == "exact")
    missing = sum(1 for r in countable if r["result"] == "missing")
    wrong   = sum(1 for r in countable if r["result"] == "wrong")

    return {
        "total":        len(countable),
        "exact":        exact,
        "missing":      missing,
        "wrong":        wrong,
        "accuracy_pct": round(exact / len(countable) * 100, 1),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Main evaluation loop
# ─────────────────────────────────────────────────────────────────────────────

def run_evaluation(
    filter_invoice: str | None = None,
    no_run: bool = False,
    verbose: bool = False,
) -> dict:
    """
    Run the full evaluation. Returns the complete results dict.

    Parameters
    ----------
    filter_invoice : str | None
        If set (e.g. 'v3_01'), only evaluate that invoice.
    no_run : bool
        If True, load from output/processed/latest/ instead of running pipeline.
    verbose : bool
        If True, print every field comparison row.
    """
    gt   = _load_ground_truth()
    cfg  = None if no_run else _load_cfg()

    run_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    all_results = []

    print(f"\n{'='*60}")
    print(f"  Invoice Extraction — Quality Evaluation")
    print(f"  {len(gt)} ground-truth invoice(s) loaded")
    print(f"  Mode: {'use existing output' if no_run else 'run pipeline now'}")
    print(f"{'='*60}\n")

    for inv_id, gt_data in sorted(gt.items()):
        source_file = gt_data["source_file"]

        # Optional filter
        if filter_invoice and filter_invoice not in source_file:
            continue

        print(f"── {inv_id}  ({source_file})")

        try:
            if no_run:
                inv_record, li_records = _load_from_latest(source_file)
            else:
                inv_record, li_records = _run_one(source_file, cfg)
        except (FileNotFoundError, RuntimeError) as e:
            print(f"   SKIP: {e}\n")
            continue

        # ── Invoice header comparison ─────────────────────────
        inv_rows = _compare_invoice(inv_record, gt_data["invoice"], INVOICE_EVAL_FIELDS)
        inv_summary = _accuracy_summary(inv_rows)

        # ── Line item comparison ──────────────────────────────
        li_rows = _compare_line_items(li_records, gt_data["line_items"], LINE_ITEM_EVAL_FIELDS)
        li_summary = _accuracy_summary(li_rows)

        # ── Vision fallback comparison ────────────────────────
        vis_inv_record, vis_li_records = _load_from_vision(source_file)
        vis_inv_rows, vis_li_rows = [], []
        vis_inv_summary = vis_li_summary = None

        if vis_inv_record:
            vis_inv_rows    = _compare_invoice(vis_inv_record, gt_data["invoice"], INVOICE_EVAL_FIELDS)
            vis_li_rows     = _compare_line_items(vis_li_records, gt_data["line_items"], LINE_ITEM_EVAL_FIELDS)
            vis_inv_summary = _accuracy_summary(vis_inv_rows)
            vis_li_summary  = _accuracy_summary(vis_li_rows)

        # ── Print field-level table ───────────────────────────
        print(f"   Invoice header accuracy : "
              f"{inv_summary['exact']}/{inv_summary['total']}  "
              f"({inv_summary['accuracy_pct']}%)  [OCR+LLM]")
        if vis_inv_summary:
            print(f"   Invoice header accuracy : "
                  f"{vis_inv_summary['exact']}/{vis_inv_summary['total']}  "
                  f"({vis_inv_summary['accuracy_pct']}%)  [Vision]")
        print(f"   Line item accuracy      : "
              f"{li_summary['exact']}/{li_summary['total']}  "
              f"({li_summary['accuracy_pct']}%)  [OCR+LLM]")
        if vis_li_summary:
            print(f"   Line item accuracy      : "
                  f"{vis_li_summary['exact']}/{vis_li_summary['total']}  "
                  f"({vis_li_summary['accuracy_pct']}%)  [Vision]")

        if verbose or inv_summary["wrong"] > 0 or inv_summary["missing"] > 0:
            print()
            print(f"   {'Field':<28}  {'Result':<10}  {'Extracted':<30}  Expected")
            print(f"   {'-'*28}  {'-'*10}  {'-'*30}  {'-'*30}")
            for row in inv_rows:
                if row["result"] != "exact" or verbose:
                    mark = "OK" if row["result"] == "exact" else "!!"
                    ex = str(row["extracted"] or "")[:28]
                    ex_gt = str(row["expected"] or "")[:28]
                    print(f"   {mark} {row['field']:<26}  {row['result']:<10}  {ex:<30}  {ex_gt}")

        if verbose and li_rows:
            print()
            print(f"   Line items:")
            for row in li_rows:
                mark = "OK" if row["result"] == "exact" else "!!"
                li = row.get("line_id", "?")
                ex = str(row["extracted"] or "")[:25]
                ex_gt = str(row["expected"] or "")[:25]
                print(f"   {mark} [{li}] {row['field']:<22}  {row['result']:<10}  {ex:<25}  {ex_gt}")

        print()

        all_results.append({
            "invoice_id":   inv_id,
            "source_file":  source_file,
            "run_ts":       run_ts,
            "invoice_summary":           inv_summary,
            "line_item_summary":         li_summary,
            "invoice_rows":              inv_rows,
            "line_item_rows":            li_rows,
            "vision_invoice_summary":    vis_inv_summary,
            "vision_line_item_summary":  vis_li_summary,
            "vision_invoice_rows":       vis_inv_rows,
            "vision_line_item_rows":     vis_li_rows,
            "vision_available":          bool(vis_inv_record),
        })

    # ── Aggregate summary ─────────────────────────────────────────────────
    total_inv_fields  = sum(r["invoice_summary"]["total"]    for r in all_results)
    exact_inv_fields  = sum(r["invoice_summary"]["exact"]    for r in all_results)
    total_li_fields   = sum(r["line_item_summary"]["total"]  for r in all_results)
    exact_li_fields   = sum(r["line_item_summary"]["exact"]  for r in all_results)

    overall_inv_acc = round(exact_inv_fields / total_inv_fields * 100, 1) if total_inv_fields else 0
    overall_li_acc  = round(exact_li_fields  / total_li_fields  * 100, 1) if total_li_fields  else 0

    print(f"{'='*60}")
    print(f"  OVERALL RESULTS  ({len(all_results)} invoice(s) evaluated)")
    print(f"{'='*60}")
    # Vision aggregate
    vis_results = [r for r in all_results if r.get("vision_available")]
    vis_inv_total  = sum(r["vision_invoice_summary"]["total"]   for r in vis_results if r["vision_invoice_summary"])
    vis_inv_exact  = sum(r["vision_invoice_summary"]["exact"]   for r in vis_results if r["vision_invoice_summary"])
    vis_li_total   = sum(r["vision_line_item_summary"]["total"] for r in vis_results if r["vision_line_item_summary"])
    vis_li_exact   = sum(r["vision_line_item_summary"]["exact"] for r in vis_results if r["vision_line_item_summary"])
    vis_inv_acc    = round(vis_inv_exact / vis_inv_total * 100, 1) if vis_inv_total else None
    vis_li_acc     = round(vis_li_exact  / vis_li_total  * 100, 1) if vis_li_total  else None

    print(f"  Invoice header accuracy  : "
          f"{exact_inv_fields}/{total_inv_fields}  ({overall_inv_acc}%)  [OCR+LLM]")
    if vis_inv_acc is not None:
        print(f"  Invoice header accuracy  : "
              f"{vis_inv_exact}/{vis_inv_total}  ({vis_inv_acc}%)  [Vision]")
    print(f"  Line item accuracy       : "
          f"{exact_li_fields}/{total_li_fields}  ({overall_li_acc}%)  [OCR+LLM]")
    if vis_li_acc is not None:
        print(f"  Line item accuracy       : "
              f"{vis_li_exact}/{vis_li_total}  ({vis_li_acc}%)  [Vision]")
    print(f"{'='*60}\n")

    # Per-field breakdown (most useful for presentations)
    print("  Per-field breakdown (invoice header):")
    field_counts: dict[str, dict] = {}
    for res in all_results:
        for row in res["invoice_rows"]:
            f = row["field"]
            field_counts.setdefault(f, {"exact": 0, "total": 0})
            if row["result"] != "skip":
                field_counts[f]["total"] += 1
                if row["result"] == "exact":
                    field_counts[f]["exact"] += 1

    print(f"  {'Field':<28}  {'Correct':<8}  {'Total':<6}  Accuracy")
    print(f"  {'-'*28}  {'-'*8}  {'-'*6}  {'-'*8}")
    for field, counts in sorted(field_counts.items(),
                                 key=lambda x: x[1]["exact"] / max(x[1]["total"], 1)):
        acc = round(counts["exact"] / counts["total"] * 100) if counts["total"] else 0
        bar = "█" * (acc // 10) + "░" * (10 - acc // 10)
        print(f"  {field:<28}  {counts['exact']:<8}  {counts['total']:<6}  {acc:>3}%  {bar}")

    print()

    # ── Write results ─────────────────────────────────────────────────────
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = RESULTS_DIR / f"evaluation_report_{run_ts}.json"
    csv_path    = RESULTS_DIR / f"evaluation_report_{run_ts}.csv"

    report = {
        "run_timestamp":  run_ts,
        "invoices_evaluated": len(all_results),
        "overall": {
            "invoice_header_accuracy_pct": overall_inv_acc,
            "line_item_accuracy_pct":      overall_li_acc,
        },
        "per_invoice": all_results,
    }
    report_path.write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8"
    )

    # Flat CSV for spreadsheet analysis
    flat_rows = []
    for res in all_results:
        for row in res["invoice_rows"]:
            flat_rows.append({
                "invoice_id":   res["invoice_id"],
                "source_file":  res["source_file"],
                "record_type":  "invoice_header",
                "line_id":      "",
                **row,
            })
        for row in res["line_item_rows"]:
            flat_rows.append({
                "invoice_id":   res["invoice_id"],
                "source_file":  res["source_file"],
                "record_type":  "line_item",
                **row,
            })

    if flat_rows:
        pd.DataFrame(flat_rows).to_csv(csv_path, index=False, encoding="utf-8-sig")

    print(f"  Report saved: {report_path}")
    print(f"  CSV saved   : {csv_path}\n")

    return report


# ─────────────────────────────────────────────────────────────────────────────
# CLI entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Evaluate extraction quality against ground truth"
    )
    parser.add_argument(
        "--invoice",
        help="Only evaluate this invoice (e.g. v3_01, v3_02)",
        default=None,
    )
    parser.add_argument(
        "--no-run",
        action="store_true",
        help="Load from output/processed/latest/ instead of running pipeline",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Print every field comparison row including correct ones",
    )
    args = parser.parse_args()
    run_evaluation(
        filter_invoice=args.invoice,
        no_run=args.no_run,
        verbose=args.verbose,
    )
