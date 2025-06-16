from fastapi import FastAPI, Request
import os, json, asyncpg
import httpx, docker, asyncio, uuid

app = FastAPI(title="Agent Gateway (label-discover)")

# ── Postgres connection (set via env, defaults valid for local compose) ──
PG_HOST = os.getenv("PG_HOST", "localhost")
PG_DB = os.getenv("PG_DB", "agent_cp")
PG_USER = os.getenv("PG_USER", "agent")
PG_PASSWORD = os.getenv("PG_PASSWORD", "agent")
DB_POOL: asyncpg.Pool | None = None

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
async def init_db():
    global DB_POOL
    DB_POOL = await asyncpg.create_pool(
        host=PG_HOST, database=PG_DB, user=PG_USER, password=PG_PASSWORD, min_size=1, max_size=5
    )
    # Create extension/table if absent (idempotent)
    async with DB_POOL.acquire() as conn:
        await conn.execute("""
        CREATE EXTENSION IF NOT EXISTS "pgcrypto";
        CREATE TABLE IF NOT EXISTS audit_log (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          timestamp TIMESTAMPTZ NOT NULL DEFAULT now(),
          user_token TEXT NOT NULL,
          thread_id UUID NOT NULL,
          actor TEXT NOT NULL,
          action TEXT NOT NULL,
          resource TEXT NOT NULL,
          status_code SMALLINT NOT NULL,
          payload JSONB,
          error_msg TEXT
        );
        """)

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

    # audit: request
    if DB_POOL:
        await DB_POOL.execute(
            """INSERT INTO audit_log (user_token, thread_id, actor, action, resource, status_code, payload)
                 VALUES ($1,$2,'gateway','invoke_request',$3,0,$4)""",
            auth, thread_id, f"{agent}/invoke", json.dumps(payload)
        )

    async with httpx.AsyncClient() as cli:
        try:
            r = await cli.post(
                AGENTS[agent],
                json=payload,
                headers={"X-Thread-Id": thread_id},
                timeout=30,
            )
            resp_json = r.json()
        except Exception as e:
            if DB_POOL:
                await DB_POOL.execute(
                    """INSERT INTO audit_log (user_token, thread_id, actor, action, resource, status_code, payload, error_msg)
                         VALUES ($1,$2,$3,'invoke_response',$4,500,NULL,$5)""",
                    auth, thread_id, agent, f"{agent}/invoke", str(e)
                )
            raise

    # Ensure thread_id is present in response for clients
    if "thread_id" not in resp_json:
        resp_json["thread_id"] = thread_id

    # audit: response
    if DB_POOL:
        await DB_POOL.execute(
            """INSERT INTO audit_log (user_token, thread_id, actor, action, resource, status_code, payload)
                 VALUES ($1,$2,$3,'invoke_response',$4,$5,$6)""",
            auth,
            thread_id,
            agent,
            f"{agent}/invoke",
            r.status_code,
            json.dumps(resp_json),
        )

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

