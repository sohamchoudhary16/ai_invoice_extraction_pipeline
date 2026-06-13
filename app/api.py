"""
app/api.py
FastAPI service — thin HTTP wrapper around the existing pipeline.

Endpoints
---------
POST /extract          Upload a single PDF → run pipeline → return JSON result
GET  /result/{job_id}  Poll job status (async path)
GET  /jobs             List all jobs in this server session
GET  /health           Ollama + Tesseract liveness check
GET  /                 Redirect to /docs

Run locally
-----------
    uvicorn app.api:app --reload --port 8000

Then open:  http://localhost:8000/docs   (Swagger UI — live demo screen)
"""

import os
import shutil
import tempfile
import uuid
import pytesseract
import yaml

from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from fastapi import FastAPI, File, UploadFile, BackgroundTasks, HTTPException
from fastapi.responses import RedirectResponse
from dotenv import load_dotenv

from src.pipeline import run_pipeline
from src.models.invoice import ExtractionResult
from src.storage.record_splitter import split_records
from src.storage.json_writer import write_json
from src.storage.sqlite_writer import write_sqlite, _INV_COLS, _LINE_COLS
from src.ingestion.file_tracker import FileTracker, compute_file_hash
from src.observability.logger import setup_logging, get_logger

load_dotenv()

# ─────────────────────────────────────────────────────────────
#  App setup
# ─────────────────────────────────────────────────────────────

app = FastAPI(
    title="Invoice Extraction API",
    description=(
        "Extracts structured data from PDF invoices using Tesseract OCR "
        "and a local Gemma 3 4B vision/text model via Ollama.\n\n"
        "Upload a PDF to `/extract` and get back a typed JSON result "
        "including invoice header, line items, confidence score, and "
        "validation errors."
    ),
    version="2.0.0",
)

log = get_logger(__name__)

# In-memory job store — persists for the lifetime of the server process.
# Keys: job_id (str)  Values: job dict (see _make_job)
_jobs: dict[str, dict] = {}

# Single background thread — one LLM call at a time (Ollama is single-threaded)
_executor = ThreadPoolExecutor(max_workers=1)


# ─────────────────────────────────────────────────────────────
#  Config loader (mirrors main.py exactly)
# ─────────────────────────────────────────────────────────────

def _load_config() -> dict:
    config_path = os.path.join(os.getcwd(), "configs", "config.yaml")
    with open(config_path, "r") as f:
        y = yaml.safe_load(f)
    return {
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
        "output_dir":           os.getenv("OUTPUT_DIR", "output"),
        "log_file":             os.getenv("LOG_FILE",   y["logging"]["log_file"]),
        "log_level":            y["logging"]["level"],
        "tesseract_path":       os.getenv("TESSERACT_PATH", "/usr/bin/tesseract"),
        "sqlite_db":            y["output"]["sqlite_db"],
        "vision_model":         os.getenv("VISION_MODEL",
                                          y["ollama"].get("vision_model",
                                                          y["ollama"]["model"])),
        "json_indent":          y["output"]["json_indent"],
    }


# Load config and set Tesseract path once at startup
_cfg = _load_config()
setup_logging(_cfg["log_file"], _cfg["log_level"])

_tess_path = _cfg["tesseract_path"]
if os.path.exists(_tess_path):
    pytesseract.pytesseract.tesseract_cmd = _tess_path


# ─────────────────────────────────────────────────────────────
#  Job helpers
# ─────────────────────────────────────────────────────────────

def _make_job(job_id: str, filename: str) -> dict:
    return {
        "job_id":     job_id,
        "filename":   filename,
        "status":     "queued",      # queued → processing → done | failed
        "submitted":  datetime.now(timezone.utc).isoformat(),
        "completed":  None,
        "result":     None,          # ExtractionResult dict when done
        "error":      None,
    }


