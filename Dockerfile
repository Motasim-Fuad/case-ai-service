# syntax=docker/dockerfile:1.7
# ---------- stage 1: build the virtualenv (wheels only, no compiler needed) ----------
FROM python:3.12-slim AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY requirements.txt .
RUN pip install -r requirements.txt

# ---------- stage 2: small runtime image, non-root ----------
FROM python:3.12-slim AS runtime
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/app \
    PATH="/opt/venv/bin:$PATH"
RUN groupadd -r app && useradd -r -g app -u 10001 -m app
COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
COPY app ./app
RUN mkdir -p /app/data/uploads && chown -R app:app /app
USER app
EXPOSE 8000
# Default command = the API. docker-compose overrides it for the Celery worker.
CMD ["sh", "-c", "exec gunicorn app.main:app -k uvicorn.workers.UvicornWorker -b 0.0.0.0:8000 -w ${WEB_CONCURRENCY:-2} --timeout 120 --graceful-timeout 30"]
