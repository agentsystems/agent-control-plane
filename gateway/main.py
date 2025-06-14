from fastapi import FastAPI, Request
import httpx, docker, asyncio

app = FastAPI(title="Agent Gateway (label-discover)")

client = docker.DockerClient.from_env()
AGENTS = {}            # name -> target URL

def refresh_agents():
    global AGENTS
    AGENTS = {}
    for c in client.containers.list(
            filters={"label": "agent.enabled=true"}):

        name = c.labels.get("com.docker.compose.service", c.name)

        port = c.labels.get("agent.port", "8000")
        AGENTS[name] = f"http://{name}:{port}/invoke"
    print("Discovered agents →", AGENTS)

refresh_agents()

# background watcher (optional but nice)
@app.on_event("startup")
async def watch_docker():
    loop = asyncio.get_event_loop()
    def _watch():
        for _ in client.events(decode=True):
            refresh_agents()
    loop.run_in_executor(None, _watch)

@app.post("/{agent}")
async def proxy(agent: str, request: Request):
    if agent not in AGENTS:
        return {"error": "unknown agent"}
    payload = await request.json()
    async with httpx.AsyncClient() as cli:
        r = await cli.post(AGENTS[agent], json=payload, timeout=30)
    return r.json()

@app.get("/agents")
async def list_agents():
    """Return the names of every discoverable agent."""
    return {"agents": list(AGENTS.keys())}

@app.get("/agents/{agent}")
async def agent_detail(agent: str):
    if agent not in AGENTS:
        return {"error": "unknown agent"}
    async with httpx.AsyncClient() as cli:
        r = await cli.get(f"http://{agent}:8000/metadata", timeout=10)
    return r.json()

@app.get("/health")
async def health():
    return {"status": "ok", "agents": list(AGENTS.keys())}

