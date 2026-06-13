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
