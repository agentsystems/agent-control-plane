# Agent Control Plane Gateway Image
# Builds the FastAPI gateway located in ./gateway

# -----------------------------------------------------------------------------
# Builder stage – install Python deps into a temporary layer
# -----------------------------------------------------------------------------
FROM python:3.12-slim AS builder

WORKDIR /app

# Install runtime deps
RUN pip install --upgrade pip \
    && pip install --no-cache-dir fastapi uvicorn[standard] httpx docker asyncpg structlog

# Copy gateway source
COPY cmd /app/cmd

# -----------------------------------------------------------------------------
# Final stage – minimal, non-root image
# -----------------------------------------------------------------------------
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Copy Python runtime, dependencies and app code from builder stage
COPY --from=builder /usr/local /usr/local
COPY --from=builder /app/cmd /app/cmd

EXPOSE 8080

# Create dedicated non-root user for runtime security
RUN adduser --disabled-password --gecos "" appuser
USER appuser

CMD ["uvicorn", "cmd.gateway.main:app", "--host=0.0.0.0", "--port=${ACP_BIND_PORT:-8080}"]
