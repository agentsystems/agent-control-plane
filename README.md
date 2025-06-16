# Agent Control Plane (runtime)

This repository contains the **gateway runtime** and libraries that power the Agent Platform. It no longer carries any Docker-Compose assets – those now live in the separate [`agent-platform-deployments`](https://github.com/agentsystems/agent-platform-deployments) repository.

---

## What’s in here

| Path | Purpose |
| ---- | ------- |
| `cmd/gateway/` | FastAPI gateway that discovers agent containers via Docker labels and proxies requests. |
| `model_router/` | (WIP) Simple model selection helper. |
| `audit/` | (Planned) Append-only Postgres audit writer. |
| `examples/agents/` | Minimal example agents built from the [agent-template](https://github.com/agentsystems/agent-template). |

---

## Building the gateway container

This repo is intended to be **built into a container image** and then orchestrated via the manifests in [`agent-platform-deployments`](https://github.com/agentsystems/agent-platform-deployments).

```
# clone
 git clone https://github.com/agentsystems/agent-control-plane.git
 cd agent-control-plane

# build image (adjust tag as needed)
 docker build -t agentsystems/agent-control-plane:0.1.0 .
```

Push the image to your registry of choice and update the image tag in the deployment repo’s Compose / Helm charts.

For quick code tweaks you can still run the gateway directly:

```
python -m venv .venv && source .venv/bin/activate
pip install -e .[dev]
uvicorn cmd.gateway.main:app --port 8080
```

But day-to-day you will spin it up via the deployment bundle, e.g.:

```
# in agent-platform-deployments
docker compose -f compose/local/docker-compose.yml up -d gateway
```

```
# clone
git clone https://github.com/agentsystems/agent-control-plane.git
cd agent-control-plane

# create venv & install
python -m venv .venv && source .venv/bin/activate
pip install -e .[dev]

# run gateway
uvicorn cmd.gateway.main:app --reload --port 8080
```

Run an agent alongside it (either with Docker or `uvicorn agent.main:app`) and the gateway will auto-register it if the container is labelled `agent.enabled=true` and exposes the port declared in `agent.port`.

For a full stack (gateway + Postgres + example agent, etc.) use the **agent-platform-deployments** repo:

```bash
# in a separate clone
cd agent-platform-deployments
docker compose -f compose/local/docker-compose.yml up -d
```

---

## Release checklist
1. Bump version in `pyproject.toml`.
2. Build & push Docker image: `docker build -t agentsystems/agent-control-plane:<tag> .`.
3. Create Git tag and release notes.
4. Update compose / Helm charts in `agent-platform-deployments` with the new `<tag>`.

---
MIT Licence © 2025 Agent Systems



Docker Desktop 4.24 + (includes Compose v2)

---



```bash
cd agentsystems/newstructure          # repo root
docker compose build                  # build agents + gateway
docker compose up -d                  # start stack (detached)
curl http://localhost:8080/agents     # → {"agents":[ ... ]}
```

Swagger for any agent:  
<http://localhost:8080/my_agent/docs>

---



```bash
curl -X POST http://localhost:8080/my_agent \
     -H "Content-Type: application/json" \
     -d '{"today":"2025-06-13"}'
```

---



```bash
docker compose down        # stop containers, keep images
docker system prune -f     # optional: clear build cache
```

---



```bash
# copy an existing folder
cp -R my_agent my_fourth_agent

# edit YAML metadata
sed -i '' 's/name:.*/name: my_fourth_agent/' my_fourth_agent/agent.yaml

# (optional) tweak greeting
sed -i '' 's/Hello!/Howdy from agent four!/' my_fourth_agent/main.py
```

Append this to **docker-compose.yml**:

```yaml
my_fourth_agent:
  build: ./my_fourth_agent
  expose: ["8000"]
  labels:
    - agent.enabled=true
    - agent.port=8000
```

Then:

```bash
docker compose build my_fourth_agent
docker compose up -d my_fourth_agent
curl http://localhost:8080/agents          # now lists my_fourth_agent
curl -X POST http://localhost:8080/my_fourth_agent \
     -H "Content-Type: application/json" \
     -d '{"today":"2025-06-13"}'
```

---



Gateway  → reverse proxy + label discovery (`/gateway`)  
Agents   → FastAPI apps in `my_*_agent/`, read their own `agent.yaml`  
Labels   → `agent.enabled=true` & `agent.port=8000` tell the gateway to route
