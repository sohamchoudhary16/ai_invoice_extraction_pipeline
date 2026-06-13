FROM continuumio/miniconda3:24.1.2-0

# Tesseract + German language data + English
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-deu \
    tesseract-ocr-eng \
    libgl1 \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Create conda environment
COPY environment.yml .
RUN conda env create -f environment.yml && conda clean -afy

# Activate env for all subsequent commands
ENV PATH=/opt/conda/envs/inv_ext_env/bin:$PATH
ENV CONDA_DEFAULT_ENV=inv_ext_env

# Copy application
COPY src/      ./src/
COPY configs/  ./configs/
# COPY .env .env

# Runtime directories
RUN mkdir -p sample_pdf/scan output/processed output/quarantine \
             output/review_queue logs

# Linux Tesseract path (no Windows path needed in container)
ENV TESSERACT_PATH=/usr/bin/tesseract
ENV OLLAMA_BASE_URL=http://ollama:11434
ENV OLLAMA_MODEL=llama3.2:3b
ENV OLLAMA_TIMEOUT=1800

ENTRYPOINT ["python", "-m", "src.main"]
