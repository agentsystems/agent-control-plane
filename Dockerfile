# -----------------------------------------------------------------------------
# Agent Control Plane Gateway Image
# Builds the FastAPI gateway located in ./gateway
# -----------------------------------------------------------------------------

# -----------------------------------------------------------------------------
# Builder stage – install Python deps and collect licenses
# -----------------------------------------------------------------------------
FROM python:3.13-slim@sha256:2a928e11761872b12003515ea59b3c40bb5340e2e5ecc1108e043f92be7e473d AS builder

ENV PYTHONUNBUFFERED=1
WORKDIR /app

# Copy and install runtime Python deps from requirements.txt
COPY requirements.txt /tmp/
RUN pip install --upgrade pip \
    && pip install --no-cache-dir -r /tmp/requirements.txt

# Copy gateway source
COPY cmd /app/cmd

# ---- License collection (Python + Debian) ----
# 1) Install pip-licenses only for generation; we'll uninstall before producing final layer.
RUN pip install --no-cache-dir pip-licenses

# 2) Collect exact Python dependency list & license metadata (JSON + license texts)
RUN mkdir -p /app/licenses/python \
 && pip freeze --exclude-editable > /app/licenses/python/THIRD_PARTY_REQUIREMENTS.txt \
 && pip-licenses \
      --format=json \
      --with-authors \
      --with-urls \
      --with-license-file \
      --no-license-path \
      > /app/licenses/python/THIRD_PARTY_LICENSES.json

# 3) Generate a human-readable ATTRIBUTIONS.md (includes embedded license texts)
RUN python - <<'PY'
import json, os
p = "/app/licenses/python/THIRD_PARTY_LICENSES.json"
data = json.load(open(p))
out = "/app/licenses/python/ATTRIBUTIONS.md"
with open(out, "w", encoding="utf-8") as f:
    f.write("# Third-Party Python Packages\n\n")
    for row in sorted(data, key=lambda r: r["Name"].lower()):
        f.write(f"## {row.get('Name','')} {row.get('Version','')}\n")
        f.write(f"- License: {row.get('License','Unknown')}\n")
        if row.get("URL"): f.write(f"- URL: {row['URL']}\n")
        if row.get("Author"): f.write(f"- Author: {row['Author']}\n")
        txt = row.get("LicenseText")
        if txt:
            f.write("\n<details><summary>License text</summary>\n\n")
            f.write(txt)
            f.write("\n</details>\n")
        f.write("\n")
PY

# 4) Copy any Apache-2.0 NOTICE files found in site-packages
RUN mkdir -p /app/licenses/python_notices \
 && python - <<'PY'
import sys, pathlib, shutil
dest = pathlib.Path("/app/licenses/python_notices")
dest.mkdir(parents=True, exist_ok=True)
# search all site-packages dirs visible to this env
for p in map(pathlib.Path, sys.path):
    if p.exists() and "site-packages" in str(p):
        for item in p.iterdir():
            if item.is_dir():
                for name in ("NOTICE","NOTICE.txt","NOTICE.md"):
                    n = item / name
                    if n.exists():
                        shutil.copy2(n, dest / f"{item.name}-{name}")
PY

# 5) Collect Debian package licensing info (per-package copyright files)
RUN mkdir -p /app/licenses/debian \
 && sh -lc '\
    for pkg in $(dpkg-query -W -f="${Package}\n"); do \
      src="/usr/share/doc/$pkg/copyright"; \
      if [ -f "$src" ]; then \
        cp "$src" "/app/licenses/debian/${pkg}-copyright"; \
      fi; \
    done'

# 6) Remove pip-licenses so it doesn't ship in the final runtime
RUN pip uninstall -y pip-licenses || true


# -----------------------------------------------------------------------------
# Final stage – minimal, non-root image
# -----------------------------------------------------------------------------
FROM python:3.13-slim@sha256:2a928e11761872b12003515ea59b3c40bb5340e2e5ecc1108e043f92be7e473d

ENV PYTHONUNBUFFERED=1
WORKDIR /app

# Copy Python runtime, dependencies and app code from builder stage
COPY --from=builder /usr/local /usr/local
COPY --from=builder /app/cmd /app/cmd

# Copy our LICENSE file
COPY LICENSE /app/LICENSE

# Copy license/attribution artifacts
COPY --from=builder /app/licenses /app/licenses

# Optional OCI label pointing to license bundle location
LABEL org.opencontainers.image.title="AgentSystems Control Plane" \
      org.opencontainers.image.description="Gateway for managing and orchestrating AI agents" \
      org.opencontainers.image.vendor="AgentSystems" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.license.files="/app/licenses" \
      org.opencontainers.image.source="https://github.com/agentsystems/agent-control-plane"

EXPOSE 8080

# Non-root user is created for future use, but we run as root so the gateway
# can access /var/run/docker.sock for container discovery. Revisit once we
# have a rootless Docker API solution.
RUN adduser --disabled-password --gecos "" appuser
# USER appuser

CMD ["sh", "-c", "uvicorn cmd.gateway.main:app --host 0.0.0.0 --port ${ACP_BIND_PORT:-8080}"]
