# AI Invoice Extraction Pipeline
## Technical Architecture & Operations Documentation

**Version:** 1.0  
**Classification:** Internal — Engineering & Executive  
**Owner:** Data Engineering  
**Schedule:** Weekly batch — every Monday 06:00 CET  

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Business Context & Problem Statement](#2-business-context--problem-statement)
3. [Architecture Overview](#3-architecture-overview)
4. [Pipeline Stages — Detailed](#4-pipeline-stages--detailed)
5. [Technology Decisions](#5-technology-decisions)
6. [Directory Structure](#6-directory-structure)
7. [Data Schema & Output](#7-data-schema--output)
8. [Operational Runbook](#8-operational-runbook)
9. [Docker & Kubernetes Deployment](#9-docker--kubernetes-deployment)
10. [Monitoring & Observability](#10-monitoring--observability)
11. [Known Limitations & Roadmap](#11-known-limitations--roadmap)

---

## 1. Executive Summary

The AI Invoice Extraction Pipeline is an automated batch processing system that ingests PDF invoice documents, extracts structured financial data using a combination of Optical Character Recognition (OCR) and a locally-hosted Large Language Model (LLM), and writes the results to a normalised database for downstream consumption.

The pipeline executes once per week — every Monday at 06:00 CET — and processes all new invoices deposited in the input directory since the previous run. Documents already processed are automatically skipped, making every run incremental and idempotent.

**What it solves:** Manual invoice data entry is slow, error-prone, and scales poorly. This pipeline eliminates manual extraction of header fields (seller, buyer, amounts, VAT IDs, IBANs) and line items (products, quantities, unit prices, discounts) from PDF invoices, including scanned documents with handwritten stamps and annotations.

**Key metrics at a glance:**

| Metric | Value |
|---|---|
| Processing cadence | Weekly (Monday 06:00 CET) |
| Supported input formats | Digital PDF, scanned PDF |
| Average processing time | 2–5 minutes per document (CPU inference) |
| Extraction confidence threshold | 80% OCR confidence |
| Output formats | JSON, CSV, SQLite |
| Quarantine handling | Automatic (corrupt / encrypted files) |
| Incremental processing | Yes (SHA-256 file hash deduplication) |

---

## 2. Business Context & Problem Statement

### 2.1 The Problem

The firm receives structured and semi-structured invoice documents from vendors and service providers. These documents arrive as PDF files of which two distinct categories exist:

**Category A — Digital PDFs:** Computer-generated invoices where the text layer is selectable. Text can be extracted directly without image processing.

**Category B — Scanned PDFs:** Physical documents scanned to PDF, or digital documents with graphical annotations overlaid (approval stamps, handwritten reviewer notes). These require image preprocessing and OCR before any structured extraction is possible.

Both categories may contain:

- Diagonal watermark stamps obscuring underlying text
- Handwritten annotations over printed fields
- Partially truncated line item descriptions
- European number formatting (1.234,56 notation)
- German and English mixed-language content

### 2.2 The Solution Approach

The pipeline implements a **tiered confidence extraction strategy**: fast deterministic methods are attempted first, with AI-powered correction applied only where confidence falls below an acceptable threshold. This minimises processing time while maximising accuracy on difficult documents.

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
                            ┌──────▼──┐  ┌────▼──────────────┐
                            │ PASS    │  │ LLM RE-EXTRACTION │
                            │ THROUGH │  │ Ollama llama3.2:3b│
                            └──────┬──┘  └────────┬──────────┘
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

---

## 4. Pipeline Stages — Detailed

### Stage 1: Ingestion & Pre-flight

Every PDF in the input directory is checked before processing begins.

**File hash check (incremental processing):** A SHA-256 hash is computed from the file's byte content. If this hash already exists in the `processing_status` table with status `"success"`, the file is skipped. This ensures re-running the pipeline never double-processes files, and adding 10 new files to a folder of 100 processes only the 10 new ones.

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

**Quarantine reasons and remediation:**

| Reason | Cause | Remediation |
|---|---|---|
| `ENCRYPTED` | Password-protected PDF | Decrypt before re-submitting |
| `CORRUPT` | File cannot be opened | Request fresh copy from sender |
| `TOO_LARGE` | Exceeds 200-page limit | Split into individual invoice files |
| `TOO_SMALL` | File smaller than 512 bytes | File is likely incomplete |
| `UNSUPPORTED_TYPE` | Not a PDF | Convert to PDF format |
| `ZERO_PAGES` | PDF opens but has no pages | File is malformed |

### Stage 3: Image Preprocessing

For scanned PDFs, each page is rendered to PNG at 300 DPI and passed through a preprocessing pipeline. This stage is the primary defence against stamp overlays which are the primary cause of OCR quality degradation.

**Why this matters:** Sample invoices showed diagonal coloured annotations ("Skonto nehmen! → 06 Belege fehlt!", "AI costs up 34%") that reduced raw OCR confidence to 52–72%. After preprocessing, confidence recovered to 66–88%.

**Preprocessing steps in order:**

1. **Colour overlay suppression:** Pixels with high colour saturation and mid-range brightness are set to white. Invoice text is black; background is white; stamps are coloured. This separates stamp pixels from text pixels using Pillow channel arithmetic — no machine learning required.

2. **Denoise:** A median filter smooths scan noise (dust, compression artefacts) without blurring text edges.

3. **Binarization:** Greyscale conversion and autocontrast increases the contrast ratio between text and background, improving Tesseract accuracy on faded scans.

4. **Border removal:** An 8-pixel border crop removes scan artefacts common at page edges.

### Stage 4: OCR

Tesseract processes the preprocessed image and returns word-level text with bounding box coordinates and confidence scores (0–100) for each word.

**Configuration:**

| Parameter | Value | Reason |
|---|---|---|
| Language | `deu+eng` | German + English mixed invoices |
| Page Segmentation Mode | `6` | Uniform text block — optimal for business documents |
| Render DPI | 300 | Standard for high-quality OCR |
| OEM | `3` | LSTM neural network engine (most accurate) |

### Stage 5: LLM Extraction

The cleaned OCR text is passed to Ollama (llama3.2:3b) with a structured extraction prompt. The model returns a flat JSON object mapping schema field names to extracted values.

**Prompt design principles:**

- OCR text is wrapped in `<DOC>` delimiters and explicitly labelled as untrusted data to prevent prompt injection
- Explicit field disambiguation rules explain which address block corresponds to seller, buyer, and ship-to
- `InvGrandTotal` is mapped explicitly to the "GRAND TOTAL" label on the invoice
- Line item table columns are described precisely so the model can match positional data to field names
- European number format (1.234,56 = 1234.56) is explained
- Temperature is set to 0 for deterministic, reproducible output

**Injection protection:** An input sanitiser scans OCR text for prompt injection patterns before LLM submission. Phrases such as "Ignore all previous instructions" are redacted and flagged in the log. This defends against malicious content embedded in scanned documents.

**Retry logic:** The LLM client retries up to 3 times with 5-second backoff on timeout or connection failure.

### Stage 6: Normalisation

Raw extracted values are coerced to correct types.

- **Float fields:** European decimal notation (comma as decimal separator) is detected and converted. Currency symbols are stripped.
- **Date fields:** Multiple formats (DD.MM.YYYY, YYYY-MM-DD, YYYYMMDD) are parsed and normalised to ISO 8601 YYYY-MM-DD.
- **IBAN:** Spaces removed. Stored as a continuous 22-character string.
- **Line items:** String values like "22,200.00" are parsed to floats. Percentage strings like "19%" are converted to numeric 19.

### Stage 7: Validation

Business rules are applied to the normalised record. Errors are collected but do not block the pipeline — every document is written to output regardless. Errors feed into the composite confidence score.

**Validation checks performed:**

| Check | Rule |
|---|---|
| Required fields present | InvoiceId, IssueDate, SellerName, InvGrandTotal, Currency must be non-null |
| Grand total positive | InvGrandTotal must be > 0 |
| Tax mathematics | TaxBasisTotal + TaxTotal ≈ GrandTotal (±€0.06 tolerance) |
| Line total mathematics | NetPrice × BilledQuantity ≈ LineTotalAmount |
| Date ordering | DueDate must be >= IssueDate |
| IBAN format | German IBAN: DE + 2 check digits + 18 alphanumeric characters = 22 total |
| VAT ID format | German VAT: DE + exactly 9 digits |
| BIC format | 8 or 11 character SWIFT code |
| Currency code | Must be a recognised ISO 4217 code |
| Numeric field types | All declared float columns must be parseable numbers |

### Stage 8: Record Splitting & Output

Records are split from a flat structure into two normalised tables before writing.

**Why two tables?** Invoice header data (seller name, buyer address, IBAN, grand total) is identical for every line item within a single invoice. A flat table repeats this data once per line item — an invoice with 4 line items repeats the grand total 4 times, causing double-counting in any downstream aggregation. The two-table design eliminates this redundancy.

**Output files written per run:**

```
output/processed/20260609T060000Z/
  invoices.json          one JSON object per invoice
  line_items.json        one JSON object per line item
  invoices.csv           flat CSV of invoice headers
  line_items.csv         flat CSV of line items
  dump.sql               full SQLite schema and data dump
  run_summary.json       processed / skipped / quarantined / failed counts

output/processed/latest/ always a copy of the most recent run
output/extractions.db    persistent SQLite (all runs accumulated)
output/quarantine/       bad files with explanation sidecars
output/review_queue/     low-confidence documents for human review
```

---

## 5. Technology Decisions

### 5.1 Why Tesseract + Local LLM instead of Azure Document Intelligence?

**Decision:** Use Tesseract for OCR and a local LLM for extraction rather than Azure Document Intelligence and Azure OpenAI.

| Factor | Azure Cloud | Local (Tesseract + Ollama) | Decision |
|---|---|---|---|
| Cost | Pay per page from day one | Free | Local wins for MVP |
| Free tier | 500 pages/month but only 2 pages per request | N/A | Cloud tier unusable for multi-page docs |
| Data privacy | Invoice data leaves The firm infrastructure | Data never leaves the machine | Local wins for confidential data |
| Latency | 3–5 seconds per page | 2–8 minutes per page (CPU) | Cloud wins — acceptable trade-off for weekly batch |
| Accuracy | Higher on degraded scans | Sufficient after preprocessing | Cloud wins slightly |
| Setup | Azure subscription + API keys + network | One-time local install | Local easier for MVP |

**Migration path:** The Tesseract and Ollama calls are isolated in two files (`src/ocr/tesseract_runner.py` and `src/extraction/model_client_ollama.py`). Replacing them with Azure SDK calls requires changing only those two files. The rest of the pipeline (preprocessing, validation, output) is cloud-agnostic.

### 5.2 Why llama3.2:3b instead of qwen2.5vl:7b (vision model)?

**Decision:** Use llama3.2:3b (text-only, 3 billion parameters) over qwen2.5vl:7b (vision-language, 7 billion parameters).

**Reasons:**

- qwen2.5vl:7b timed out consistently (> 120 seconds) on a CPU-only machine with 16GB RAM. The vision encoder adds 3–5 minutes of preprocessing overhead before the LLM begins generating text.
- The pipeline passes OCR text to the LLM, not page images. A vision model provides no benefit when the input is already text.
- llama3.2:3b processes a 2500-character OCR text block in 90–180 seconds on the same hardware, within acceptable bounds for a weekly batch run.

**When a vision model becomes relevant:** If OCR confidence on a document falls below ~40% and the text is too corrupted to be useful, a vision model reading the page image directly would outperform the text pipeline. The image preprocessing stage (colour overlay suppression, binarization) currently recovers confidence to 66–88%, making this edge case rare in practice.

### 5.3 Why SQLite instead of PostgreSQL?

**Decision:** SQLite for MVP. Migrate to PostgreSQL when concurrent access or network connectivity is required.

SQLite is zero-infrastructure — a single file managed natively by Python. The schema and queries are identical to PostgreSQL. Migration is a single connection string change plus minor driver swap.

**Migration trigger:** Move to PostgreSQL when any of the following occur:

- Multiple users or systems need concurrent read/write access
- Daily document volumes exceed ~500 files
- A downstream system needs to query data via network connection

### 5.4 Why not use LangChain for chunking?

**Decision:** Call Ollama directly via HTTP rather than using LangChain's chunking abstractions.

LangChain's chunking is designed for Retrieval-Augmented Generation (RAG) over large document corpora — splitting text into overlapping semantic chunks for similarity search. Invoice extraction is a different problem entirely: a single document, a single structured extraction prompt, a single JSON response. Chunking an invoice splits tables and field-value pairs across chunk boundaries, destroying the spatial relationships the LLM needs to extract correctly. A direct 4-line HTTP call to Ollama provides full control with no added complexity.

---

## 6. Directory Structure

```
ai_invoice_extraction_pipeline/
│
├── src/                                  All Python source code
│   ├── main.py                           Entry point — python -m src.main
│   ├── pipeline.py                       Per-document orchestrator
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
│   ├── models/
│   │   └── schema.py                     Column definitions for both output tables
│   │
│   └── observability/
│       ├── logger.py                     Structured JSON-line logger
│       └── metrics.py                    In-memory counters, flushed to metrics.json
│
├── configs/
│   └── config.yaml                       All pipeline configuration
│
├── sample_pdf/
│   └── scan/                             DROP INPUT PDFs HERE
│
├── output/                               Generated on first run
│   ├── processed/
│   │   ├── latest/                       Copy of most recent run outputs
│   │   └── YYYYMMDDTHHMMSSZ/             Timestamped output directory per run
│   ├── quarantine/                       Bad files with explanation sidecars
│   ├── review_queue/                     Low-confidence extractions for human review
│   ├── extractions.db                    Persistent SQLite database
│   └── metrics.json                      Runtime metrics from last run
│
├── logs/
│   └── logs.txt                          Structured JSON-line log
│
├── .env                                  Local secrets — TESSERACT_PATH etc.
├── .env.example                          Template for .env
├── requirements.txt                      Python package dependencies
└── README.md                             Quick-start guide
```

---

## 7. Data Schema & Output

### 7.1 The Two-Table Model

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

### 7.2 Sample Output — invoices.json (one record)

```json
{
  "InvoiceId": "INV-2024-00421",
  "SourceFile": "invoice_v2_01.pdf",
  "FileHash": "a3f2c19b1d44...",
  "RunTimestamp": "2026-06-09T06:00:00Z",
  "IssueDate": "2024-03-15",
  "DueDate": "2024-04-14",
  "Currency": "EUR",
  "SellerName": "Nordvista Software GmbH",
  "SellerVatId": "DE289341027",
  "Iban": "DE89370400440532013000",
  "Bic": "COBADEFFXXX",
  "InvGrandTotal": 38413.20,
  "InvTaxTotal": 6133.20,
  "InvTaxBasisTotal": 32280.00,
  "ExtractionMethod": "llm_ocr",
  "_confidence_score": "0.882",
  "_confidence_tier": "high",
  "_action": "auto_accept",
  "_is_valid": true,
  "_validation_errors": "[]"
}
```

### 7.3 Sample Output — line_items.json (two records from same invoice)

```json
[
  {
    "SourceFile": "invoice_v2_01.pdf",
    "InvoiceId": "INV-2024-00421",
    "LineId": "1",
    "ProductName": "Data Engineering Consulting",
    "ProductDescription": "Fabric pipeline implementation and MLOps configurati...",
    "BilledQuantity": 12.0,
    "BilledUnit": "DAY",
    "NetPrice": 1850.00,
    "DiscountAmount": null,
    "LineTotalAmount": 22200.00,
    "TaxRatePercent": 19.0
  },
  {
    "SourceFile": "invoice_v2_01.pdf",
    "InvoiceId": "INV-2024-00421",
    "LineId": "2",
    "ProductName": "AI Pipeline Development",
    "ProductDescription": "GPT-4o document extraction — Bronze/Silver/Gold laye...",
    "BilledQuantity": 5.0,
    "BilledUnit": "DAY",
    "NetPrice": 2100.00,
    "DiscountAmount": -210.00,
    "LineTotalAmount": 10290.00,
    "TaxRatePercent": 19.0,
    "DiscountReason": "Early project completion bonus discount"
  }
]
```

---

## 8. Operational Runbook

### 8.1 Scheduled Batch Run

The pipeline runs every Monday at 06:00 CET via Windows Task Scheduler.

**Manual run:**

```bash
# Terminal 1 — start Ollama inference server (keep running)
set OLLAMA_MODELS=C:\Users\SohamChoudhary\OneDrive\Documents\OllamaModels
ollama serve

# Terminal 2 — run the pipeline
conda activate inv_ext_env
cd ai_invoice_extraction_pipeline
python -m src.main
```

**Windows Task Scheduler setup for weekly batch:**

1. Open Task Scheduler → Create Task
2. General: name "Invoice Extraction Pipeline", run whether user is logged on or not
3. Triggers: Weekly, Monday, 06:00 AM
4. Action: Program = `python.exe` from the conda env, arguments = `-m src.main`, start in = project directory
5. Conditions: check "Wake the computer to run this task"

**Note:** Ollama must be running before the pipeline starts. Configure Ollama as a Windows service, or add a pre-task that starts `ollama serve` and waits for the health endpoint to respond.

### 8.2 Adding New Invoices

1. Copy PDF files to `sample_pdf/scan/`
2. Run the pipeline (or wait for Monday 06:00)
3. Files already processed in previous runs are automatically skipped via file hash check
4. Only new files are processed

### 8.3 Checking Results After a Run

```bash
# Quick stats
cat output/processed/latest/run_summary.json

# Which documents need human review
ls output/review_queue/

# Which documents were rejected as unprocessable
ls output/quarantine/

# Recent log entries
tail -50 logs/logs.txt
```

### 8.4 Force Re-processing a Previously Processed File

The file hash prevents the same file from being re-processed. To force re-processing:

```python
import sqlite3
conn = sqlite3.connect("output/extractions.db")
conn.execute("DELETE FROM processing_status WHERE source_file = ?", ("invoice_name.pdf",))
conn.commit()
conn.close()
```

Re-run the pipeline. The file will be treated as new.

### 8.5 Human Review Queue

Documents in `output/review_queue/` contain:

- A copy of the original PDF
- A `_review.json` sidecar with the extracted record, validation errors, confidence breakdown, and reasons for flagging

A human reviewer should open the original PDF alongside the `_review.json`, verify or correct the extracted fields, and update `"review_status"` to `"approved"` or `"corrected"`.

---

## 9. Docker & Kubernetes Deployment

### 9.1 When Does Containerisation Make Sense?

**Docker: Recommended as soon as the pipeline runs on any machine other than the original developer's laptop.**

Containerising solves the dependency portability problem. The pipeline currently depends on a specific Python version, Tesseract installed at a specific Windows path with German language data, and Ollama running as a separate service. None of these are portable. Docker wraps all dependencies into a reproducible image that runs identically on any machine.

**Kubernetes: Only when scale or cloud deployment is needed.**

| Scenario | Right tool |
|---|---|
| Weekly batch, single machine, < 100 documents | Windows Task Scheduler |
| Weekly batch, need portability | Docker + Task Scheduler |
| Daily batch, multiple machines | Docker Compose |
| Real-time processing, auto-scaling, cloud | Kubernetes (AKS) |

The current use case is Docker + Task Scheduler. Kubernetes is the correct future state if volume increases to hundreds of documents per day or if the pipeline is moved to Azure infrastructure.

### 9.2 Dockerfile

```dockerfile
FROM python:3.11-slim AS builder
WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --target /build/deps -r requirements.txt

FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-deu \
    tesseract-ocr-eng \
    libgl1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /build/deps /usr/local/lib/python3.11/site-packages

WORKDIR /app
COPY src/      ./src/
COPY configs/  ./configs/
COPY .env.example .env

RUN mkdir -p sample_pdf/scan output/processed output/quarantine \
             output/review_queue logs

ENV TESSERACT_PATH=/usr/bin/tesseract
ENV OLLAMA_BASE_URL=http://ollama:11434
ENV OLLAMA_MODEL=llama3.2:3b

ENTRYPOINT ["python", "-m", "src.main"]
```

**Important:** Tesseract is installed inside the container with language data included. No path configuration required. Ollama runs as a separate container.

### 9.3 Docker Compose

```yaml
version: "3.9"

services:
  ollama:
    image: ollama/ollama:latest
    container_name: invoice-ollama
    volumes:
      - C:/Users/SohamChoudhary/OneDrive/Documents/OllamaModels:/root/.ollama
    ports:
      - "11434:11434"
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:11434/api/tags"]
      interval: 30s
      timeout: 10s
      retries: 3

  pipeline:
    build: .
    container_name: invoice-pipeline
    depends_on:
      ollama:
        condition: service_healthy
    environment:
      OLLAMA_BASE_URL: http://ollama:11434
      OLLAMA_MODEL: llama3.2:3b
      OLLAMA_TIMEOUT: "1800"
    volumes:
      - ./sample_pdf/scan:/app/sample_pdf/scan
      - ./output:/app/output
      - ./logs:/app/logs
    restart: "no"
```

**Commands:**

```bash
# First time — download the model
docker compose run --rm ollama ollama pull llama3.2:3b

# Start Ollama (stays running in background)
docker compose up -d ollama

# Run the pipeline
docker compose run --rm pipeline
```

**Scheduling with Docker:**

Create `run_pipeline.bat` and schedule it in Task Scheduler:

```batch
@echo off
cd C:\path\to\ai_invoice_extraction_pipeline
docker compose run --rm pipeline
```

### 9.4 Kubernetes — Future State

For cloud deployment at scale, the architecture uses a Kubernetes CronJob on Azure Kubernetes Service (AKS).

```
┌────────────────────────────────────────────────────────────────┐
│                    AKS Cluster                                 │
│                                                                │
│  CronJob                                                       │
│  schedule: "0 5 * * 1"  (Monday 05:00 UTC = 06:00 CET)        │
│  concurrencyPolicy: Forbid                                     │
│  backoffLimit: 2                                               │
│  activeDeadlineSeconds: 14400 (4-hour limit)                   │
│                         │                                      │
│              ┌──────────┴──────────────────┐                  │
│              │                             │                  │
│   Pipeline Pod                    Ollama Deployment            │
│   (invoice-pipeline image)        (GPU node pool)              │
│   CPU: 2 cores, RAM: 4GB          Service: ClusterIP :11434    │
│              │                                                 │
│              │         PersistentVolumeClaims                  │
│              └──────── input-pvc  (Azure File Share)           │
│                        output-pvc (Azure File Share)           │
│                        ollama-pvc (Azure Disk — models)        │
└────────────────────────────────────────────────────────────────┘
```

**Kubernetes CronJob manifest:**

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: invoice-extraction
  namespace: data-engineering
spec:
  schedule: "0 5 * * 1"
  concurrencyPolicy: Forbid
  successfulJobsHistoryLimit: 3
  failedJobsHistoryLimit: 3
  jobTemplate:
    spec:
      backoffLimit: 2
      activeDeadlineSeconds: 14400
      template:
        spec:
          restartPolicy: OnFailure
          containers:
            - name: pipeline
              image: your-registry/invoice-pipeline:latest
              env:
                - name: OLLAMA_BASE_URL
                  value: "http://ollama-service:11434"
                - name: OLLAMA_MODEL
                  value: "llama3.2:3b"
                - name: OLLAMA_TIMEOUT
                  value: "1800"
              volumeMounts:
                - name: input-vol
                  mountPath: /app/sample_pdf/scan
                - name: output-vol
                  mountPath: /app/output
              resources:
                requests:
                  cpu: "1"
                  memory: "2Gi"
                limits:
                  cpu: "2"
                  memory: "4Gi"
          volumes:
            - name: input-vol
              persistentVolumeClaim:
                claimName: invoice-input-pvc
            - name: output-vol
              persistentVolumeClaim:
                claimName: invoice-output-pvc
```

---

## 10. Monitoring & Observability

### 10.1 Structured Logging

Every pipeline event is written as a JSON line to `logs/logs.txt`. This format is directly ingestible by Azure Monitor, Splunk, Datadog, and any log aggregation tool.

**Example log entries:**

```
{"ts":"2026-06-09T06:00:01Z","logger":"src.pipeline","level":"info","event":"pipeline_start","file":"invoice_v2_01.pdf"}
{"ts":"2026-06-09T06:00:06Z","logger":"src.ocr.tesseract_runner","level":"info","event":"ocr_complete","page":1,"words":250,"avg_conf":88.3}
{"ts":"2026-06-09T06:02:31Z","logger":"src.extraction.model_client_ollama","level":"info","event":"llm_response_ok","model":"llama3.2:3b","latency_s":145.4}
{"ts":"2026-06-09T06:02:32Z","logger":"src.confidence.scorer","level":"info","event":"confidence_scored","composite":0.882,"tier":"high","action":"auto_accept"}
{"ts":"2026-06-09T06:02:33Z","logger":"src.pipeline","level":"info","event":"pipeline_done","file":"invoice_v2_01.pdf","action":"auto_accept","valid":true}
```

### 10.2 Runtime Metrics

After each run, `output/metrics.json` contains aggregated statistics:

```json
{
  "counters": {
    "docs_attempted": 3,
    "docs_processed_ok": 3,
    "ocr_pages_total": 3,
    "llm_calls_success": 3,
    "docs_queued_review": 1
  },
  "distributions": {
    "ocr_avg_confidence": { "count": 3, "min": 66.6, "max": 88.3, "mean": 75.4 },
    "llm_latency_s": { "count": 3, "min": 145.4, "max": 211.6, "mean": 174.2 },
    "doc_total_seconds": { "count": 3, "min": 152.1, "max": 218.8, "mean": 184.3 }
  }
}
```

### 10.3 Key Metrics to Monitor

| Metric | Alert threshold | What it indicates |
|---|---|---|
| `docs_pipeline_error` counter | > 0 | A document failed to process entirely |
| `quarantined` count in run_summary | > 0 | Bad files received — investigate sender |
| `ocr_avg_confidence` mean | < 60% | Scan quality degrading across documents |
| `llm_latency_s` mean | > 300s | Model performance degrading — check RAM |
| Review queue as % of run total | > 30% | Extraction quality declining — prompt needs tuning |
| `failed` count in run_summary | > 0 | Processing failures requiring investigation |

---

## 11. Known Limitations & Roadmap

### 11.1 Current Limitations

**Extraction quality on complex documents:** The pipeline achieves high accuracy on cleanly structured invoices with standard layouts. Heavily annotated documents with multiple overlapping stamps reduce OCR confidence and may route to human review.

**Sequential processing:** The pipeline processes documents one at a time. For weekly batches of up to ~30 documents this is acceptable. Beyond that, parallelisation is needed.

**Local inference latency:** CPU-only inference with llama3.2:3b takes 90–180 seconds per page. Acceptable for weekly batch. Not suitable for real-time processing.

**Human review queue is read-only:** Corrected values from human review are not automatically written back to the database. This is a manual step currently.

**No automated test suite:** Changes require a full pipeline run to validate.

### 11.2 Roadmap

**Priority 1 — Before processing more than 100 documents per week:**

- Add automated test suite with sample invoice fixtures
- Parallelise document processing (3–4 workers)
- Move to GPU inference or cloud LLM for acceptable latency at scale

**Priority 2 — Before multi-user or multi-system access:**

- Replace SQLite with PostgreSQL
- Implement human review feedback loop (corrections written back to database)
- Add REST API layer for downstream system integration

**Priority 3 — Production hardening:**

- Dockerise and deploy via Docker Compose
- Implement Kubernetes CronJob for Azure deployment
- Add Azure Monitor integration for log aggregation
- Build review UI showing original PDF alongside extracted fields with inline correction

---

*Document generated: June 2026*  
*Pipeline version: v2 — Tesseract + LLM batch pipeline*  
*Contact: Data Engineering Team, The firm*
