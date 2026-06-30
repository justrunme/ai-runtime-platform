# syntax=docker/dockerfile:1
FROM python:3.12-slim@sha256:423ed6ab25b1921a477529254bfeeabf5855151dc2c3141699a1bfc852199fbf AS builder

WORKDIR /app
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
COPY pyproject.toml ./
COPY app ./app
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install .

FROM python:3.12-slim@sha256:423ed6ab25b1921a477529254bfeeabf5855151dc2c3141699a1bfc852199fbf AS runtime

LABEL org.opencontainers.image.title="ai-runtime-gateway" \
      org.opencontainers.image.description="OpenAI-compatible LLM runtime gateway with policy-based routing" \
      org.opencontainers.image.source="https://github.com/justrunme/ai-runtime-platform" \
      org.opencontainers.image.licenses="MIT"

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"
COPY --from=builder /opt/venv /opt/venv
COPY app ./app

USER 65532:65532
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request, sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz').status == 200 else 1)"]
CMD ["uvicorn", "app.gateway.main:app", "--host", "0.0.0.0", "--port", "8080"]
