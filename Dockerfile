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
COPY app/      ./app/
COPY configs/  ./configs/

RUN mkdir -p sample_pdf/scan output/processed output/quarantine \
             output/review_queue logs

ENV TESSERACT_PATH=/usr/bin/tesseract
ENV OLLAMA_BASE_URL=http://ollama:11434
ENV OLLAMA_MODEL=gemma3:4b
ENV VISION_MODEL=gemma3:4b
ENV OLLAMA_TIMEOUT=1800
ENV CONFIDENCE_THRESHOLD=60

# Default command: run the API server.
# Override with: docker run ... python -m src.main   (for batch mode)
CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000"]
