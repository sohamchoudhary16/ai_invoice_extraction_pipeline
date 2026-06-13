@echo off
echo Starting Invoice Extraction API...
echo.
echo Make sure Ollama is already running (ollama serve in another window)
echo.
cd /d "%~dp0"
uvicorn app.api:app --reload --port 8000
