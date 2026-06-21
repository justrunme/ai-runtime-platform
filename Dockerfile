FROM python:3.12-slim AS runtime

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY pyproject.toml ./
COPY app ./app
RUN pip install --no-cache-dir .

USER 65532:65532
EXPOSE 8080
CMD ["uvicorn", "app.gateway.main:app", "--host", "0.0.0.0", "--port", "8080"]