def _run_job(job_id: str, pdf_path: str) -> None:
    """
    Executed in the background thread.

    Runs the full pipeline including vision fallback when composite
    confidence is below VISION_THRESHOLD — same behaviour as main.py.
    """
    from src.extraction.vision_fallback import run_vision_fallback, VISION_THRESHOLD

    job = _jobs[job_id]
    job["status"] = "processing"
    log.info("api_job_start", job_id=job_id, file=job["filename"])

    try:
        # ── 1. Copy uploaded temp file into sample_pdf/scan/ ──
        #    So it appears in the watched input directory and is
        #    tracked by the file tracker like a batch-mode file.
        scan_dir = os.path.join(os.getcwd(), "sample_pdf", "scan")
        os.makedirs(scan_dir, exist_ok=True)
        original_filename = job["filename"]
        scan_path = os.path.join(scan_dir, original_filename)
        # Avoid overwriting an existing file with the same name
        if os.path.exists(scan_path):
            stem, ext = os.path.splitext(original_filename)
            ts_suffix = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            scan_path = os.path.join(scan_dir, f"{stem}_{ts_suffix}{ext}")
        shutil.copy2(pdf_path, scan_path)
        log.info("api_copied_to_scan", job_id=job_id, dest=scan_path)

        # ── 2. Timestamps and run directory ───────────────────
        run_ts  = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        run_dir = os.path.join(_cfg["output_dir"], "processed", run_ts)
        os.makedirs(run_dir, exist_ok=True)

        # ── 3. Primary pipeline ───────────────────────────────
        raw = run_pipeline(scan_path, _cfg)
        raw["_pdf_path"] = os.path.abspath(scan_path)

        record  = raw.get("record")
        records = raw.get("records", [record] if record else [])

        # ── 4. Write primary outputs (JSON + CSV + SQLite) ────
        file_hash = compute_file_hash(scan_path)
        for r in records:
            r["FileHash"] = file_hash

        invoice_records, line_item_records = split_records(
            records, run_timestamp=run_ts
        )

        # JSON
        write_json(
            invoice_records,
            line_item_records,
            _cfg["output_dir"],
            run_ts,
            _cfg["json_indent"],
        )

        # CSV
        import pandas as pd
        def _write_csv(recs, path, cols):
            if not recs:
                return
            os.makedirs(os.path.dirname(path), exist_ok=True)
            rows = [{c: r.get(c) for c in cols} for r in recs]
            pd.DataFrame(rows, columns=cols).to_csv(
                path, index=False, encoding="utf-8-sig"
            )
            log.info("api_csv_written", path=path, rows=len(rows))

        _write_csv(invoice_records,
                   os.path.join(run_dir, "invoices.csv"),   _INV_COLS)
        _write_csv(line_item_records,
                   os.path.join(run_dir, "line_items.csv"), _LINE_COLS)

        # SQLite
        sql_path = os.path.join(run_dir, "dump.sql")
        if invoice_records or line_item_records:
            write_sqlite(invoice_records, line_item_records,
                         _cfg["sqlite_db"], sql_path)

        # Run summary
        import json as _json
        summary = {
            "run_timestamp": run_ts,
            "source":        "api",
            "job_id":        job_id,
            "filename":      original_filename,
            "invoices":      len(invoice_records),
            "line_items":    len(line_item_records),
        }
        with open(os.path.join(run_dir, "run_summary.json"), "w") as f:
            _json.dump(summary, f, indent=2)

        log.info("api_primary_outputs_written",
                 job_id=job_id, run_dir=run_dir,
                 invoices=len(invoice_records),
                 line_items=len(line_item_records))

        # ── 5. Vision fallback ────────────────────────────────
        comp_score = raw.get("confidence", {}).get("composite_score", 1.0)
        vision_dir = None

        if comp_score < VISION_THRESHOLD:
            log.info("api_vision_fallback_triggered",
                     job_id=job_id,
                     score=comp_score,
                     model=_cfg["vision_model"])

            run_vision_fallback(
                low_confidence_results=[raw],
                all_flat_records=records,
                cfg=_cfg,
                run_ts=run_ts,
                run_dir=run_dir,
            )
            vision_dir = os.path.join(run_dir, "vision")

        # ── 6. Build typed result ─────────────────────────────
        result = ExtractionResult.from_pipeline_result(raw)

        job["result"]          = result.model_dump()
        job["status"]          = "done"
        job["completed"]       = datetime.now(timezone.utc).isoformat()
        job["run_dir"]         = run_dir
        job["vision_dir"]      = vision_dir
        job["composite_score"] = comp_score
        job["output_files"] = {
            "invoices_json":    os.path.join(run_dir, "invoices.json"),
            "line_items_json":  os.path.join(run_dir, "line_items.json"),
            "invoices_csv":     os.path.join(run_dir, "invoices.csv"),
            "line_items_csv":   os.path.join(run_dir, "line_items.csv"),
            "sqlite_db":        _cfg["sqlite_db"],
            "run_summary":      os.path.join(run_dir, "run_summary.json"),
            "comparison_json":  os.path.join(vision_dir, "comparison.json") if vision_dir else None,
        }

        log.info("api_job_done",
                 job_id=job_id,
                 score=result.composite_score,
                 action=result.action,
                 vision_ran=(vision_dir is not None),
                 run_dir=run_dir)

    except Exception as e:
        job["status"]    = "failed"
        job["error"]     = str(e)
        job["completed"] = datetime.now(timezone.utc).isoformat()
        log.error("api_job_failed", job_id=job_id, error=str(e))
    finally:
        # Clean up temp file regardless of outcome
        try:
            os.remove(pdf_path)
        except OSError:
            pass


