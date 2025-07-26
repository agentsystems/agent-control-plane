# Agent Control Plane Gateway Image
# Builds the FastAPI gateway located in ./gateway

# -----------------------------------------------------------------------------
# Builder stage – install Python deps into a temporary layer
# -----------------------------------------------------------------------------
FROM python:3.12-slim@sha256:4600f71648e110b005bf7bca92dbb335e549e6b27f2e83fceee5e11b3e1a4d01 AS builder

WORKDIR /app

# Install runtime deps
RUN pip install --upgrade pip \
    && pip install --no-cache-dir fastapi uvicorn[standard] httpx docker asyncpg structlog python-multipart

# Copy gateway source
COPY cmd /app/cmd

# -----------------------------------------------------------------------------
# Final stage – minimal, non-root image
# -----------------------------------------------------------------------------
FROM python:3.12-slim@sha256:4600f71648e110b005bf7bca92dbb335e549e6b27f2e83fceee5e11b3e1a4d01

ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Copy Python runtime, dependencies and app code from builder stage
COPY --from=builder /usr/local /usr/local
COPY --from=builder /app/cmd /app/cmd

EXPOSE 8080

# Non-root user is created for future use, but we run as root so the gateway
# can access /var/run/docker.sock for container discovery. Revisit once we
# have a rootless Docker API solution.
RUN adduser --disabled-password --gecos "" appuser
# USER appuser

CMD ["sh", "-c", "uvicorn cmd.gateway.main:app --host 0.0.0.0 --port ${ACP_BIND_PORT:-8080}"]
