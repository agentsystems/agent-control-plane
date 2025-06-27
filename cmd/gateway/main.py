from fastapi import FastAPI, Request, HTTPException
import os, json, asyncpg
import httpx, docker, asyncio, uuid
from pydantic import BaseModel
from typing import Optional
import datetime

from fastapi.responses import HTMLResponse, JSONResponse

app = FastAPI(title="Agent Gateway (label-discover)")

# Postgres connection (set via env, defaults valid for local compose)
PG_HOST = os.getenv("PG_HOST", "localhost")
PG_DB = os.getenv("PG_DB", "agent_cp")
PG_USER = os.getenv("PG_USER", "agent")
PG_PASSWORD = os.getenv("PG_PASSWORD", "agent")
DB_POOL: asyncpg.Pool | None = None

# Cache of approved registry hostnames (e.g. "docker.io", "registry.agentsystems.ai")
ENABLED_REGISTRY_HOSTS: set[str] = set()

client = docker.DockerClient.from_env()
AGENTS = {} # name -> target URL


def extract_registry(image_ref: str) -> str:
    """Return the registry hostname part of a Docker image reference.

    Rules (heuristic, good enough for MVP):
    1. If the reference contains a host (has a '.' or ':' before first '/') → take that segment.
    2. Else treat it as Docker Hub → return 'docker.io'.
    """
    first = image_ref.split('/')[0]
    if ('.' in first) or (':' in first) or first == 'localhost':
        return first.lower()
    return 'docker.io'

async def refresh_enabled_registries(trigger_discovery: bool = True):
    """Populate ENABLED_REGISTRY_HOSTS from the DB.

    If ``trigger_discovery`` is True, refresh the in-memory AGENTS map right after
    updating the cache so that registry changes take immediate effect without
    waiting for a Docker event.
    """
    global ENABLED_REGISTRY_HOSTS
    if not DB_POOL:
        ENABLED_REGISTRY_HOSTS = {'docker.io'}  # sane default for early startup
        return
    rows = await DB_POOL.fetch("SELECT url FROM registries WHERE enabled = TRUE")
    hosts = set()
    for r in rows:
        url = r['url']
        # url stored with http/https scheme; strip scheme and any path
        host = url.split('://')[-1].split('/')[0].lower()
        hosts.add(host)
    if not hosts:
        hosts.add('docker.io')  # always allow hub if catalogue empty
    ENABLED_REGISTRY_HOSTS = hosts
    print('[gateway] Enabled registries ->', ENABLED_REGISTRY_HOSTS)
    if trigger_discovery:
        refresh_agents()


def refresh_agents(verbose: bool = False):
    """Discover containers with `agent.enabled=true` and rebuild the in-memory map.

    Containers whose image registry host is *not* in ENABLED_REGISTRY_HOSTS are ignored.
    """
    global AGENTS
    new_agents: dict[str, str] = {}
    for c in client.containers.list(filters={"label": "agent.enabled=true"}):
        # Determine registry host of the image
        image_ref = (c.image.tags[0] if c.image.tags else c.attrs.get('Config', {}).get('Image', ''))
        host = extract_registry(image_ref)
        if host not in ENABLED_REGISTRY_HOSTS:
            print(f"[gateway] Skipping agent {c.name} – registry '{host}' not enabled")
            continue

        name = c.labels.get("com.docker.compose.service", c.name)
        port = c.labels.get("agent.port", "8000")
        new_agents[name] = f"http://{name}:{port}/invoke"

    agents_changed = new_agents != AGENTS
    AGENTS = new_agents

    if verbose or agents_changed:
        print("Discovered agents →", AGENTS)

# Initial discovery with verbose logging (registry list may still be empty at this point)
refresh_agents(verbose=True)

