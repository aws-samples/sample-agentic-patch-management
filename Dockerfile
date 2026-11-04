# Single-stage build: FastAPI backend + pre-built React frontend
# Frontend is built on the host (deploy.sh) to avoid esbuild crashes
# under Finch/x86 emulation on Apple Silicon.
# Usage: docker build -t patch-automation-ui .

# Base image pinned by digest (python:3.12-slim, multi-arch index). To update:
#   docker buildx imagetools inspect public.ecr.aws/docker/library/python:3.12-slim
FROM public.ecr.aws/docker/library/python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

WORKDIR /app

# Dependencies from the hash-locked file. Regenerate after changing requirements.txt:
#   uv pip compile ui/api/requirements.txt --generate-hashes --python-version 3.12 \
#     --python-platform x86_64-manylinux_2_28 --no-header -o ui/api/requirements.lock
COPY ui/api/requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

# Copy backend
COPY ui/api/server.py /app/server.py
COPY ui/api/reconcile.py /app/reconcile.py

# Copy pre-built frontend (built on host by deploy.sh)
COPY ui/frontend/dist /app/static

# Run as non-root user
RUN groupadd -r appuser && useradd -r -g appuser -d /app -s /sbin/nologin appuser \
    && chown -R appuser:appuser /app
USER appuser

# Environment defaults. The root filesystem is read-only in ECS; only /tmp is writable.
ENV PORT=8000
ENV STATIC_DIR=/app/static
ENV PYTHONDONTWRITEBYTECODE=1

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health', timeout=4)"]

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
