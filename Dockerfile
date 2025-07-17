# Agent Control Plane Gateway Image
# Builds the FastAPI gateway located in ./gateway

FROM python:3.13-slim AS base

# Install OS-level deps (none for now)

# Install python deps
RUN pip install --upgrade pip \
    && pip install --no-cache-dir fastapi uvicorn[standard] httpx docker asyncpg structlog

WORKDIR /app

# Copy gateway code
COPY cmd /app/cmd

EXPOSE 8080

CMD ["sh", "-c", "uvicorn cmd.gateway.main:app --host 0.0.0.0 --port ${ACP_BIND_PORT:-8080}"]
