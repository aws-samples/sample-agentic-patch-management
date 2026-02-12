# Multi-stage build: React frontend + FastAPI backend in one container
# Usage: docker build -t patch-automation-ui .

# ── Stage 1: Build frontend ────────────────────────────────────────
FROM node:20-slim AS frontend-build
WORKDIR /build
COPY ui/frontend/package.json ui/frontend/package-lock.json ./
RUN npm ci --silent
COPY ui/frontend/ ./
RUN npm run build

# ── Stage 2: Production runtime ────────────────────────────────────
FROM python:3.12-slim

WORKDIR /app

# Install Python dependencies
COPY ui/api/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy backend
COPY ui/api/server.py /app/server.py

# Copy agent config (needed for AgentCore ARN resolution)
COPY agent/.bedrock_agentcore.yaml /app/agent/.bedrock_agentcore.yaml

# Copy built frontend
COPY --from=frontend-build /build/dist /app/static

# Environment defaults
ENV PORT=8000
ENV STATIC_DIR=/app/static

EXPOSE 8000

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
