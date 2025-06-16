# Agent Control Plane Gateway Image
# Builds the FastAPI gateway located in ./gateway

FROM python:3.12-slim AS base

# Install OS-level deps (none for now)

# Install python deps
RUN pip install --upgrade pip \
    && pip install --no-cache-dir fastapi uvicorn[standard] httpx docker

WORKDIR /app

# Copy gateway code
COPY cmd /app/cmd

EXPOSE 8080

CMD ["uvicorn", "cmd.gateway.main:app", "--host", "0.0.0.0", "--port", "8080"]
