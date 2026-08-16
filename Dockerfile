# syntax=docker/dockerfile:1
# ---------------------------------------------------------------------------
# One image serves both roles — the API (uvicorn) and the UI (streamlit) —
# selected by the `command` in docker-compose. Keeps the build simple and the
# two services perfectly in sync.
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install deps first so the layer caches across code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code.
COPY app ./app
COPY ui ./ui
COPY .streamlit ./.streamlit

# Run as a non-root user (least privilege).
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/data && chown -R appuser /app
USER appuser

EXPOSE 8000 8501

# Default: the API. docker-compose overrides `command` for the UI service.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
