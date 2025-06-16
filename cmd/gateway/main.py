from fastapi import FastAPI, Request
import httpx, docker, asyncio, uuid

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
    """Forward an invocation to the target agent.

    Requirements:
    • Request must include header `Authorization: Bearer <token>` (value not yet validated).
    • Gateway generates a `thread_id` (uuid4) and forwards it via header so
      agent can echo it back.
    """
    if agent not in AGENTS:
        return {"error": "unknown agent"}

    if not (auth := request.headers.get("Authorization", "")).startswith("Bearer "):
        return {"error": "missing or invalid Authorization bearer token"}

    payload = await request.json()
    thread_id = str(uuid.uuid4())
    async with httpx.AsyncClient() as cli:
        r = await cli.post(
            AGENTS[agent],
            json=payload,
            headers={"X-Thread-Id": thread_id},
            timeout=30,
        )
    # Ensure thread_id is present in response for clients
    resp_json = r.json()
    if "thread_id" not in resp_json:
        resp_json["thread_id"] = thread_id
    return resp_json

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

