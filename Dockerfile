# Pulsar 5 — Open Agent Hackathon 2026
#
#   docker build -t pulsar5 .
#   docker run --rm -p 8000:8000 -e SAMPLE_MODE=true pulsar5
#   docker run --rm -p 8000:8000 --env-file .env pulsar5
#
# Running against a Zetaris or Nightshift on the host? Use
# host.docker.internal instead of localhost in .env.

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000

WORKDIR /app

# Dependencies at build time: nothing is installed or downloaded in the
# request path, as the technical requirements demand.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY scripts/ ./scripts/
COPY data/ ./data/
COPY ui/ ./ui/
COPY input_examples/ ./input_examples/
COPY run.py ./

RUN mkdir -p logs data/hmem && \
    useradd --create-home --uid 10001 pulsar && \
    chown -R pulsar:pulsar /app
USER pulsar

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

CMD ["python", "run.py"]