# background watcher (optional)
@app.on_event("startup")
async def init_db():
    global DB_POOL
    retries = 10
    while retries:
        try:
            DB_POOL = await asyncpg.create_pool(
                host=PG_HOST,
                database=PG_DB,
                user=PG_USER,
                password=PG_PASSWORD,
                min_size=1,
                max_size=5,
            )
            break
        except Exception as e:
            print("[gateway] Postgres not ready, retrying…", e)
            retries -= 1
            await asyncio.sleep(2)
    if DB_POOL is None:
        print("[gateway] WARNING: audit logging disabled – could not connect to Postgres")
        return
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
        # Ensure registries table exists
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS registries (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          name TEXT NOT NULL,
          url TEXT NOT NULL,
          auth_type TEXT NOT NULL,
          username TEXT,
          password TEXT,
          enabled BOOLEAN NOT NULL DEFAULT TRUE,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """)

@app.on_event("startup")
async def watch_docker():
    loop = asyncio.get_event_loop()
    def _watch():
        for _ in client.events(decode=True):
            refresh_agents()
    loop.run_in_executor(None, _watch)

# ---------------- Registry catalogue -----------------
class RegistryCreate(BaseModel):
    name: str
    url: str
    auth_type: str = "none"  # none | basic | bearer
    username: Optional[str] = None
    password: Optional[str] = None
    enabled: bool = True

class RegistryUpdate(BaseModel):
    name: Optional[str] = None
    url: Optional[str] = None
    auth_type: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    enabled: Optional[bool] = None

@app.get("/registries")
async def list_registries():
    """Return all registries currently stored in the catalogue."""
    if DB_POOL:
        rows = await DB_POOL.fetch("SELECT * FROM registries ORDER BY created_at")
        return [dict(r) for r in rows]
    return []

@app.post("/registries")
async def create_registry(reg: RegistryCreate):
    if not DB_POOL:
        raise HTTPException(status_code=500, detail="DB not initialized")
    row = await DB_POOL.fetchrow(
        """INSERT INTO registries (name, url, auth_type, username, password, enabled)
           VALUES ($1,$2,$3,$4,$5,$6) RETURNING *""",
        reg.name, reg.url, reg.auth_type, reg.username, reg.password, reg.enabled,
    )
    await refresh_enabled_registries()
    return dict(row)

@app.patch("/registries/{registry_id}")
async def update_registry(registry_id: uuid.UUID, reg: RegistryUpdate):
    if not DB_POOL:
        raise HTTPException(status_code=500, detail="DB not initialized")
    updates = reg.dict(exclude_unset=True)
    if not updates:
        return {"updated": False}
    set_clauses = []
    values = []
    idx = 1
    for k, v in updates.items():
        set_clauses.append(f"{k}=${idx}")
        values.append(v)
        idx += 1
    set_clauses.append("updated_at=now()")
    query = "UPDATE registries SET " + ", ".join(set_clauses) + f" WHERE id=${idx} RETURNING *"
    values.append(registry_id)
    row = await DB_POOL.fetchrow(query, *values)
    if row is None:
        raise HTTPException(status_code=404, detail="registry not found")
    await refresh_enabled_registries()
    return dict(row)

@app.delete("/registries/{registry_id}")
async def delete_registry(registry_id: uuid.UUID):
    """Delete a registry entry by ID."""
    if not DB_POOL:
        raise HTTPException(status_code=500, detail="DB not initialized")
    row = await DB_POOL.fetchrow("DELETE FROM registries WHERE id=$1 RETURNING id", registry_id)
    if row is None:
        raise HTTPException(status_code=404, detail="registry not found")
    await refresh_enabled_registries()
    return {"deleted": True, "id": str(row["id"])}

@app.get("/{agent}/docs", response_class=HTMLResponse)
async def proxy_docs(agent: str):
    """Serve the FastAPI Swagger UI of a given agent through the gateway."""
    if agent not in AGENTS:
        raise HTTPException(status_code=404, detail="unknown agent")
    async with httpx.AsyncClient() as cli:
        r = await cli.get(f"http://{agent}:8000/docs", timeout=10)
        html = r.text.replace('url: "/openapi.json"', f'url: "/{agent}/openapi.json"')
        return HTMLResponse(content=html, status_code=r.status_code)


@app.get("/{agent}/openapi.json", response_class=JSONResponse)
async def proxy_openapi(agent: str):
    """Expose the agent's OpenAPI schema so Swagger UI loads correctly."""
    if agent not in AGENTS:
        raise HTTPException(status_code=404, detail="unknown agent")
    async with httpx.AsyncClient() as cli:
        r = await cli.get(f"http://{agent}:8000/openapi.json", timeout=10)
        return JSONResponse(content=r.json(), status_code=r.status_code)


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
    """Return the names of every discoverable agent.

    We refresh on-demand to avoid a race where the background Docker event
    listener temporarily clears AGENTS right before a request.
    """
    refresh_agents()
    return {"agents": list(AGENTS.keys())}

@app.get("/agents/{agent}")
async def agent_detail(agent: str):
    if agent not in AGENTS:
        return {"error": "unknown agent"}
    async with httpx.AsyncClient() as cli:
        r = await cli.get(f"http://{agent}:8000/metadata", timeout=10)
    return r.json()



# ---------------- Existing health endpoint -----------------
@app.get("/health")
async def health():
    return {"status": "ok", "agents": list(AGENTS.keys())}