# ─────────────────────────────────────────────────────────────
#  Routes
# ─────────────────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
def root():
    """Redirect browser root to Swagger docs."""
    return RedirectResponse(url="/docs")


@app.get(
    "/health",
    summary="Health check",
    description="Checks that Tesseract is available and Ollama is reachable.",
    tags=["system"],
)
def health():
    """
    Returns 200 if both Tesseract and Ollama are reachable.
    Returns 503 with details if either is unavailable.
    """
    import requests

    status = {"tesseract": "unknown", "ollama": "unknown", "model": _cfg["ollama_model"]}
    problems = []

    # Tesseract check
    try:
        ver = pytesseract.get_tesseract_version()
        status["tesseract"] = f"ok (v{ver})"
    except Exception as e:
        status["tesseract"] = f"error: {e}"
        problems.append("tesseract")

    # Ollama check
    try:
        r = requests.get(
            f"{_cfg['ollama_base_url']}/api/tags",
            timeout=5,
        )
        r.raise_for_status()
        tags = [m["name"] for m in r.json().get("models", [])]
        model_loaded = _cfg["ollama_model"] in tags
        status["ollama"]       = "ok"
        status["models_loaded"] = tags
        status["model_ready"]   = model_loaded
        if not model_loaded:
            problems.append(f"model {_cfg['ollama_model']} not pulled yet")
    except Exception as e:
        status["ollama"] = f"error: {e}"
        problems.append("ollama")

    if problems:
        raise HTTPException(status_code=503, detail={"problems": problems, **status})

    return {"status": "ok", **status}


@app.post(
    "/extract",
    summary="Extract invoice data from a PDF",
    description=(
        "Upload a PDF invoice (digital or scanned). The pipeline runs asynchronously — "
        "this endpoint returns immediately with a `job_id`. "
        "Poll `GET /result/{job_id}` to retrieve the result.\n\n"
        "**Processing time:** 2–8 minutes on CPU (Ollama inference)."
    ),
    tags=["extraction"],
    status_code=202,
)
async def extract(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(..., description="PDF invoice file"),
):
    """
    Accept a PDF upload, queue it for extraction, return job_id immediately.

    The client should then poll GET /result/{job_id} until status == 'done'.
    """
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are accepted.")

    # Save upload to a temp file (pipeline needs a real path, not a file object)
    suffix = f"_{file.filename}"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = tmp.name

    job_id = str(uuid.uuid4())
    _jobs[job_id] = _make_job(job_id, file.filename)

    # Submit to background thread (non-blocking — request returns immediately)
    background_tasks.add_task(_run_job, job_id, tmp_path)

    log.info("api_job_queued", job_id=job_id, filename=file.filename)

    return {
        "job_id":   job_id,
        "status":   "queued",
        "filename": file.filename,
        "poll_url": f"/result/{job_id}",
        "message":  "Job queued. Poll /result/{job_id} for the result.",
    }


@app.get(
    "/result/{job_id}",
    summary="Poll job result",
    description="Returns the current status of an extraction job. Poll until `status == 'done'`.",
    tags=["extraction"],
)
def result(job_id: str):
    """
    Return job status and result.

    Possible status values:
    - **queued**     — waiting in queue
    - **processing** — pipeline is running
    - **done**       — extraction complete, result is in the response
    - **failed**     — pipeline error, check `error` field
    """
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found.")
    return job


@app.get(
    "/jobs",
    summary="List all jobs",
    description="Returns all jobs submitted in this server session (most recent first).",
    tags=["extraction"],
)
def list_jobs(
    status: Optional[str] = None,
    limit: int = 20,
):
    """
    List jobs, optionally filtered by status.

    Parameters
    ----------
    status : str, optional
        Filter by status: queued, processing, done, failed
    limit : int
        Max number of jobs to return (default 20)
    """
    jobs = list(reversed(list(_jobs.values())))
    if status:
        jobs = [j for j in jobs if j["status"] == status]
    return {
        "total":  len(_jobs),
        "shown":  min(len(jobs), limit),
        "jobs":   jobs[:limit],
    }
