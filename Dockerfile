# Multi-stage build: React frontend + FastAPI backend in one container
# Usage: docker build -t patch-automation-ui .

# ── Stage 1: Build frontend ────────────────────────────────────────
FROM public.ecr.aws/docker/library/node:20-slim AS frontend-build
WORKDIR /build
COPY ui/frontend/package.json ui/frontend/package-lock.json ./
RUN npm ci --silent
COPY ui/frontend/ ./
ARG BUILD_TIMESTAMP
RUN npm run build

# ── Stage 2: Production runtime ────────────────────────────────────
FROM public.ecr.aws/docker/library/python:3.12-slim

WORKDIR /app

# Install curl for container health check
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY ui/api/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy backend
COPY ui/api/server.py /app/server.py

# Copy agent config (needed for AgentCore ARN resolution)
COPY agent/.bedrock_agentcore.yaml /app/agent/.bedrock_agentcore.yaml

# Copy built frontend
COPY --from=frontend-build /build/dist /app/static

# Run as non-root user
RUN groupadd -r appuser && useradd -r -g appuser -d /app -s /sbin/nologin appuser \
    && chown -R appuser:appuser /app
USER appuser

# Environment defaults
ENV PORT=8000
ENV STATIC_DIR=/app/static

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD curl -f http://localhost:8000/api/health || exit 1

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
