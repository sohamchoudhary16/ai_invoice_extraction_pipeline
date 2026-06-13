# AI Invoice Extraction Pipeline

Local pipeline for extracting structured invoice data from scanned and digital PDFs
using **Tesseract OCR** + **Ollama (Qwen2.5-VL)** — no paid APIs required.

---

## Architecture

```
PDF Input
   │
   ▼
[Classifier]          Is this a digital or scanned PDF?
   │
   ├── Digital ──────► PyMuPDF direct text extraction
   │
   └── Scanned ──────► Tesseract OCR (word-level confidence scores)
                            │
                     ┌──────┴───────┐
                  High conf      Low conf (<80%)
                     │               │
                     │         Ollama Qwen2.5-VL
                     │         (re-extracts + corrects)
                     └──────┬───────┘
                            │
                      [Normalizer]     Maps to schema columns
                            │
                      [Validator]      Business rule checks
                            │
                   ┌────────┼────────┐
                   ▼        ▼        ▼
               file.json  file.csv  file.sql
```

---

## Prerequisites

### 1. Python 3.10+
```
python --version
```

### 2. Tesseract OCR
- Download installer: https://github.com/UB-Mannheim/tesseract/wiki
- Pick the latest Windows `.exe`
- During install: **check "Add to PATH"**, keep English language data
- Verify: `tesseract --version`
- **No account or API key needed**

### 3. Ollama
- Download: https://ollama.com/download
- **No account or API key needed** — runs fully locally
- After install, pull the model:
  ```
  ollama pull qwen2.5vl:7b
  ```
  (~5GB download, one time)

---

## Setup

```bash
# 1. Clone / navigate to project
cd ai_invoice_extraction_pipeline

# 2. Create virtual environment
python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # Mac/Linux

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment
copy .env.example .env
# Open .env and set TESSERACT_PATH to where tesseract.exe is installed
# Run `where tesseract` in terminal to find the exact path
```

### .env keys explained

| Key | Required | Where to get it |
|-----|----------|-----------------|
| `TESSERACT_PATH` | ✅ Yes | Path to `tesseract.exe` on your machine. Run `where tesseract`. |
| `OLLAMA_BASE_URL` | No | Default is `http://localhost:11434` — only change if you changed Ollama's port |
| `OLLAMA_MODEL` | No | Default is `qwen2.5vl:7b` — do not use 72b on 16GB RAM |
| `CONFIDENCE_THRESHOLD` | No | Default 80 — lower if too many words are flagged |

**No Azure, OpenAI, or any cloud API key is needed.**

---

## Running

```bash
# Terminal 1 — start Ollama (keep this running)
ollama serve

# Terminal 2 — run the pipeline
cd ai_invoice_extraction_pipeline
venv\Scripts\activate
python -m src.main
```

Drop your PDFs into `sample_pdf/scan/` before running.

---

## Output

All outputs are merged across all processed PDFs:

| File | Location | Contents |
|------|----------|----------|
| `file.json` | `output/zugferd/` | All records as JSON array |
| `file.csv` | `output/zugferd/` | All records as flat CSV |
| `file.sql` | `output/zugferd/` | SQL INSERT statements |
| `extractions.db` | `output/` | SQLite database |
| `logs.txt` | `logs/` | Full pipeline log |

---

## Performance on Windows CPU + 16GB RAM

| Stage | Time per page |
|-------|---------------|
| Digital PDF extraction | < 1 second |
| Tesseract OCR | 2–5 seconds |
| Ollama LLM (7B, CPU) | 30–90 seconds |
| **Total per page** | ~1 minute |

This is expected for CPU-only inference. You are validating pipeline logic, not throughput.
