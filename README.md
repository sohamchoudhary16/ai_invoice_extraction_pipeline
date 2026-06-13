# AI Invoice Extraction Pipeline
## Technical Architecture & Operations Documentation

**Version:** 3.0
**Classification:** Internal — Engineering & Executive
**Owner:** Data Engineering
**Schedule:** Weekly batch — every Monday 06:00 CET; also available on-demand via REST API
**Last Updated:** June 2026

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Business Context & Problem Statement](#2-business-context--problem-statement)
3. [Architecture Overview](#3-architecture-overview)
4. [Pipeline Stages — Detailed](#4-pipeline-stages--detailed)
5. [Vision Fallback — How It Works](#5-vision-fallback--how-it-works)
6. [FastAPI — Real-Time Extraction Layer](#6-fastapi--real-time-extraction-layer)
7. [Pydantic Data Models](#7-pydantic-data-models)
8. [Technology Decisions](#8-technology-decisions)
9. [Directory Structure](#9-directory-structure)
10. [Data Schema & Output](#10-data-schema--output)
11. [Operational Runbook](#11-operational-runbook)
12. [Docker & Kubernetes Deployment (WSL2 + k3d)](#12-docker--kubernetes-deployment)
13. [Monitoring & Observability](#13-monitoring--observability)
14. [Known Limitations & Roadmap](#14-known-limitations--roadmap)
15. [Extraction Quality Evaluation](#15-extraction-quality-evaluation)
16. [Automated Test Suite](#16-automated-test-suite)

---

## 1. Executive Summary

The AI Invoice Extraction Pipeline is an automated document processing system that ingests PDF invoice documents, extracts structured financial data using a combination of Optical Character Recognition (OCR) and a locally-hosted Large Language Model (LLM), and writes the results to a normalised database for downstream consumption.

The pipeline has **two operating modes**: a weekly batch run (every Monday at 06:00 CET) that processes all new invoices in the input directory, and an **on-demand REST API** (FastAPI, port 8000) that accepts a single PDF upload, processes it asynchronously, and returns a fully typed JSON result. Both modes share the same pipeline core and produce identical outputs.

**What it solves:** Manual invoice data entry is slow, error-prone, and scales poorly. This pipeline eliminates manual extraction of header fields (seller, buyer, amounts, VAT IDs, IBANs) and line items (products, quantities, unit prices, discounts) from PDF invoices, including scanned documents with handwritten stamps and annotations.

### v3 Changes vs v2

| Area | v2 | v3 |
|---|---|---|
| Operating modes | Batch only (`python -m src.main`) | Batch + **FastAPI REST API** (`uvicorn app.api:app`) |
| API entry point | Not present | `app/api.py` — 4 endpoints: `/extract`, `/result/{job_id}`, `/jobs`, `/health` |
| On-demand submission | Not present | `submit_invoice.bat` — one-line PDF upload via `curl` |
| Job polling | Not present | `poll_job.py` — live status polling with formatted result display |
| Log monitoring | Manual `tail` | `watch_logs.bat` — live log streaming via PowerShell |
| API server launcher | Not present | `run_api.bat` — starts `uvicorn` with one double-click |
| Pydantic model layer | Implicit (schema.py only) | `src/models/invoice.py` — typed `InvoiceRecord`, `LineItem`, `ExtractionResult` |
| Schema source of truth | Hardcoded list in `schema.py` | Derived from `InvoiceRecord.model_fields` — single source of truth |

### v2 Changes vs v1

| Area | v1 | v2 |
|---|---|---|
| LLM model | `llama3.2:3b` (text-only) | `gemma3:4b` (text + vision) |
| Low-confidence fallback | LLM re-extraction on OCR text | Vision fallback — raw page images sent to `gemma3:4b` |
| Vision fallback module | Not present | `src/extraction/vision_fallback.py` |
| Output structure | Single output set per run | Primary output + `vision/` subdirectory for flagged documents |
| Comparison report | Not present | `vision/comparison.json` — field-by-field diff |
| Confidence routing trigger | `score < 0.85` | Primary routing unchanged; vision fallback at `score < 0.80` |

**Key metrics at a glance:**

| Metric | Value |
|---|---|
| Processing cadence | Weekly batch (Monday 06:00 CET) **or** on-demand via API |
| Supported input formats | Digital PDF, scanned PDF |
| Average processing time | 2–5 min per doc (primary) + 10–15 min vision pass if triggered |
| OCR confidence threshold | 60% (words below this → LLM re-extraction) |
| Vision fallback threshold | Composite score < 0.80 |
| Output formats | JSON, CSV, SQLite |
| Quarantine handling | Automatic (corrupt / encrypted files) |
| Incremental processing | Yes (SHA-256 file hash deduplication) |
| API mode | FastAPI on port 8000 — async job queue, Swagger UI at `/docs` |

---

## 2. Business Context & Problem Statement

### 2.1 The Problem

Encory receives structured and semi-structured invoice documents from vendors and service providers. These documents arrive as PDF files of which two distinct categories exist:

**Category A — Digital PDFs:** Computer-generated invoices where the text layer is selectable. Text can be extracted directly without image processing.

**Category B — Scanned PDFs:** Physical documents scanned to PDF, or digital documents with graphical annotations overlaid (approval stamps, handwritten reviewer notes). These require image preprocessing and OCR before any structured extraction is possible.

Both categories may contain:

- Diagonal watermark stamps obscuring underlying text
- Handwritten annotations over printed fields
- Partially truncated line item descriptions
- European number formatting (1.234,56 notation)
- German and English mixed-language content

### 2.2 The Solution Approach

The pipeline implements a **tiered confidence extraction strategy**:

1. Fast, deterministic Tesseract OCR is attempted first on every document
2. If OCR confidence is acceptable (≥ 80%), the extracted text is passed to `gemma3:4b` for structured field extraction
3. If the resulting **composite confidence score** falls below 0.80, the document is re-processed by sending the **raw page image** directly to the `gemma3:4b` vision model — bypassing the text entirely
4. Both the primary and vision extractions are written to output, with a field-by-field comparison report to evaluate which approach produced better results

This design minimises processing time and cost while maximising accuracy on difficult documents, and it produces a comparison dataset for ongoing quality measurement.

---

## 3. Architecture Overview

### 3.1 High-Level Pipeline Flow

```
┌─────────────────────────────────────────────────────────────────────────┐
│                     WEEKLY BATCH RUN — Monday 06:00 CET                 │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                    ┌───────────────┴──────────────────┐
                    │         INPUT DIRECTORY          │
                    │       sample_pdf/scan/*.pdf      │
                    └───────────────┬──────────────────┘
                                    │
                    ┌───────────────▼───────────────────┐
                    │         INGESTION LAYER           │
                    │  SHA-256 hash → already done? SKIP│
                    │  Integrity pre-flight checks      │
                    └───────┬───────────────────────────┘
                            │
             ┌──────────────┴──────────────┐
             │                             │
      Load failed                    Load OK
             │                             │
             ▼                             ▼
      ┌────────────┐        ┌──────────────────────────┐
      │ QUARANTINE │        │    PREPROCESSING LAYER   │
      │ output/    │        │  Colour overlay removal  │
      │ quarantine/│        │  Denoise + binarize      │
      └────────────┘        └──────────────┬───────────┘
                                           │
                            ┌──────────────▼───────────┐
                            │       OCR LAYER          │
                            │  Tesseract deu+eng       │
                            │  300 DPI, PSM 6          │
                            └──────────────┬───────────┘
                                           │
                            ┌──────────────▼────────────┐
                            │   CONFIDENCE EVALUATION   │
                            └──────┬───────────┬────────┘
                                   │           │
                             >= 80%            < 80%
                                   │           │
                            ┌──────▼──┐  ┌────▼───────────────────┐
                            │ PASS    │  │  LLM RE-EXTRACTION     │
                            │ THROUGH │  │  gemma3:4b (text mode) │
                            └──────┬──┘  └────────┬───────────────┘
                                   └──────┬───────┘
                                          │
                            ┌─────────────▼────────────┐
                            │  NORMALISE + VALIDATE    │
                            │  Dates, floats, IBAN     │
                            │  Tax math, regex checks  │
                            └─────────────┬────────────┘
                                          │
                            ┌─────────────▼────────────┐
                            │  COMPOSITE CONFIDENCE    │
                            │  SCORING + ROUTING       │
                            └──────┬──────────┬────────┘
                                   │          │
                       score >= 0.85     score < 0.85
                                   │          │
                            ┌──────▼──┐  ┌───▼───────────┐
                            │  AUTO   │  │ REVIEW QUEUE  │
                            │ ACCEPT  │  │ output/       │
                            └──────┬──┘  │ review_queue/ │
                                   │     └───────────────┘
                                   ▼
                    ┌──────────────────────────────────┐
                    │          OUTPUT LAYER            │
                    │  invoices table (1 row/invoice)  │
                    │  invoice_line_items (1 row/item) │
                    │  Timestamped JSON + CSV + SQL    │
                    └──────────────────────────────────┘
                                   │
                    ┌──────────────▼──────────────────────────────┐
                    │   VISION FALLBACK (additive, post-pipeline) │
                    │   Triggered when composite_score < 0.80     │
                    │   Raw page image → gemma3:4b (vision mode)  │
                    │   Writes vision/ subdirectory + comparison  │
                    └─────────────────────────────────────────────┘
```

### 3.2 Composite Confidence Scoring Model

The pipeline computes a **composite confidence score** (0.0–1.0) from five weighted signals. This score determines the routing decision for each extracted record.

| Signal | Weight | What it measures |
|---|---|---|
| OCR confidence | 20% | Average Tesseract word confidence across the page |
| Field extraction confidence | 30% | LLM-reported confidence per extracted field |
| Validation pass rate | 25% | Percentage of business rule checks that passed |
| Required field completeness | 15% | Percentage of mandatory fields with non-null values |
| Guardrail issues | 10% | Penalty for injection attempts or unknown fields |

**Routing decisions:**

| Score range | Tier | Action |
|---|---|---|
| >= 0.85 | High | Auto-accept — written to output immediately |
| 0.65–0.84 | Medium | Partial review — written and flagged |
| 0.40–0.64 | Low | Human review — full review package generated |
| < 0.40 | Invalid | Reject — review queue only, not written to main output |
| < 0.80 | Any | Additionally triggers vision fallback (additive) |

---

## 4. Pipeline Stages — Detailed

### Stage 1: Ingestion & Pre-flight

Every PDF in the input directory is checked before processing begins.

**File hash check (incremental processing):** A SHA-256 hash is computed from the file's byte content. If this hash already exists in the `processing_status` table with status `"success"`, the file is skipped.

**Integrity checks performed:**

- File exists and is non-empty (> 512 bytes)
- File extension is `.pdf`
- PDF opens without error
- PDF is not password-encrypted
- Page count is between 1 and 200
- At least one page has content

**Outcome:** Valid file proceeds. Invalid file is quarantined.

### Stage 2: Quarantine

Files that fail pre-flight checks are copied to `output/quarantine/` with a timestamped filename and a plain-text sidecar explaining the reason.

| Reason | Cause | Remediation |
|---|---|---|
| `ENCRYPTED` | Password-protected PDF | Decrypt before re-submitting |
| `CORRUPT` | File cannot be opened | Request fresh copy from sender |
| `TOO_LARGE` | Exceeds 200-page limit | Split into individual invoice files |
| `TOO_SMALL` | File smaller than 512 bytes | File is likely incomplete |
| `UNSUPPORTED_TYPE` | Not a PDF | Convert to PDF format |
| `ZERO_PAGES` | PDF opens but has no pages | File is malformed |

### Stage 3: Image Preprocessing

For scanned PDFs, each page is rendered to PNG at 300 DPI and passed through a preprocessing pipeline before OCR.

**Preprocessing steps in order:**

1. **Colour overlay suppression:** Pixels with high colour saturation and mid-range brightness are set to white. Invoice text is black; background is white; stamps are coloured. This separates stamp pixels from text pixels using Pillow channel arithmetic — no ML required.
2. **Denoise:** A median filter smooths scan noise (dust, compression artefacts) without blurring text edges.
3. **Binarization:** Greyscale conversion and autocontrast increases the contrast ratio between text and background, improving Tesseract accuracy on faded scans.
4. **Border removal:** An 8-pixel border crop removes scan artefacts common at page edges.

**Why this matters:** Sample invoices showed diagonal coloured annotations that reduced raw OCR confidence to 52–72%. After preprocessing, confidence recovered to 66–88%.

### Stage 4: OCR

Tesseract processes the preprocessed image and returns word-level text with bounding box coordinates and confidence scores (0–100) for each word.

| Parameter | Value | Reason |
|---|---|---|
| Language | `deu+eng` | German + English mixed invoices |
| Page Segmentation Mode | `6` | Uniform text block — optimal for business documents |
| Render DPI | 300 | Standard for high-quality OCR |
| OEM | `3` | LSTM neural network engine (most accurate) |

### Stage 5: LLM Extraction

The cleaned OCR text is passed to `gemma3:4b` via Ollama with a structured extraction prompt. The model returns a flat JSON object mapping schema field names to extracted values.

**Model: `gemma3:4b`** — Google Gemma 3, 4-billion-parameter model. Used in **text mode** for the primary pipeline (receiving OCR text) and in **vision mode** for the fallback (receiving raw page images). A single model handles both roles, eliminating the need to run and manage two separate models.

**Prompt design principles:**

- OCR text is wrapped in `<DOC>` delimiters and labelled as untrusted data to prevent prompt injection
- Explicit field disambiguation rules distinguish seller, buyer, and ship-to address blocks
- European number format (1.234,56 = 1234.56) is explained
- Temperature is set to 0 for deterministic, reproducible output

**Injection protection:** An input sanitiser scans OCR text for prompt injection patterns before LLM submission. Phrases such as "Ignore all previous instructions" are redacted and flagged in the log.

**Retry logic:** The LLM client retries up to 3 times with 5-second backoff on timeout or connection failure.

### Stage 6: Normalisation

Raw extracted values are coerced to correct types.

- **Float fields:** European decimal notation detected and converted. Currency symbols stripped.
- **Date fields:** Multiple formats (DD.MM.YYYY, YYYY-MM-DD, YYYYMMDD) parsed and normalised to ISO 8601.
- **IBAN:** Spaces removed. Stored as a continuous 22-character string.
- **Line items:** String values like "22,200.00" parsed to floats. Percentage strings like "19%" converted to numeric 19.

### Stage 7: Validation

Business rules are applied to the normalised record. Errors are collected but do not block the pipeline.

| Check | Rule |
|---|---|
| Required fields present | InvoiceId, IssueDate, SellerName, InvGrandTotal, Currency must be non-null |
| Grand total positive | InvGrandTotal must be > 0 |
| Tax mathematics | TaxBasisTotal + TaxTotal ≈ GrandTotal (±€0.06 tolerance) |
| Line total mathematics | NetPrice × BilledQuantity ≈ LineTotalAmount |
| Date ordering | DueDate must be >= IssueDate |
| IBAN format | German IBAN: DE + 2 check digits + 18 alphanumeric = 22 total |
| VAT ID format | German VAT: DE + exactly 9 digits |
| BIC format | 8 or 11 character SWIFT code |
| Currency code | Must be a recognised ISO 4217 code |
| Numeric field types | All declared float columns must be parseable numbers |

### Stage 8: Record Splitting & Output

Records are split from a flat structure into two normalised tables before writing. Invoice header data (seller name, buyer address, IBAN, grand total) is identical for every line item within an invoice — a flat table repeats this data once per line item, causing double-counting in downstream aggregation. The two-table design eliminates this redundancy.

---

## 5. Vision Fallback — How It Works

### 5.1 Why a Vision Fallback Exists

The primary pipeline passes OCR text to `gemma3:4b`. When OCR quality is poor (stamps, faded print, complex layouts), the text fed to the LLM is already corrupted — the model can only work with what Tesseract gave it, and cannot recover words that were misread or missed entirely.

The vision fallback bypasses this limitation entirely: it sends the **raw page image** directly to `gemma3:4b`'s vision encoder. The model reads the invoice as a human would — from pixels, not pre-processed text.

### 5.2 Trigger Condition

After the primary pipeline completes all documents, `main.py` checks which results have `composite_score < 0.80`. These documents are collected into `low_conf_results` and passed to `run_vision_fallback()`.

```
composite_score < 0.80  →  document added to low_conf_results
                           vision fallback runs after primary pipeline
```

> **Note:** All 3 sample invoices currently score below 0.80 (scores: 0.535, 0.532, 0.515), meaning all three trigger the vision fallback on every run. This results in 6 LLM calls total per run instead of 3, adding approximately 10–15 minutes to the total run time on CPU. This is acceptable for a weekly batch.

### 5.3 What the Vision Fallback Does

The module is defined in `src/extraction/vision_fallback.py`. It is **additive only** — the primary pipeline output is never modified.

**Step by step:**

1. Each low-confidence PDF is rendered to PNG at **150 DPI** using PyMuPDF (lower than OCR's 300 DPI — vision models read the full image context, not individual characters)
2. Each page image is base64-encoded and sent to `gemma3:4b` via the Ollama `/api/chat` endpoint with the image attached
3. The model returns a JSON object with all extracted invoice fields
4. Results across pages are merged (first non-null value wins for header fields; line items accumulated across all pages)
5. A field-by-field comparison is built against the primary OCR+LLM record

**Ollama vision API call format:**

```python
{
    "model": "gemma3:4b",
    "stream": False,
    "format": "json",
    "options": {"temperature": 0, "seed": 42},
    "messages": [
        {"role": "system", "content": VISION_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": VISION_EXTRACTION_PROMPT,
            "images": [base64_page_image],   # raw image, not OCR text
        },
    ],
}
```

### 5.4 Vision Output Files

A `vision/` subdirectory is created inside the run's timestamped output folder whenever the fallback runs:

```
output/processed/20260609T060000Z/
├── invoices.json              ← primary OCR+LLM output (unchanged)
├── line_items.json            ← primary OCR+LLM output (unchanged)
├── invoices.csv
├── line_items.csv
├── dump.sql
├── run_summary.json
└── vision/                    ← NEW — only created when score < 0.80
    ├── invoices.json          ← vision model extraction results
    ├── line_items.json        ← vision model line items
    ├── invoices.csv
    ├── line_items.csv
    └── comparison.json        ← field-by-field diff
```

### 5.5 The comparison.json File

This is the key diagnostic output. For each low-confidence document it records:

```json
{
  "source_file": "invoice_v2_01.pdf",
  "ocr_composite_score": 0.535,
  "ocr_llm_model": "gemma3:4b",
  "vision_model": "gemma3:4b",
  "field_comparison": {
    "agreed": {
      "InvoiceId": "INV-2024-00421",
      "Currency": "EUR"
    },
    "disagreed": {
      "InvGrandTotal": {
        "ocr_llm": 38413.20,
        "vision":  38413.00
      }
    },
    "ocr_only": {
      "SellerPhone": "+49 30 123456"
    },
    "vision_only": {
      "BuyerEmail": "accounts@buyer.de"
    }
  },
  "summary": {
    "agreed":      18,
    "disagreed":    3,
    "ocr_only":     2,
    "vision_only":  1
  }
}
```

**How to use this report:** Compare `agreed` vs `disagreed` counts across multiple runs. If vision consistently extracts fields that OCR+LLM misses (`vision_only`), or corrects values the primary pipeline got wrong (`disagreed`), that is the signal to weight vision results higher or replace the primary approach for that document category.

### 5.6 ExtractionMethod Field

Every record written to output carries an `ExtractionMethod` tag so downstream systems can distinguish how it was produced:

| Value | Meaning |
|---|---|
| `llm_ocr` | Primary pipeline: Tesseract text → gemma3:4b |
| `vision_gemma3:4b` | Vision fallback: raw page image → gemma3:4b |

---

## 6. FastAPI — Real-Time Extraction Layer

### 6.1 Overview

`app/api.py` wraps the pipeline as a **REST API** (a web interface where other systems or users can submit invoices programmatically without running Python directly). It is a thin layer — the same `run_pipeline()` function used by `main.py` runs here unchanged. The difference is the trigger mechanism: instead of scanning a folder on a schedule, the API accepts a PDF upload via HTTP.

The server is started with:

```bash
uvicorn app.api:app --reload --port 8000
```

Or on Windows, by double-clicking `run_api.bat`.

Once running, the **Swagger UI** (an interactive web page that documents all endpoints and lets you test them with file uploads directly in the browser) is available at:

```
http://localhost:8000/docs
```

### 6.2 The Four Endpoints

#### `POST /extract` — Submit an invoice

Accepts a PDF file as a multipart form upload. Returns immediately with a `job_id` — processing happens in the background. HTTP status `202 Accepted`.

```bash
# Using curl (or use submit_invoice.bat — see Section 11.5)
curl -X POST http://localhost:8000/extract \
  -F "file=@sample_pdf/scan/invoice_v3_01.pdf"
```

Response:

```json
{
  "job_id":   "3f2a1b4c-8d9e-4f2a-b1c3-2d4e5f6a7b8c",
  "status":   "queued",
  "filename": "invoice_v3_01.pdf",
  "poll_url": "/result/3f2a1b4c-8d9e-4f2a-b1c3-2d4e5f6a7b8c",
  "message":  "Job queued. Poll /result/{job_id} for the result."
}
```

**What happens behind the scenes:**
1. The uploaded file is saved to a temporary path
2. A UUID `job_id` is generated and a job entry is created in `_jobs` (the in-memory job store)
3. `background_tasks.add_task(_run_job, job_id, tmp_path)` hands the work to FastAPI's background task system
4. The HTTP response returns immediately — the caller does not wait for the pipeline
5. The background task runs `run_pipeline()`, then `run_vision_fallback()` if the score is below the threshold, then writes all outputs (JSON, CSV, SQLite) and updates the job status to `"done"`
6. The uploaded temp file is deleted from disk after processing regardless of success or failure

> **Single-threaded by design:** A `ThreadPoolExecutor(max_workers=1)` ensures only one LLM call runs at a time. Ollama is single-threaded — submitting two invoices simultaneously would deadlock. The second job waits in the `queued` state until the first completes.

#### `GET /result/{job_id}` — Poll for result

Returns the current state of a job. Poll this endpoint until `status == "done"`.

```bash
curl http://localhost:8000/result/3f2a1b4c-8d9e-4f2a-b1c3-2d4e5f6a7b8c
```

**Status lifecycle:** `queued` → `processing` → `done` | `failed`

When `status == "done"`, the response includes:

```json
{
  "job_id":    "3f2a1b4c-...",
  "filename":  "invoice_v3_01.pdf",
  "status":    "done",
  "submitted": "2026-06-09T10:21:44Z",
  "completed": "2026-06-09T10:29:55Z",
  "composite_score": 0.535,
  "result": {
    "source_file":       "invoice_v3_01.pdf",
    "ok":                true,
    "composite_score":   0.535,
    "confidence_tier":   "low",
    "action":            "human_review",
    "validation_errors": ["TAX_MATH: ...", "INVALID_IBAN: ..."],
    "invoice": {
      "InvoiceId":     "INV-2024-00421",
      "SellerName":    "XYZcompany",
      "InvGrandTotal": 10290.0,
      "IssueDate":     "2024-03-15",
      "DueDate":       "2024-04-14",
      "Iban":          "021000021",
      "Bic":           "CHASUS33"
    },
    "line_items": [ {...} ],
    "queued_for_review": true,
    "review_reasons": ["confidence_action=human_review", "validation_errors=4"]
  },
  "output_files": {
    "invoices_json":   "output/processed/20260609T102955Z/invoices.json",
    "line_items_json": "output/processed/20260609T102955Z/line_items.json",
    "sqlite_db":       "output/extractions.db",
    "comparison_json": "output/processed/20260609T102955Z/vision/comparison.json"
  }
}
```

When `status == "failed"`, the response includes an `error` field with the exception message.

#### `GET /jobs` — List all jobs

Returns all jobs submitted in the current server session (most recent first), with optional `?status=done|failed|processing|queued` filter and `?limit=N` cap.

```bash
curl "http://localhost:8000/jobs?status=failed&limit=5"
```

#### `GET /health` — Liveness check

Verifies that both Tesseract and Ollama are reachable before processing a job. Returns `200 OK` if healthy, `503 Service Unavailable` with details if either is down.

```bash
curl http://localhost:8000/health
```

Healthy response:

```json
{
  "status":       "ok",
  "tesseract":    "ok (v5.3.1)",
  "ollama":       "ok",
  "model":        "gemma3:4b",
  "models_loaded": ["gemma3:4b"],
  "model_ready":  true
}
```

Unhealthy response (HTTP 503):

```json
{
  "detail": {
    "problems":  ["ollama"],
    "tesseract": "ok (v5.3.1)",
    "ollama":    "error: Connection refused"
  }
}
```

### 6.3 How the API Differs from Batch Mode

| Aspect | Batch mode (`src/main.py`) | API mode (`app/api.py`) |
|---|---|---|
| Trigger | Cron / manual CLI | HTTP `POST /extract` |
| Input | Folder scan (`sample_pdf/scan/`) | Single file upload per request |
| Deduplication | SHA-256 + `processing_status` table | Same — file is copied to `sample_pdf/scan/` and hash-checked |
| Vision fallback | Runs after all docs complete | Runs per-job in background thread |
| Output | Timestamped directory + `latest/` | Same directory structure; path returned in `output_files` |
| Concurrency | Sequential (one doc at a time) | Sequential (single worker thread); subsequent uploads queue |
| Job tracking | `run_summary.json` per run | In-memory `_jobs` dict; cleared on server restart |

### 6.4 `run_api.bat` — Starting the Server

```bat
@echo off
echo Starting Invoice Extraction API...
echo.
echo Make sure Ollama is already running (ollama serve in another window)
echo.
cd /d "%~dp0"
uvicorn app.api:app --reload --port 8000
```

**Usage:** Double-click `run_api.bat` from the project root. This starts the FastAPI server with `--reload` (auto-restarts when source files change — useful during development). The Swagger UI is available immediately at `http://localhost:8000/docs`.

**Pre-requisite:** Ollama must already be running in a separate terminal (`ollama serve`) before the API server is started. The `/health` endpoint confirms this.

### 6.5 `submit_invoice.bat` — Submitting a PDF via Command Line

```bat
@echo off
REM Usage: submit_invoice.bat path\to\invoice.pdf
REM Example: submit_invoice.bat sample_pdf\scan\invoice_v3_01.pdf

set PDF=%1
if "%PDF%"=="" (
    echo Usage: submit_invoice.bat path\to\invoice.pdf
    exit /b 1
)

echo Submitting %PDF% to extraction API...
echo.

curl -s -X POST http://localhost:8000/extract ^
  -F "file=@%PDF%" | python -m json.tool

echo.
echo Copy the job_id above, then run:
echo   curl http://localhost:8000/result/YOUR_JOB_ID
```

**Usage:**

```bat
submit_invoice.bat sample_pdf\scan\invoice_v3_01.pdf
```

The script calls `curl` to POST the file, pipes the JSON response through `python -m json.tool` for pretty-printing, and prints instructions for the polling command. The `python -m json.tool` call requires no external package — it is part of Python's standard library.

### 6.6 `poll_job.py` — Live Job Status Polling

`poll_job.py` provides a continuously-updating status view. It polls `GET /result/{job_id}` every 10 seconds and prints live progress until the job completes or fails.

**Usage:**

```bash
python poll_job.py 3f2a1b4c-8d9e-4f2a-b1c3-2d4e5f6a7b8c
```

**Example output during processing:**

```
Polling: http://localhost:8000/result/3f2a1b4c-...
Press Ctrl+C to stop

[   0s]  status=queued       file=invoice_v3_01.pdf
[  10s]  status=processing   file=invoice_v3_01.pdf
[  20s]  status=processing   file=invoice_v3_01.pdf
...
[ 240s]  status=done         file=invoice_v3_01.pdf

=======================================================
  EXTRACTION COMPLETE
=======================================================
  InvoiceId    : INV-2024-00421
  SellerName   : XYZcompany
  BuyerName    : ABC Company LLC
  IssueDate    : 2024-03-15
  GrandTotal   : 10290.0 USD
  IBAN         : 021000021
  Score        : 0.535  (low)
  Action       : human_review
  Val. errors  : ['TAX_MATH: ...', 'INVALID_IBAN: ...']
  Warnings     : none

  Line items   : 1
    1. Al Pipeline Development  qty=5.0  total=10290.0
=======================================================

Full result saved to:  output/processed/latest/invoices.json
```

If the API server is not reachable (e.g. uvicorn not started), the script catches `ConnectionError`, prints a warning, waits 5 seconds, and retries — it does not crash.

### 6.7 `watch_logs.bat` — Live Log Streaming

```bat
@echo off
echo Watching pipeline logs (Ctrl+C to stop)...
echo.
powershell -Command "Get-Content logs\logs.txt -Wait -Tail 30"
```

**Usage:** Double-click `watch_logs.bat` while the API (or batch pipeline) is running. PowerShell's `Get-Content -Wait` is the Windows equivalent of `tail -f` — it shows the last 30 lines and then prints each new line as it is written to `logs/logs.txt`. Useful for watching LLM inference progress in real time.

### 6.8 Typical End-to-End API Workflow

```
Terminal 1              Terminal 2              Terminal 3
──────────────          ──────────────          ───────────────────
ollama serve            run_api.bat             submit_invoice.bat
                        (starts uvicorn)          invoice_v3_01.pdf
                        Swagger: /docs
                                                python poll_job.py
                                                  JOB_ID
                        [background thread      watch_logs.bat
                         processing...]           (optional)

                        status: queued
                        status: processing
                        ...
                        status: done            ← formatted result
                                                   printed by poll
```

---

## 7. Pydantic Data Models

### 7.1 Overview

`src/models/invoice.py` defines three Pydantic v2 models that provide a typed, validated representation of every data object the pipeline produces. Pydantic is a Python library that enforces data types at runtime — if a field is declared as `Optional[float]` and the pipeline passes the string `"1.234,56"`, Pydantic automatically coerces it to `1234.56` via the field validator.

These models serve four purposes:
1. **Type safety** — downstream code (the API, the evaluation script) works with typed objects, not raw dicts
2. **A second normalisation pass** — field validators on the models duplicate key coercions from `field_normalizer.py`, so type errors are caught even if the normaliser is bypassed
3. **Single schema source of truth** — `schema.py` derives `COLUMNS` from `InvoiceRecord.model_fields`, meaning the SQLite table, CSV headers, and Pydantic model are always in sync
4. **API response typing** — FastAPI uses `ExtractionResult` to generate the Swagger schema automatically

### 7.2 `LineItem`

Maps one-to-one with a row in the `invoice_line_items` SQLite table.

**Fields:** `SourceFile`, `InvoiceId`, `LineId`, `ProductName`, `ProductDescription`, `BilledQuantity` (float), `BilledUnit`, `NetPrice` (float), `DiscountAmount` (float), `LineTotalAmount` (float), `TaxRatePercent` (float), `TaxCategory`, `TaxType`, `BillingStartDate`, `DiscountIndicator`, `DiscountReason`.

**Validators:**
- `coerce_float` — all five numeric fields accept raw LLM strings (`"22,200.00"`, `"19%"`) and coerce them to `float`
- `coerce_date` — `BillingStartDate` accepts any supported date format and normalises to ISO

**Post-init check:** After all fields are validated, `model_post_init` runs a soft line-math check (`NetPrice × BilledQuantity ≈ LineTotalAmount`). If the check fails, a `line_math_warning` string is set on the object — this is excluded from serialisation and used only for logging.

### 7.3 `InvoiceRecord`

Maps one-to-one with a row in the `invoices` SQLite table. Contains all ~50 invoice header fields plus pipeline quality metadata.

**A note on underscore field names:** Pydantic v2 forbids field names beginning with `_`. The pipeline quality fields (`_confidence_score`, `_action`, `_is_valid`, etc.) arrive in raw dicts with leading underscores. These are declared in `InvoiceRecord` without the underscore and mapped via `Field(alias="_confidence_score")`. The `to_storage_dict()` method re-adds the underscores before writing to SQLite, so the storage layer needs no changes.

**Validators:**
- `coerce_date` — `IssueDate` and `DueDate` normalised to ISO
- `coerce_float` — all 10 financial total fields coerced from string
- `normalise_iban` — strips spaces, uppercases
- `normalise_currency` — strips whitespace, uppercases

**Post-init checks** (`model_post_init`) — the same 5 business rule checks from `business_rules.py` are re-run at the Pydantic level as a second validation pass:
- Tax math (`TaxBasisTotal + TaxTotal ≈ GrandTotal`)
- Date ordering (`DueDate >= IssueDate`)
- IBAN format (German pattern)
- VAT ID format (German pattern)
- BIC format
- Currency allowlist

These produce `format_warnings` (soft — pipeline continues) rather than errors (hard — would block). The warnings are accessible via `invoice.get_format_warnings()` and are passed through to the `ExtractionResult`.

**Schema source of truth:** `schema.py` imports `InvoiceRecord` and derives `COLUMNS = [k for k in InvoiceRecord.model_fields]`. This means adding a field to `InvoiceRecord` automatically adds it to the SQLite table schema, CSV headers, and column definitions everywhere — no manual synchronisation needed.

### 7.4 `ExtractionResult`

The top-level result object for one PDF. Used by the API endpoint (`/result/{job_id}`) and the evaluation script.

**Fields:**

| Field | Type | Description |
|---|---|---|
| `source_file` | str | Original PDF filename |
| `ok` | bool | Whether the pipeline completed without error |
| `error` | Optional[str] | Exception message if `ok=False` |
| `doc_kind` | Optional[str] | `"digital"`, `"scanned"`, or `"mixed"` |
| `total_pages` | Optional[int] | Page count |
| `invoice` | Optional[InvoiceRecord] | Typed invoice header |
| `line_items` | list[LineItem] | All extracted line items |
| `avg_ocr_confidence` | Optional[float] | Mean Tesseract confidence across pages |
| `composite_score` | Optional[float] | 0.0–1.0 routing score |
| `confidence_tier` | Optional[str] | `high`, `medium`, `low`, `invalid` |
| `action` | Optional[str] | `auto_accept`, `partial_review`, `human_review`, `reject` |
| `validation_errors` | list[str] | Business rule failures |
| `format_warnings` | list[str] | Pydantic soft warnings |
| `queued_for_review` | bool | Whether a review package was written |
| `review_reasons` | list[str] | Why the document was queued |

**`from_pipeline_result(cls, result: dict)`** — a class method that builds an `ExtractionResult` from the raw dict returned by `run_pipeline()`. This is the bridge between the dict-based pipeline internals and the typed model layer. The API calls this after `run_pipeline()` completes.

---

## 8. Technology Decisions

### 6.1 Why gemma3:4b instead of llama3.2:3b?

**Decision:** Replace `llama3.2:3b` with `gemma3:4b` as the single model for both text and vision extraction.

| Factor | llama3.2:3b | gemma3:4b | Decision |
|---|---|---|---|
| Vision capability | No — text only | Yes — multimodal | gemma3:4b required for vision fallback |
| Parameter count | 3B | 4B | Marginal size increase, acceptable |
| CPU inference time | 180–360s/page | ~similar | No significant regression |
| Single model for both roles | No — needs separate vision model | Yes | gemma3:4b wins — simpler ops |
| Memory requirement | ~3GB RAM | ~4GB RAM | Both fit within 16GB system |

The critical benefit: running one model (`gemma3:4b`) in two modes eliminates the need to pull, manage, and switch between two separate Ollama models. The same endpoint handles both the primary text extraction and the vision fallback.

### 6.2 Why send the image to the vision model, not the OCR text?

When Tesseract OCR confidence is low, the extracted text is already corrupted — words are misread, fields are merged or split incorrectly, and table structure is lost. Passing corrupted text to an LLM for "re-extraction" only reformats the corruption; it cannot recover information that was never correctly read.

Sending the **raw page image** to a vision model gives the model access to the original pixel data. It can read text directly from the image, understand spatial layout (which number belongs to which column), and handle stamps, rotated text, and overlapping annotations that confuse OCR entirely.

The key architectural rule: **if OCR failed, do not pass OCR output downstream — pass the image.**

### 6.3 Why Tesseract + Local LLM instead of Azure Document Intelligence?

| Factor | Azure Cloud | Local (Tesseract + Ollama) | Decision |
|---|---|---|---|
| Cost | Pay per page | Free | Local wins for MVP |
| Data privacy | Invoice data leaves Encory infrastructure | Data never leaves the machine | Local wins for confidential data |
| Latency | 3–5 seconds per page | 2–8 minutes per page (CPU) | Cloud wins — acceptable for weekly batch |
| Setup | Azure subscription + API keys | One-time local install | Local easier for MVP |

**Migration path:** The Tesseract and Ollama calls are isolated in `src/ocr/tesseract_runner.py` and `src/extraction/model_client_ollama.py`. Replacing them with Azure SDK calls requires changing only those two files. The vision fallback in `vision_fallback.py` would be replaced by Azure Document Intelligence's Layout API.

### 6.4 Why SQLite instead of PostgreSQL?

SQLite is zero-infrastructure — a single file managed natively by Python. The schema and queries are identical to PostgreSQL. **Migration trigger:** move to PostgreSQL when concurrent access, daily volumes above ~500 files, or network connectivity to the database are required.

### 6.5 Why not use LangChain for chunking?

LangChain's chunking is designed for RAG over large document corpora. Invoice extraction is a different problem: a single document, a single structured extraction prompt, a single JSON response. Chunking an invoice splits tables and field-value pairs across chunk boundaries, destroying the spatial relationships the LLM needs. A direct 4-line HTTP call to Ollama provides full control with no added complexity.

---

## 9. Directory Structure

```
v3_ai_invoice_extraction_pipeline/
│
├── app/                                  FastAPI REST API layer (v3)
│   └── api.py                            4 endpoints: /extract, /result, /jobs, /health
│
├── src/                                  All Python source code
│   ├── main.py                           Entry point — python -m src.main
│   ├── pipeline.py                       Per-document orchestrator
│   │
│   ├── models/
│   │   ├── invoice.py                    Pydantic v2: InvoiceRecord, LineItem, ExtractionResult
│   │   └── schema.py                     Column definitions derived from InvoiceRecord.model_fields
│   │
│   ├── ingestion/
│   │   ├── loader.py                     Pre-flight integrity checks
│   │   ├── pdf_classifier.py             Digital vs scanned page detection
│   │   └── file_tracker.py               SHA-256 hash + processing_status table
│   │
│   ├── preprocessing/
│   │   ├── image_cleanup.py              Colour overlay removal, denoise, binarize
│   │   └── language_detector.py          Language detection stub
│   │
│   ├── ocr/
│   │   ├── tesseract_runner.py           Runs Tesseract, returns structured word data
│   │   ├── confidence.py                 Splits words into high/low confidence buckets
│   │   └── bbox_mapper.py                Maps word bounding boxes to page zones
│   │
│   ├── extraction/
│   │   ├── vision_fallback.py            v2 — raw page image → gemma3:4b vision
│   │   ├── extractor.py                  Per-page extraction orchestrator
│   │   ├── prompt_builder.py             Builds system + user prompt for Ollama
│   │   ├── model_client_ollama.py        HTTP client for Ollama API with retry logic
│   │   └── merger.py                     Merges multi-page results, one row per line item
│   │
│   ├── guardrails/
│   │   ├── input_sanitizer.py            Strips prompt injection from OCR text
│   │   └── output_filter.py              Validates LLM JSON, checks field evidence
│   │
│   ├── validation/
│   │   ├── field_normalizer.py           Type coercion — floats, dates, IBAN
│   │   ├── business_rules.py             IBAN/VAT regex, tax math, date ordering
│   │   └── duplicate_checker.py          Detects duplicate invoices in SQLite
│   │
│   ├── confidence/
│   │   └── scorer.py                     Composite score + routing decision
│   │
│   ├── review/
│   │   └── review_queue.py               Writes low-confidence docs to review_queue/
│   │
│   ├── quarantine/
│   │   └── quarantine.py                 Copies bad files with explanation sidecars
│   │
│   ├── storage/
│   │   ├── record_splitter.py            Splits flat records into invoices + line_items
│   │   ├── json_writer.py                Timestamped JSON output writer
│   │   └── sqlite_writer.py              Two-table normalised SQLite writer
│   │
│   └── observability/
│       ├── logger.py                     Structured JSON-line logger
│       └── metrics.py                    In-memory counters, flushed to metrics.json
│
├── configs/
│   └── config.yaml                       All pipeline configuration
│
├── k8s/
│   ├── cronjob.yaml                      CronJob manifest
│   ├── namespace.yaml                    Namespace manifest
│   ├── ollama.yaml                       Ollama deployment manifest
│   └── pvc.yaml                          PersistentVolumeClaim manifests
│
├── tests/
│   ├── conftest.py                       Adds project root to sys.path for all tests
│   ├── test_field_normalizer.py          21 tests — date/float/IBAN normalisation
│   ├── test_business_rules.py            22 tests — all 10 validation rules
│   └── test_confidence_scorer.py          8 tests — composite scoring and tiers
│
├── evaluation/
│   └── evaluate.py                       End-to-end accuracy evaluation framework
│
├── test_ground_truth/
│   ├── gt_invoices_invoice_v3_01.json    Manually verified correct values
│   ├── gt_invoices_invoice_v3_02.json
│   ├── gt_invoices_invoice_v3_03.json
│   ├── gt_line_items_invoice_v3_01.json
│   ├── gt_line_items_invoice_v3_02.json
│   └── gt_line_items_invoice_v3_03.json
│
├── sample_pdf/
│   └── scan/                             DROP INPUT PDFs HERE (batch + API both read here)
│
├── output/                               Generated on first run
│   ├── processed/
│   │   ├── latest/                       Copy of most recent run outputs
│   │   └── YYYYMMDDTHHMMSSZ/             Timestamped output directory per run
│   │       ├── invoices.json
│   │       ├── line_items.json
│   │       ├── invoices.csv
│   │       ├── line_items.csv
│   │       ├── dump.sql
│   │       ├── run_summary.json
│   │       └── vision/                   Only created when composite_score < 0.80
│   │           ├── invoices.json
│   │           ├── line_items.json
│   │           ├── invoices.csv
│   │           ├── line_items.csv
│   │           └── comparison.json
│   ├── quarantine/                       Bad files with explanation sidecars
│   ├── review_queue/                     Low-confidence documents for human review
│   ├── extractions.db                    Persistent SQLite database
│   └── metrics.json                      Runtime metrics from last run
│
├── logs/
│   └── logs.txt                          Structured JSON-line log
│
├── .env                                  Local secrets — TESSERACT_PATH, API keys etc.
├── Dockerfile                            Containerise the pipeline (batch mode)
├── requirements.txt                      Python package dependencies
├── environment.yml                       Conda environment specification
├── conftest.py                           Root-level pytest path setup
├── poll_job.py                           CLI polling script for API job results
├── run_api.bat                           Windows: start uvicorn API server
├── submit_invoice.bat                    Windows: submit a PDF via curl
├── watch_logs.bat                        Windows: live log streaming (tail -f equivalent)
└── README.md                             Quick-start guide
```

---

## 10. Data Schema & Output

### 8.1 The Two-Table Model

```
invoices                              invoice_line_items
(one row per invoice)                 (one row per line item)
─────────────────────────            ────────────────────────────
InvoiceId          PK                SourceFile      FK
SourceFile                           InvoiceId       FK
FileHash                             LineId
RunTimestamp                         ProductName
IssueDate                            ProductDescription
DueDate                              BilledQuantity
Currency                             BilledUnit
SellerName                           NetPrice
SellerVatId                          DiscountAmount
SellerStreet                         LineTotalAmount
SellerPostcode                       TaxRatePercent
SellerCity                           TaxCategory
SellerCountry                        TaxType
BuyerId                              BillingStartDate
BuyerName                            DiscountIndicator
BuyerStreet                          DiscountReason
BuyerPostcode
BuyerCity
BuyerCountry
BuyerEmail
ShipToName
ShipToStreet
ShipToPostcode
ShipToCity
ShipToCountry
Iban
Bic
BankAccountName
PaymentMeansCode
PaymentTerms
InvLineTotal
InvAllowanceTotal
InvTaxBasisTotal
InvTaxTotal
InvGrandTotal
InvDuePayable
ExtractionMethod
_confidence_score
_confidence_tier
_is_valid
_validation_errors
```

### 8.2 ExtractionMethod Values

| Value | Set by | Meaning |
|---|---|---|
| `llm_ocr` | Primary pipeline | Tesseract OCR text → gemma3:4b text mode |
| `vision_gemma3:4b` | Vision fallback | Raw page image → gemma3:4b vision mode |

### 8.3 Sample Output — invoices.json

```json
{
  "InvoiceTypeCode": null,
  "InvAllowanceTotal": -22200.0,
  "ShipToCity": null,
  "InvRounding": null,
  "IssueDate": "2024-03-15",
  "Currency": "USD",
  "ShipToName": null,
  "SellerEmail": "us.accounts.payable@xyzcompany.com",
  "PaymentTerms": "Net 30 days from invoice date",
  "InvChargeTotal": null,
  "PaymentMeansCode": "ACH",
  "SellerName": "XYZcompany",
  "BuyerReference": "INV-2024-00421",
  "_confidence_score": "0.535",
  "BankAccountName": "Payment method: ACH",
  "_is_valid": false,
  "BuyerCity": null,
  "SellerPhone": null,
  "InvLineTotal": 32490.0,
  "BuyerStreet": "12 DAY $1,850.00 _ $22,200.00",
  "_validation_errors": "['TAX_MATH: 32280.0 + 2864.85 = 35144.85 ≠ 10290.0', 'LINE_MATH: 2100.0 × 5.0 = 10500.00 ≠ 10290.0', \"INVALID_IBAN: '021000021'\", \"INVALID_VAT_ID: '8875'\"]",
  "SellerStreet": "1 Data Engineering Consulting Fabric pipeline implementation and MLOps configurati",
  "SellerCountry": "USA",
  "BuyerEmail": null,
  "_confidence_tier": "low",
  "InvTaxTotalCurrencyId": null,
  "FileHash": "",
  "SellerVatId": "8875",
  "SellerPostcode": null,
  "ExtractionMethod": "llm_ocr",
  "BuyerId": "CORP-US-001",
  "ShipToStreet": null,
  "BuyerName": "ABC Company LLC",
  "Bic": "CHASUS33",
  "Iban": "021000021",
  "InvGrandTotal": 10290.0,
  "InvDuePayable": 10290.0,
  "BuyerPostcode": null,
  "_action": "human_review",
  "InvPrepaidTotal": null,
  "ShipToCountry": null,
  "SellerCity": "Boston",
  "SourceFile": "invoice_v3_01.pdf",
  "_conflicts": "",
  "InvTaxBasisTotal": 32280.0,
  "ShipToPostcode": null,
  "InvoiceId": "INV-2024-00421",
  "BuyerCountry": null,
  "SellerContactName": null,
  "ReferencedInvoiceId": null,
  "InvTaxTotal": 2864.85,
  "DueDate": "2024-04-14",
  "RunTimestamp": "20260609T104224Z",
  "_is_duplicate": "False"
}
```

### 8.4 Sample Output — vision/comparison.json

```json
{
  "source_file": "invoice_v3_01.pdf",
  "ocr_composite_score": 0.535,
  "ocr_llm_model": "gemma3:4b",
  "vision_model": "gemma3:4b",
  "field_comparison": {
    "agreed": {
      "Currency": "USD",
      "InvoiceId": "INV-2024-00421",
      "PaymentMeansCode": "ACH",
      "PaymentTerms": "Net 30 days from invoice date",
      "SellerCity": "Boston",
      "SourceFile": "invoice_v3_01.pdf"
    },
    "disagreed": {
      "BankAccountName": {
        "ocr_llm": "Payment method: ACH",
        "vision": "ABC Company LLC"
      },
      "Bic": {
        "ocr_llm": "CHASUS33",
        "vision": "SWIFT/BIC: CHASUS3"
      },
      "BuyerName": {
        "ocr_llm": "ABC Company LLC",
        "vision": "XYZ Company Inc."
      },
      "BuyerReference": {
        "ocr_llm": "INV-2024-00421",
        "vision": "PO-2024-0312"
      },
      "BuyerStreet": {
        "ocr_llm": "12 DAY $1,850.00 _ $22,200.00",
        "vision": "1 Industrial Parkway"
      },
      "DueDate": {
        "ocr_llm": "2024-04-14",
        "vision": "04/14/2024"
      },
      "InvAllowanceTotal": {
        "ocr_llm": -22200.0,
        "vision": "$0.00"
      },
      "InvDuePayable": {
        "ocr_llm": 10290.0,
        "vision": "$35,144.85"
      },
      "InvGrandTotal": {
        "ocr_llm": 10290.0,
        "vision": "$53,144.85"
      },
      "InvLineTotal": {
        "ocr_llm": 32490.0,
        "vision": "$22,200.00"
      },
      "InvTaxBasisTotal": {
        "ocr_llm": 32280.0,
        "vision": "$32,280.00"
      },
      "InvTaxTotal": {
        "ocr_llm": 2864.85,
        "vision": "$2,864.85"
      },
      "IssueDate": {
        "ocr_llm": "2024-03-15",
        "vision": "03/15/2024"
      },
      "SellerCountry": {
        "ocr_llm": "USA",
        "vision": "US"
      },
      "SellerEmail": {
        "ocr_llm": "us.accounts.payable@xyzcompany.com",
        "vision": "invoicing@abccompany.com"
      },
      "SellerName": {
        "ocr_llm": "XYZcompany",
        "vision": "ABC Company LLC"
      },
      "SellerStreet": {
        "ocr_llm": "1 Data Engineering Consulting Fabric pipeline implementation and MLOps configurati",
        "vision": "200 State Street, Suite 1400"
      }
    },
    "ocr_only": {
      "BilledQuantity": 5.0,
      "BilledUnit": "DAY",
      "BuyerId": "CORP-US-001",
      "Iban": "021000021",
      "LineId": "1",
      "LineTotalAmount": 10290.0,
      "NetPrice": 2100.0,
      "ProductDescription": "Document Intelligence Pipeline",
      "ProductName": "Al Pipeline Development GPT-40 document extraction pipeline — Bronze/Silver",
      "SellerVatId": "8875",
      "TaxCategory": "S",
      "TaxRatePercent": 8875.0
    },
    "vision_only": {
      "BuyerCity": "Malden",
      "BuyerCountry": "US",
      "BuyerPostcode": "MA 02148",
      "OcrCompositeScore": 0.535,
      "SellerContactName": "M. Bauer",
      "SellerPhone": "(617) 452-7800",
      "SellerPostcode": "MA 02109",
      "ShipToCity": "Malden",
      "ShipToCountry": "US",
      "ShipToName": "XYZ Company Inc. - Operations Hub",
      "ShipToPostcode": "MA 02148",
      "ShipToStreet": "1 Industrial Parkway",
      "VisionModel": "gemma3:4b"
    }
  },
  "summary": {
    "agreed": 6,
    "disagreed": 17,
    "ocr_only": 12,
    "vision_only": 13
  }
}
```

---

## 11. Operational Runbook

### 9.1 Environment Configuration (.env)

```bash
# Tesseract
TESSERACT_PATH=C:\Users\Documents\tesseract\tesseract.exe

# Ollama — no API key required, runs locally
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=gemma3:4b
OLLAMA_TIMEOUT=1800

# OCR confidence threshold (words below this % → LLM re-extraction)
CONFIDENCE_THRESHOLD=80

# Paths
INPUT_DIR=sample_pdf/scan
OUTPUT_DIR=output
LOG_FILE=logs/logs.txt
```

### 9.2 First-Time Setup

```bash
# 1. Pull the model (one time only — ~3GB download)
ollama pull gemma3:4b

# 2. Verify it is available
ollama list
# Should show: gemma3:4b

# 3. Confirm vision capability
ollama run gemma3:4b "Describe what you see" --image /path/to/any/image.png
```

### 9.3 Scheduled Batch Run

**Manual run:**

```bash
# Terminal 1 — start Ollama (keep running)
set OLLAMA_MODELS=C:\Users\Documents\OllamaModels
ollama serve

# Terminal 2 — run the pipeline
conda activate inv_ext_env
cd ai_invoice_extraction_pipeline
python -m src.main
```

**Expected runtime per document:**

| Stage | Time |
|---|---|
| Preprocessing + OCR | 15–30 seconds |
| LLM extraction (primary) | 180–360 seconds |
| Vision fallback (if triggered) | 180–360 seconds additional |
| Total per low-confidence doc | ~8–9 minutes |

**Windows Task Scheduler setup for weekly batch:**

1. Open Task Scheduler → Create Task
2. General: name "Invoice Extraction Pipeline", run whether user is logged on or not
3. Triggers: Weekly, Monday, 06:00 AM
4. Action: `python.exe` from conda env, arguments `-m src.main`, start in project directory
5. Conditions: check "Wake the computer to run this task"

Ollama must be running as a Windows service before the pipeline starts, or add a pre-task that starts `ollama serve` and waits for the health endpoint (`http://localhost:11434/api/tags`) to respond.

### 9.4 On-Demand Processing via API

As an alternative to waiting for the Monday batch run, any invoice can be processed immediately via the FastAPI server.

**Step 1 — Start Ollama (Terminal 1):**

```bash
set OLLAMA_MODELS=C:\Users\Documents\OllamaModels
ollama serve
```

**Step 2 — Start the API server (Terminal 2, or double-click `run_api.bat`):**

```bash
uvicorn app.api:app --reload --port 8000
```

Confirm the server is healthy:

```bash
curl http://localhost:8000/health
# or open http://localhost:8000/docs in a browser
```

**Step 3 — Submit a PDF (double-click `submit_invoice.bat` or use curl directly):**

```bat
submit_invoice.bat sample_pdf\scan\invoice_v3_01.pdf
```

Copy the `job_id` from the response.

**Step 4 — Poll for the result:**

```bash
python poll_job.py 3f2a1b4c-8d9e-4f2a-b1c3-2d4e5f6a7b8c
```

The poller prints live status every 10 seconds and displays a formatted summary when the job completes. Alternatively, poll manually:

```bash
curl http://localhost:8000/result/3f2a1b4c-...
```

**Step 5 — Monitor logs in real time (optional, double-click `watch_logs.bat`):**

```bat
watch_logs.bat
```

This streams `logs/logs.txt` live — useful during the LLM inference phase to see OCR confidence scores, LLM latency, and validation results as they occur.



1. Copy PDF files to `sample_pdf/scan/`
2. Run the pipeline (or wait for Monday 06:00)
3. Files already processed in previous runs are automatically skipped via file hash check

### 9.5 Checking Results After a Run

```bash
# Quick stats
cat output/processed/latest/run_summary.json

# Vision fallback results (if any documents were below 0.80)
cat output/processed/latest/vision/comparison.json

# Which documents need human review
ls output/review_queue/

# Which documents were quarantined
ls output/quarantine/

# Recent log entries
tail -50 logs/logs.txt
```

### 9.6 Force Re-processing a Previously Processed File

```python
import sqlite3
conn = sqlite3.connect("output/extractions.db")
conn.execute("DELETE FROM processing_status WHERE source_file = ?", ("invoice_name.pdf",))
conn.commit()
conn.close()
```

Re-run the pipeline. The file will be treated as new.

### 9.7 Adjusting the Vision Fallback Threshold

The threshold is defined in `src/extraction/vision_fallback.py`:

```python
VISION_THRESHOLD = 0.80
```

And referenced in `configs/config.yaml`:

```yaml
vision:
  VISION_THRESHOLD: 0.80
```

Lowering this value (e.g. to `0.80`) reduces how many documents trigger the vision fallback, shortening total run time at the cost of fewer vision comparisons. Raising it (e.g. to `1.0`) triggers vision on every document — useful during initial calibration to build a complete comparison dataset.

---

## 12. Docker & Kubernetes Deployment

### 10.1 Deployment Options — Choosing the Right Approach

| Scenario | Right tool |
|---|---|
| Weekly batch, single machine, < 100 documents | Windows Task Scheduler (local Python) |
| Weekly batch, need portability or reproducibility | Docker + Task Scheduler |
| Local Kubernetes testing (WSL2, no cloud) | Docker + k3d (this section) |
| Production, cloud, auto-scaling | Azure Kubernetes Service (AKS) |

The deployment described in detail below targets **WSL2 + k3d**: a full local Kubernetes cluster running inside Windows Subsystem for Linux. This is the recommended path for testing the Kubernetes manifests on your laptop before any cloud deployment.

---

### 10.2 Dockerfile

The pipeline uses a Conda-based image so that the `inv_ext_env` environment defined in `environment.yml` is reproduced exactly.

```dockerfile
FROM continuumio/miniconda3:24.1.2-0

RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-deu \
    tesseract-ocr-eng \
    libgl1 \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY environment.yml .
RUN conda env create -f environment.yml && conda clean -afy

ENV PATH=/opt/conda/envs/inv_ext_env/bin:$PATH
ENV CONDA_DEFAULT_ENV=inv_ext_env

COPY src/      ./src/
COPY configs/  ./configs/

RUN mkdir -p sample_pdf/scan output/processed output/quarantine \
             output/review_queue logs

ENV TESSERACT_PATH=/usr/bin/tesseract
ENV OLLAMA_BASE_URL=http://ollama:11434
ENV OLLAMA_MODEL=gemma3:4b
ENV VISION_MODEL=gemma3:4b
ENV OLLAMA_TIMEOUT=1800
ENV CONFIDENCE_THRESHOLD=60

ENTRYPOINT ["python", "-m", "src.main"]
```

**Key design choices:**

- Based on `continuumio/miniconda3` rather than `python:3.11-slim` — this ensures `conda env create` works and all pinned package versions in `environment.yml` are respected
- Tesseract + German language data installed at the OS level; no path configuration needed inside the container (`TESSERACT_PATH=/usr/bin/tesseract`)
- Ollama is **not** included in this image — it runs as a separate pod and is reached via the `OLLAMA_BASE_URL` environment variable

---

### 10.3 Python Environment (environment.yml)

```yaml
name: inv_ext_env
channels:
  - conda-forge
  - defaults
dependencies:
  - python=3.11
  - pip
  - pip:
    - pymupdf==1.24.5
    - pytesseract==0.3.13
    - Pillow==10.4.0
    - pypdf==4.3.1
    - requests==2.32.3
    - lxml==5.2.2
    - pandas==2.2.2
    - numpy==1.26.4
    - python-dotenv==1.0.1
    - pyyaml==6.0.1
    - tqdm==4.66.4
```

All versions are pinned. The conda environment is built once during `docker build` and baked into the image layer.

---

### 10.4 Local Deployment on WSL2 + k3d

This section covers the complete setup from a fresh Windows machine to a running Kubernetes cluster with the pipeline deployed as a CronJob.

All commands from **Step 2 onwards** run inside a WSL2 terminal unless stated otherwise.

#### Step 1 — Install WSL2 and Ubuntu

Open PowerShell as Administrator and run:

```powershell
wsl --install
```

This installs WSL2 and Ubuntu (the default distribution) in one step. Reboot when prompted. After reboot, Ubuntu will launch automatically and ask you to create a Linux username and password.

To verify WSL2 is active after setup:

```powershell
wsl --list --verbose
# Should show Ubuntu with VERSION 2
```

#### Step 2 — Install Docker inside WSL2

Open a WSL2 (Ubuntu) terminal and run:

```bash
sudo apt update
sudo apt install docker.io -y

# Start the Docker daemon and enable it on boot
sudo systemctl start docker
sudo systemctl enable docker

# Allow your user to run Docker without sudo
sudo usermod -aG docker $USER

# Apply the group change without logging out
newgrp docker

# Verify Docker is working
docker run hello-world
```

> **Note:** This installs the Docker Engine directly inside WSL2 (`docker.io`), not Docker Desktop. No Docker Desktop licence is required.

#### Step 3 — Install kubectl inside WSL2

```bash
# Download the latest stable kubectl binary
curl -LO "https://dl.k8s.io/release/$(curl -Ls https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"

# Make it executable and move to PATH
chmod +x kubectl
sudo mv kubectl /usr/local/bin/

# Verify
kubectl version --client
```

#### Step 4 — Install k3d inside WSL2

k3d runs a lightweight k3s Kubernetes cluster inside Docker containers. It is the simplest way to get a full Kubernetes environment locally.

```bash
curl -s https://raw.githubusercontent.com/k3d-io/k3d/main/install.sh | bash

# Verify
k3d version
```

#### Step 5 — Create the k3d Cluster

```bash
k3d cluster create invoice-cluster \
  --agents 1 \
  --port "8080:80@loadbalancer"

# Verify the cluster is up and kubectl is pointing to it
kubectl cluster-info
kubectl get nodes
# Should show 1 server node and 1 agent node in Ready state
```

The cluster runs entirely inside Docker containers in WSL2. No cloud account or VM is needed.

#### Step 6 — Build the Pipeline Docker Image

Navigate to your project root (inside WSL2 — use the WSL path to your Windows files):

```bash
# Your Windows project folder is accessible at /mnt/c/Users/<you>/...
cd /mnt/c/Users/<your-username>/path/to/ai_invoice_extraction_pipeline

# Build the image — tag must match what cronjob.yaml references
docker build -t v2-ai-invoice-extraction:latest .

# Verify the image was built
docker images | grep v2-ai-invoice-extraction
```

**This step takes 5–10 minutes** on the first run because it downloads the Miniconda base image and runs `conda env create`. Subsequent builds use the Docker layer cache and are faster.

#### Step 7 — Import the Image into k3d

k3d runs its own internal Docker registry. Images built in WSL2 must be explicitly imported into the cluster — they are not automatically visible to cluster nodes.

```bash
k3d image import v2-ai-invoice-extraction:latest -c invoice-cluster

# Also import the Ollama image if you have it locally
# (or let k3d pull it from Docker Hub on first deploy)
```

> **Why this step?** The CronJob manifest sets `imagePullPolicy: Never`, which tells Kubernetes to use only locally available images. If the image isn't imported, the pod will fail with `ErrImageNeverPull`.

#### Step 8 — Deploy the Kubernetes Manifests

Apply the manifests in dependency order:

```bash
# 1. Create the namespace first — all other resources live inside it
kubectl apply -f k8s/namespace.yaml

# 2. Create the PersistentVolumeClaims (storage volumes)
kubectl apply -f k8s/pvc.yaml

# 3. Deploy Ollama (the LLM inference server)
kubectl apply -f k8s/ollama.yaml

# 4. Wait for Ollama to be ready before proceeding
kubectl rollout status deployment/ollama -n invoice-pipeline
# Waits until the readiness probe passes (GET /api/tags returns 200)

# 5. Deploy the pipeline CronJob
kubectl apply -f k8s/cronjob.yaml
```

Verify everything deployed correctly:

```bash
kubectl get all -n invoice-pipeline
# Should show:
#   deployment.apps/ollama        1/1  Running
#   service/ollama                ClusterIP
#   cronjob.batch/invoice-extraction  (schedule: 0 5 * * 1)
#   persistentvolumeclaim/invoice-input-pvc    Bound
#   persistentvolumeclaim/invoice-output-pvc   Bound
#   persistentvolumeclaim/ollama-models-pvc     Bound
```

#### Step 9 — Pull the gemma3:4b Model into Ollama

The Ollama pod starts without any models. Pull `gemma3:4b` once — it is stored on the `ollama-models-pvc` persistent volume and survives pod restarts.

```bash
# Get the name of the running Ollama pod
kubectl get pods -n invoice-pipeline

# Exec into it and pull the model (~3 GB download)
kubectl exec -it deployment/ollama -n invoice-pipeline -- ollama pull gemma3:4b

# Verify the model is available
kubectl exec -it deployment/ollama -n invoice-pipeline -- ollama list
# Should show: gemma3:4b
```

#### Step 10 — Copy Input PDFs to the Volume

The pipeline reads from the `invoice-input-pvc` volume mounted at `/app/sample_pdf/scan`. To load invoices into it, run a temporary pod that mounts the same volume:

```bash
# Copy PDFs from your local WSL filesystem into the cluster volume
kubectl run pdf-loader --image=busybox --restart=Never \
  --overrides='{"spec":{"volumes":[{"name":"input","persistentVolumeClaim":{"claimName":"invoice-input-pvc"}}],"containers":[{"name":"pdf-loader","image":"busybox","command":["sleep","3600"],"volumeMounts":[{"mountPath":"/data","name":"input"}]}]}}' \
  -n invoice-pipeline

# Wait for the pod to be running
kubectl wait --for=condition=Ready pod/pdf-loader -n invoice-pipeline

# Copy your PDFs in
kubectl cp ./sample_pdf/scan/. invoice-pipeline/pdf-loader:/data/

# Clean up the loader pod
kubectl delete pod pdf-loader -n invoice-pipeline
```

#### Step 11 — Trigger a Manual Run (Test Before Waiting for Monday)

The CronJob runs automatically every Monday at 05:00 UTC. To trigger it immediately for testing:

```bash
# Create a one-off Job from the CronJob spec
kubectl create job invoice-test-run \
  --from=cronjob/invoice-extraction \
  -n invoice-pipeline

# Watch the pod start and follow its logs
kubectl get pods -n invoice-pipeline -w

# Once the pod name appears, stream its logs
kubectl logs -f job/invoice-test-run -n invoice-pipeline
```

#### Step 12 — Retrieve Output Files

After the job completes, copy output files out of the `invoice-output-pvc` volume using the same loader-pod pattern:

```bash
kubectl run output-reader --image=busybox --restart=Never \
  --overrides='{"spec":{"volumes":[{"name":"output","persistentVolumeClaim":{"claimName":"invoice-output-pvc"}}],"containers":[{"name":"output-reader","image":"busybox","command":["sleep","3600"],"volumeMounts":[{"mountPath":"/data","name":"output"}]}]}}' \
  -n invoice-pipeline

kubectl wait --for=condition=Ready pod/output-reader -n invoice-pipeline

# Copy the entire output directory to your local filesystem
kubectl cp invoice-pipeline/output-reader:/data ./output-from-k8s/

kubectl delete pod output-reader -n invoice-pipeline
```

---

### 10.5 Kubernetes Manifest Reference

#### namespace.yaml

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: invoice-pipeline
```

All pipeline resources (Ollama deployment, CronJob, PVCs) are scoped to the `invoice-pipeline` namespace. This isolates the pipeline from other workloads on the same cluster.

#### pvc.yaml

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: invoice-input-pvc
  namespace: invoice-pipeline
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: local-path
  resources:
    requests:
      storage: 2Gi
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: invoice-output-pvc
  namespace: invoice-pipeline
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: local-path
  resources:
    requests:
      storage: 5Gi
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: ollama-models-pvc
  namespace: invoice-pipeline
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: local-path
  resources:
    requests:
      storage: 10Gi
```

`storageClassName: local-path` is the default storage class provided by k3d/k3s. It provisions storage from a directory on the node's local disk — no external storage provider needed for local development.

| PVC | Size | Purpose |
|---|---|---|
| `invoice-input-pvc` | 2 Gi | PDF input files |
| `invoice-output-pvc` | 5 Gi | JSON, CSV, SQLite, logs per run |
| `ollama-models-pvc` | 10 Gi | gemma3:4b model weights (~4 GB) |

#### ollama.yaml

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ollama
  namespace: invoice-pipeline
spec:
  replicas: 1
  selector:
    matchLabels:
      app: ollama
  template:
    metadata:
      labels:
        app: ollama
    spec:
      containers:
        - name: ollama
          image: ollama/ollama:latest
          imagePullPolicy: IfNotPresent
          ports:
            - containerPort: 11434
          volumeMounts:
            - name: ollama-models
              mountPath: /root/.ollama
          resources:
            requests:
              memory: "4Gi"
              cpu: "1"
            limits:
              memory: "10Gi"
              cpu: "3"
          readinessProbe:
            httpGet:
              path: /api/tags
              port: 11434
            initialDelaySeconds: 30
            periodSeconds: 10
      volumes:
        - name: ollama-models
          persistentVolumeClaim:
            claimName: ollama-models-pvc
---
apiVersion: v1
kind: Service
metadata:
  name: ollama
  namespace: invoice-pipeline
spec:
  selector:
    app: ollama
  ports:
    - port: 11434
      targetPort: 11434
  type: ClusterIP
```

The Ollama `Deployment` runs as a persistent, always-on service. The pipeline CronJob connects to it via the in-cluster DNS name `http://ollama.invoice-pipeline.svc.cluster.local:11434` — set as `OLLAMA_BASE_URL` in `cronjob.yaml`. The `readinessProbe` ensures Kubernetes does not route traffic to Ollama until the HTTP API is ready.

#### cronjob.yaml

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: invoice-extraction
  namespace: invoice-pipeline
spec:
  schedule: "0 5 * * 1"       # Every Monday 05:00 UTC = 06:00 CET
  concurrencyPolicy: Forbid   # Never run two jobs at the same time
  successfulJobsHistoryLimit: 3
  failedJobsHistoryLimit: 3
  jobTemplate:
    spec:
      backoffLimit: 2
      activeDeadlineSeconds: 14400   # 4-hour hard limit per run
      template:
        spec:
          restartPolicy: OnFailure
          containers:
            - name: pipeline
              image: v3-ai-invoice-extraction:latest
              imagePullPolicy: Never   # Use locally imported image only
              env:
                - name: TESSERACT_PATH
                  value: "/usr/bin/tesseract"
                - name: OLLAMA_BASE_URL
                  value: "http://ollama.invoice-pipeline.svc.cluster.local:11434"
                - name: OLLAMA_MODEL
                  value: "gemma3:4b"
                - name: OLLAMA_TIMEOUT
                  value: "1800"
                - name: CONFIDENCE_THRESHOLD
                  value: "80"
              volumeMounts:
                - name: input-vol
                  mountPath: /app/sample_pdf/scan
                - name: output-vol
                  mountPath: /app/output
              resources:
                requests:
                  memory: "2Gi"
                  cpu: "1"
                limits:
                  memory: "4Gi"
                  cpu: "2"
          volumes:
            - name: input-vol
              persistentVolumeClaim:
                claimName: invoice-input-pvc
            - name: output-vol
              persistentVolumeClaim:
                claimName: invoice-output-pvc
```

**Key settings:**

| Setting | Value | Reason |
|---|---|---|
| `schedule` | `0 5 * * 1` | Monday 05:00 UTC = 06:00 CET |
| `concurrencyPolicy` | `Forbid` | Prevents overlapping runs if a job is still running when the next is due |
| `imagePullPolicy` | `Never` | Uses the locally imported image; fails fast if the image wasn't imported (Step 7) |
| `OLLAMA_BASE_URL` | `http://ollama.invoice-pipeline.svc.cluster.local:11434` | In-cluster DNS — resolves to the Ollama Service inside the namespace |
| `activeDeadlineSeconds` | `14400` | 4-hour hard kill; increase to `21600` if vision fallback consistently runs on all documents |

---

### 10.6 Cluster Management Commands

```bash
# Check the status of all resources in the namespace
kubectl get all -n invoice-pipeline

# View CronJob schedule and last run status
kubectl get cronjob -n invoice-pipeline

# List all completed and running jobs
kubectl get jobs -n invoice-pipeline

# Stream logs from the most recent pipeline job
kubectl logs -l job-name=invoice-extraction -n invoice-pipeline --tail=100 -f

# Describe a failing pod (shows events, exit codes, OOM kills)
kubectl describe pod <pod-name> -n invoice-pipeline

# Check Ollama is healthy and the model is loaded
kubectl exec -it deployment/ollama -n invoice-pipeline -- ollama list

# Delete the cluster entirely (does NOT delete your local files)
k3d cluster delete invoice-cluster

# Recreate the cluster from scratch
k3d cluster create invoice-cluster --agents 1

# some output examples
$ k3d cluster list
NAME              SERVERS   AGENTS   LOADBALANCER
invoice-cluster   1/1       1/1      true

$ kubectl get deployments -A
NAMESPACE          NAME                     READY   UP-TO-DATE   AVAILABLE   AGE
invoice-pipeline   ollama                   1/1     1            1           15h
kube-system        coredns                  1/1     1            1           16h
kube-system        local-path-provisioner   1/1     1            1           16h
kube-system        metrics-server           1/1     1            1           16h
kube-system        traefik                  1/1     1            1           16h

$ kubectl port-forward deployment/ollama 11434:11434 -n invoice-pipeline
Forwarding from 127.0.0.1:11434 -> 11434
Forwarding from [::1]:11434 -> 11434
Handling connection for 11434
Handling connection for 11434
```

---

## 13. Monitoring & Observability

### 11.1 Structured Logging

Every pipeline event is written as a JSON line to `logs/logs.txt`.

```
{"ts": "2026-06-09T10:21:44.399105+00:00", "logger": "__main__", "level": "info", "event": "pipeline_startup", "model": "gemma3:4b", "vision_model": "gemma3:4b", "threshold": 80, "vision_threshold": 0.8, "input_dir": "sample_pdf/scan", "run_timestamp": "20260609T102144Z"}
Tesseract set: C:\Users\Documents\tesseract\tesseract.exe
Found 1 PDF(s) in 'sample_pdf/scan'
Processing PDFs:   0%|          | 0/1 [00:00<?, ?it/s]{"ts": "2026-06-09T10:21:44.519559+00:00", "logger": "src.ingestion.loader", "level": "info", "event": "load_ok", "path": "invoice_v3_01.pdf", "pages": 1, "bytes": 362231}
{"ts": "2026-06-09T10:21:44.520664+00:00", "logger": "src.pipeline", "level": "info", "event": "pipeline_start", "file": "invoice_v3_01.pdf"}
{"ts": "2026-06-09T10:21:44.523907+00:00", "logger": "src.ingestion.loader", "level": "info", "event": "load_ok", "path": "invoice_v3_01.pdf", "pages": 1, "bytes": 362231}
{"ts": "2026-06-09T10:21:44.532385+00:00", "logger": "src.ingestion.pdf_classifier", "level": "info", "event": "pdf_classified", "path": "sample_pdf/scan\\invoice_v3_01.pdf", "kind": "scanned", "digital": 0, "scanned": 1, "has_xml": false}
{"ts": "2026-06-09T10:21:51.698901+00:00", "logger": "src.ocr.tesseract_runner", "level": "info", "event": "ocr_complete", "page": 1, "words": 286, "avg_conf": 67.5, "psm": 6, "lang": "deu+eng"}
{"ts": "2026-06-09T10:21:51.698901+00:00", "logger": "src.extraction.extractor", "level": "info", "event": "ocr_post_preprocess", "page": 1, "avg_conf": 67.51, "words": 286}
{"ts": "2026-06-09T10:21:52.305522+00:00", "logger": "src.extraction.extractor", "level": "info", "event": "sending_to_llm", "page": 1, "ocr_conf": 67.51, "text_chars": 1570, "low_conf_words": 120}
{"ts": "2026-06-09T10:25:42.219174+00:00", "logger": "src.extraction.model_client_ollama", "level": "info", "event": "llm_response_ok", "model": "gemma3:4b", "attempt": 1, "latency_s": 229.91, "chars": 1944}
{"ts": "2026-06-09T10:25:42.230092+00:00", "logger": "src.extraction.merger", "level": "info", "event": "line_items_expanded", "invoice": "INV-2024-00421", "rows": 1}
[Validator] invoice_v3_01.pdf — 4 error(s)
{"ts": "2026-06-09T10:25:42.242616+00:00", "logger": "src.confidence.scorer", "level": "info", "event": "confidence_scored", "composite": 0.535, "tier": "low", "action": "human_review", "breakdown": {"ocr": 0.6751, "field_confidence": 0.0, "validation": 0.6, "completeness": 1.0, "guardrails": 1.0}}
[ReviewQueue] Queued: invoice_v3_01.pdf — reasons: ['confidence_action=human_review', 'validation_errors=4']
{"ts": "2026-06-09T10:25:42.261953+00:00", "logger": "src.pipeline", "level": "info", "event": "pipeline_done", "file": "invoice_v3_01.pdf", "action": "human_review", "valid": false, "review": true}
{"ts": "2026-06-09T10:25:42.265954+00:00", "logger": "__main__", "level": "info", "event": "low_confidence_flagged_for_vision", "file": "invoice_v3_01.pdf", "composite_score": 0.535, "vision_model": "gemma3:4b"}
{"ts": "2026-06-09T10:25:42.299489+00:00", "logger": "__main__", "level": "info", "event": "doc_done", "file": "invoice_v3_01.pdf", "invoice_id": "INV-2024-00421", "grand_total": 10290.0, "rows": 1, "model": "gemma3:4b", "action": "human_review", "composite_score": 0.535}
Processing PDFs: 100%|██████████| 1/1 [03:57<00:00, 237.85s/it]
{"ts": "2026-06-09T10:25:42.312377+00:00", "logger": "__main__", "level": "info", "event": "all_docs_done", "run_timestamp": "20260609T102144Z", "stats": {"processed": 1, "skipped": 0, "quarantined": 0, "failed": 0}, "total_flat_records": 1}
{"ts": "2026-06-09T10:25:42.317097+00:00", "logger": "src.storage.record_splitter", "level": "info", "event": "records_split", "flat_in": 1, "invoices_out": 1, "line_items_out": 1}
[JSON] 1 invoice(s) → output\processed\20260609T102144Z\invoices.json
[JSON] 1 line item(s) → output\processed\20260609T102144Z\line_items.json
[SQLite] 1 invoice(s) → output/extractions.db
[SQLite] 1 line item(s) → output/extractions.db
[SQLite] SQL dump → output\processed\20260609T102144Z\dump.sql
{"ts": "2026-06-09T10:25:42.734254+00:00", "logger": "src.extraction.vision_fallback", "level": "info", "event": "vision_fallback_start", "documents": 1, "model": "gemma3:4b", "threshold": 0.8}
{"ts": "2026-06-09T10:25:42.756462+00:00", "logger": "src.extraction.vision_fallback", "level": "info", "event": "vision_pdf_resolved", "file": "invoice_v3_01.pdf", "path": "c:\\Users\\SohamChoudhary\\Downloads\\Documents\\v3_ai_invoice_extraction_pipeline\\sample_pdf\\scan\\invoice_v3_01.pdf"}
{"ts": "2026-06-09T10:25:42.760609+00:00", "logger": "src.extraction.vision_fallback", "level": "info", "event": "vision_processing", "file": "invoice_v3_01.pdf", "ocr_confidence": 0.535, "model": "gemma3:4b"}
[CSV] 1 row(s) → output\processed\20260609T102144Z\invoices.csv
[CSV] 1 row(s) → output\processed\20260609T102144Z\line_items.csv
{"ts": "2026-06-09T10:25:43.677114+00:00", "logger": "src.extraction.vision_fallback", "level": "info", "event": "vision_page_call", "file": "invoice_v3_01.pdf", "page": 1, "model": "gemma3:4b"}
{"ts": "2026-06-09T10:29:54.480365+00:00", "logger": "src.extraction.vision_fallback", "level": "info", "event": "vision_complete", "file": "invoice_v3_01.pdf", "vision_fields_extracted": 33, "vision_line_items": 2, "agreed": 6, "disagreed": 17, "model": "gemma3:4b"}
{"ts": "2026-06-09T10:29:54.500218+00:00", "logger": "src.extraction.vision_fallback", "level": "info", "event": "vision_fallback_done", "vision_invoices": 1, "vision_line_items": 2, "comparisons": 1, "output_dir": "output\\processed\\20260609T102144Z\\vision"}
[Vision JSON] 1 record(s) → output\processed\20260609T102144Z\vision\invoices.json
[Vision JSON] 2 record(s) → output\processed\20260609T102144Z\vision\line_items.json
[Vision JSON] 1 record(s) → output\processed\20260609T102144Z\vision\comparison.json
[Vision CSV] 1 row(s) → output\processed\20260609T102144Z\vision\invoices.csv
[Vision CSV] 2 row(s) → output\processed\20260609T102144Z\vision\line_items.csv

🔍 Vision fallback complete [gemma3:4b]
   Documents re-processed : 1
   Vision invoices        : 1
   Vision line items      : 2
   Comparison report      : output\processed\20260609T102144Z\vision/comparison.json

✅ Done  [20260609T102144Z]
   Processed   : 1 new
   Skipped     : 0 (already done)
   Quarantined : 0
   Failed      : 0
   Invoices    : 1
   Line items  : 1
   JSON latest : output/processed/latest/
   DB          : output/extractions.db
   Summary     : output\processed\20260609T102144Z\run_summary.json
   Vision docs : 1 re-processed
   Vision out  : output\processed\20260609T102144Z/vision/
   Comparison  : output\processed\20260609T102144Z/vision/comparison.json
```

### 11.2 Runtime Metrics

```json
{
  "counters": {
    "docs_attempted": 3,
    "docs_processed_ok": 3,
    "ocr_pages_total": 3,
    "llm_calls_success": 3,
    "docs_queued_review": 3,
    "vision_fallback_triggered": 3
  },
  "distributions": {
    "ocr_avg_confidence": { "count": 3, "min": 66.6, "max": 72.1, "mean": 69.5 },
    "llm_latency_s":      { "count": 3, "min": 145.4, "max": 211.6, "mean": 174.2 },
    "doc_total_seconds":  { "count": 3, "min": 152.1, "max": 218.8, "mean": 184.3 }
  }
}
```

### 11.3 Key Metrics to Monitor

| Metric | Alert threshold | What it indicates |
|---|---|---|
| `docs_pipeline_error` | > 0 | A document failed to process entirely |
| `quarantined` count | > 0 | Bad files received — investigate sender |
| `ocr_avg_confidence` mean | < 60% | Scan quality degrading across documents |
| `llm_latency_s` mean | > 300s | Model performance degrading — check RAM |
| Review queue as % of run | > 30% | Extraction quality declining |
| `vision_fallback_triggered` | > 50% of run | Consider tuning preprocessing or switching to vision-primary |
| Vision `disagreed` count avg | > 5 per doc | OCR+LLM and vision consistently diverge — investigate root cause |

### 11.4 Using comparison.json for Quality Improvement

The `vision/comparison.json` file accumulates evidence over multiple runs. Track these trends:

- **High `agreed` + low `disagreed`** across all docs → both approaches are consistent; OCR+LLM primary is reliable
- **High `vision_only` count** → vision model is recovering fields the OCR pipeline misses entirely; consider vision-primary for that document type
- **High `disagreed` count on financial fields** (InvGrandTotal, InvTaxTotal) → one approach has a systematic error; cross-reference against source documents to identify which

---

## 14. Known Limitations & Roadmap

### 12.1 Current Limitations

**All sample invoices score below 0.60.** The current confidence scoring may be over-penalising documents that are actually correctly extracted. Before tuning the scorer or preprocessing, use the `comparison.json` to verify whether the vision model agrees with the primary extraction — if it does, the issue is scoring calibration, not extraction quality.

**Vision fallback is additive, not corrective.** The primary pipeline output is never overwritten by vision results. A document that routes to `human_review` because of low confidence still requires a human reviewer even if the vision extraction produced higher-quality results. A future improvement would auto-promote vision results to the primary output when vision confidence is measurably higher.

**Sequential processing.** Documents are processed one at a time. Acceptable for weekly batches of ~30 documents. Parallelisation is required beyond that volume.

**CPU inference latency.** Both primary and vision passes run on CPU. With all 3 current invoices triggering vision fallback, total run time is approximately 30–45 minutes. Acceptable for weekly batch; not suitable for real-time or daily processing at scale.

**Human review loop is write-only.** Corrected values from human review are not written back to the database automatically. A `PATCH /review/{invoice_id}` endpoint is the next step.

### 12.2 Roadmap

**Priority 1 — Calibration (immediate):**

- Analyse `comparison.json` across 5–10 runs to understand where OCR+LLM and vision agree and disagree
- Tune composite confidence scorer weights if documents are being over-penalised
- Tune colour overlay suppression thresholds if preprocessing is degrading text quality

**Priority 2 — Before > 100 documents per week:**

- ~~Add automated test suite with sample invoice fixtures~~ **Done** — see Section 16
- Parallelise document processing (3–4 workers)
- Implement auto-promotion: if vision confidence > primary confidence, use vision result as the primary record

**Priority 3 — Before multi-user or multi-system access:**

- Replace SQLite with PostgreSQL
- Implement human review feedback loop (corrections written back to database)
- ~~Add REST API layer for downstream system integration~~ **Done** — see Section 6 (FastAPI: `/extract`, `/result`, `/jobs`, `/health`)
- Add `PATCH /review/{invoice_id}` endpoint so human-corrected values are written back to the database

**Priority 4 — Production hardening:**

- Dockerise and deploy via Docker Compose
- Kubernetes CronJob for Azure deployment (manifests already in `k8s/`)
- Azure Monitor integration for log aggregation
- Review UI showing original PDF alongside extracted fields with inline correction

---


---

## 15. Extraction Quality Evaluation

### 13.1 Purpose

The evaluation framework measures how accurately the pipeline extracts fields from real invoice PDFs compared to manually verified ground truth values. It produces per-field accuracy metrics for both the primary OCR+LLM extraction path and the vision fallback path side by side, giving a quantitative basis for architecture decisions.

This is distinct from the unit test suite (Section 14). The evaluation runs the full pipeline on real PDFs and compares results end to end. It takes 30–45 minutes because Ollama must process all three invoices.

### 13.2 Ground Truth Files

Manually verified correct values are stored in `test_ground_truth/`:

```
test_ground_truth/
  gt_invoices_invoice_v3_01.json     ← ABC Company LLC / INV-2024-00421
  gt_invoices_invoice_v3_02.json     ← DataStream Consulting / RE-2024-0887
  gt_invoices_invoice_v3_03.json     ← Stratosphere Cloud / INV-CLOUD-2024-1103
  gt_line_items_invoice_v3_01.json   ← 2 line items
  gt_line_items_invoice_v3_02.json   ← 3 line items
  gt_line_items_invoice_v3_03.json   ← 4 line items
```

Each file contains the complete correct field values for that invoice, with `"ExtractionMethod": "ground_truth"` and `"_confidence_score": 1.0` to distinguish them from pipeline outputs.

### 13.3 Running the Evaluation

```bash
# Full evaluation — runs pipeline on all 3 invoices (requires Ollama running)
python -m evaluation.evaluate --verbose

# Single invoice only
python -m evaluation.evaluate --invoice v3_01 --verbose

# Use already-processed output (no pipeline re-run, fast)
python -m evaluation.evaluate --no-run --verbose
```

The `--no-run` flag loads from `output/processed/latest/` for OCR+LLM results and from `output/processed/latest/vision/` for vision results. Vision outputs are automatically copied to `latest/vision/` at the end of each run.

### 13.4 Evaluation Results — 3 Invoices (June 2026 baseline)

#### Overall accuracy

| Metric | OCR+LLM | Vision |
|---|---|---|
| Invoice header accuracy | 28/66 (42.4%) | 26/66 (39.4%) |
| Line item accuracy | 35/58 (60.3%) | 7/50 (14.0%) |

#### Per-field breakdown (invoice header, all 3 invoices)

| Field | Correct | Total | Accuracy |
|---|---|---|---|
| DueDate | 3 | 3 | 100% |
| Currency | 3 | 3 | 100% |
| InvTaxBasisTotal | 3 | 3 | 100% |
| InvTaxTotal | 3 | 3 | 100% |
| IssueDate | 2 | 3 | 67% |
| SellerName | 2 | 3 | 67% |
| SellerCountry | 2 | 3 | 67% |
| BuyerCountry | 2 | 3 | 67% |
| InvLineTotal | 2 | 3 | 67% |
| PaymentMeansCode | 2 | 3 | 67% |
| Bic | 2 | 3 | 67% |
| InvoiceId | 1 | 3 | 33% |
| PaymentTerms | 1 | 3 | 33% |
| SellerVatId | 0 | 3 | 0% |
| SellerStreet | 0 | 3 | 0% |
| SellerCity | 0 | 3 | 0% |
| BuyerName | 0 | 3 | 0% |
| BuyerStreet | 0 | 3 | 0% |
| BuyerCity | 0 | 3 | 0% |
| BuyerEmail | 0 | 3 | 0% |
| InvGrandTotal | 0 | 3 | 0% |
| InvDuePayable | 0 | 3 | 0% |

### 13.5 Interpreting the Results

**What the pipeline gets right reliably:** Financial totals (TaxBasisTotal, TaxTotal, LineTotal — all 100%), dates, currency, payment codes, and BIC. These are structurally stable fields that appear in predictable positions.

**What it consistently gets wrong:**

- **Seller/Buyer address confusion (0%):** The model frequently swaps the seller and buyer blocks. This is a prompt engineering problem — the zone-based prompt needs tighter disambiguation between the "SOLD BY" and "BILL TO" blocks. Fix: add explicit seller/buyer block headers in the prompt and increase the zone separation.

- **InvGrandTotal (0%):** The model extracts a line item total instead of the invoice grand total. The `InvGrandTotal` instruction in `prompt_builder.py` needs to be more explicit — e.g. "look specifically for the line labelled GRAND TOTAL or TOTAL DUE at the bottom of the invoice, not any subtotal."

- **InvoiceId (33%):** Only one of three invoices extracts the correct ID. The other two either miss it entirely or extract a reference number. Invoice numbering formats vary significantly across the three documents.

**Why Vision scores lower than OCR+LLM on line items:** The vision model extracted more line items than actually exist (inflated denominator), and matched fewer correctly. This is expected behaviour for a 4B-parameter model reading a full page image — it hallucinates additional rows from table structure. For line items, the text-based OCR+LLM path is consistently more accurate. This finding supports the hybrid architecture: use OCR+LLM as primary for structured table extraction, vision fallback only for header fields on low-confidence documents.

### 13.6 Evaluation Output Files

```
evaluation/results/
  evaluation_report_YYYYMMDDTHHMMSSZ.json   ← full per-field results
  evaluation_report_YYYYMMDDTHHMMSSZ.csv    ← flat CSV for spreadsheet analysis
```

The CSV is suitable for importing into Excel or Power BI to track accuracy trends across pipeline versions over time.

### 13.7 Bug Fixed During Evaluation — parse_float

Running the evaluation surfaced a bug in `src/validation/field_normalizer.py`: the `parse_float` function incorrectly parsed `8.875` (a US decimal tax rate) as `8875.0` because the European thousand-separator regex `^\d{1,3}(\.\d{3})+` matched `8.875` as a single digit followed by a three-digit group. This caused `TaxRatePercent` to be stored as 8875.0 rather than 8.875 for all US-format invoices.

The fix adds a mandatory comma requirement to the European format detection: European format only triggers when both dot-thousands AND comma-decimal are present. Plain decimals like `8.875` correctly fall through to direct float parsing.

This was caught by `tests/test_field_normalizer.py::TestParseFloat::test_tax_rate_decimal` failing — demonstrating the value of the test suite catching regressions before they reach production.

---

## 16. Automated Test Suite

### 14.1 Purpose

The test suite covers deterministic, isolated pipeline logic — functions that have no external dependencies (no PDF, no Ollama, no Tesseract required). Tests run in under 1 second and can be executed by anyone with just the Python environment.

These tests are separate from the evaluation framework (Section 15). Tests verify correctness of individual functions. The evaluation measures end-to-end extraction accuracy on real documents.

### 14.2 Running Tests

```bash
# All tests
pytest tests/ -v

# Single module
pytest tests/test_field_normalizer.py -v
pytest tests/test_business_rules.py -v
pytest tests/test_confidence_scorer.py -v

# With coverage (if pytest-cov installed)
pytest tests/ -v --cov=src --cov-report=term-missing
```

### 14.3 Test Files

```
tests/
  __init__.py
  test_field_normalizer.py    ← 21 tests
  test_business_rules.py      ← 22 tests
  test_confidence_scorer.py   ← 8 tests

conftest.py                   ← adds project root to sys.path for all tests
```

### 14.4 Test Coverage by Module

#### test_field_normalizer.py — 21 tests

Covers `parse_date`, `parse_float`, `normalize_iban`, and `normalize_record`.

| Class | Tests | What is verified |
|---|---|---|
| `TestParseDate` | 8 | ISO passthrough, German format (DD.MM.YYYY), slash format, compact (YYYYMMDD), dash DMY, None input, empty string, unparseable returns as-is |
| `TestParseFloat` | 13 | Plain decimal, European comma decimal, European thousand separator, US thousand separator, euro/dollar symbol stripping, percent stripping, tax rate decimal (the bug that was caught), None, zero, integer string, large European number, negative value |
| `TestNormaliseIban` | 4 | Space stripping, uppercasing, None → None, empty → None |
| `TestNormalizeRecord` | 6 | Date fields coerced, float fields coerced, IBAN normalised, None values pass through, unknown fields preserved, no crash on garbage input |

#### test_business_rules.py — 22 tests

Covers `validate()` from `src/validation/business_rules.py`.

| Class | Tests | What is verified |
|---|---|---|
| `TestRequiredFields` | 6 | All required present → no errors, missing InvoiceId/IssueDate/SellerName/InvGrandTotal each caught individually, empty record catches all 5 required fields |
| `TestTaxMath` | 3 | Correct math → no error, mismatch caught, within ±€0.06 tolerance passes |
| `TestLineMath` | 3 | Correct line math, mismatch caught, discount scenario correctly flags (NetPrice × Qty ≠ LineTotalAmount when discount applied) |
| `TestDateOrdering` | 2 | Due after issue → passes, due before issue → caught |
| `TestIbanVatBic` | 6 | Invalid IBAN caught, invalid VAT caught, invalid BIC caught, valid BIC (CHASUS33) passes, unknown currency caught, EUR/USD/GBP all pass |
| `TestNegativeValues` | 2 | Negative grand total caught, positive passes |

#### test_confidence_scorer.py — 8 tests

Covers `compute_composite_score()` from `src/confidence/scorer.py`.

| Test | What is verified |
|---|---|
| `test_perfect_inputs_give_high_score` | OCR 95%, no errors → score ≥ 0.80 |
| `test_low_ocr_reduces_score` | OCR 40% → score < 0.80 |
| `test_validation_errors_reduce_score` | 5 errors → lower than 0 errors |
| `test_missing_required_fields_reduce_score` | Empty record → score < 0.80 |
| `test_guardrail_issues_reduce_score` | 2 guardrail issues → lower than 0 issues |
| `test_tiers_are_assigned_correctly` | High/medium/low/invalid tiers all assignable |
| `test_result_has_required_keys` | Output always has composite_score, tier, action, breakdown |
| `test_composite_score_in_range` | Score always 0.0–1.0 |

### 14.5 Current Test Results

```
pytest tests/ -v
============================================================
platform win32 -- Python 3.11.8, pytest-9.0.3
collected 61 items

tests/test_business_rules.py        22 passed
tests/test_confidence_scorer.py      8 passed
tests/test_field_normalizer.py      58 passed  (3 were failing before parse_float fix)

61 passed in 0.90s
============================================================
```

All 61 tests pass after the `parse_float` bug fix described in Section 15.7.

### 14.6 What the Tests Do Not Cover

Tests intentionally do not cover modules with external dependencies (Tesseract, Ollama, PDF files, SQLite). Those are covered by:

- `check_pipeline.py` — full diagnostic health check including external services
- `evaluation/evaluate.py` — end-to-end accuracy on real invoices

Integration tests requiring Ollama and a real PDF are out of scope for the unit test suite and would be added as a separate `tests/integration/` directory in a future iteration.

*Document version: 5.0 — + FastAPI Layer + Pydantic Models + .bat Helper Scripts + poll_job.py + Updated Directory Structure*
*Pipeline version: v3*
*Updated: June 2026*
*Contact: Data Engineering Team*
