# Local OCR service: Tesseract (English + French) behind a tiny HTTP API.
# Only the app container can reach it; no port is published.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    OMP_THREAD_LIMIT=2

RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng tesseract-ocr-fra poppler-utils \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY server.py .
RUN useradd --create-home --uid 1000 ocr
USER ocr

EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health')" || exit 1
CMD ["python", "server.py"]
