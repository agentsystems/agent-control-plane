# AgentSystems — Local Dev Stack

One gateway, many label-discoverable agents — all in Docker Compose.

---

## Prerequisites
Docker Desktop 4.24 + (includes Compose v2)

---

## 1  Build and start

    cd agentsystems/newstructure          # repo root
    docker compose build                  # build agents + gateway
    docker compose up -d                  # start stack (detached)
    curl http://localhost:8080/agents     # → {"agents":[ ... ]}

Swagger for any agent:  
<http://localhost:8080/my_agent/docs>

---

## 2  Invoke an agent

    curl -X POST http://localhost:8080/my_agent \
         -H "Content-Type: application/json" \
         -d '{"today":"2025-06-13"}'

---

## 3  Stop & clean

    docker compose down        # stop containers, keep images
    docker system prune -f     # optional: clear build cache

---

## 4  Add a new agent

    # copy an existing folder
    cp -R my_agent my_fourth_agent

    # edit YAML metadata
    sed -i '' 's/name:.*/name: my_fourth_agent/' my_fourth_agent/agent.yaml

    # (optional) tweak greeting
    sed -i '' 's/Hello!/Howdy from agent four!/' my_fourth_agent/main.py

Append this to **docker-compose.yml**:

    my_fourth_agent:
      build: ./my_fourth_agent
      expose: ["8000"]
      labels:
        - agent.enabled=true
        - agent.port=8000

Then:

    docker compose build my_fourth_agent
    docker compose up -d my_fourth_agent
    curl http://localhost:8080/agents          # now lists my_fourth_agent
    curl -X POST http://localhost:8080/my_fourth_agent \
         -H "Content-Type: application/json" \
         -d '{"today":"2025-06-13"}'

---

## Component map
Gateway  → reverse proxy + label discovery (`/gateway`)  
Agents   → FastAPI apps in `my_*_agent/`, read their own `agent.yaml`  
Labels   → `agent.enabled=true` & `agent.port=8000` tell the gateway to route
