import asyncio
import os
import uuid

import asyncpg
import docker
import httpx
import structlog
import datetime
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

logger = structlog.get_logger()

# ----------------------------------------------------------------------------
# Async invocation job store (DB-backed, falls back to in-memory dict).
# ----------------------------------------------------------------------------
JOBS: dict[str, dict] = {}  # thread_id -> record when DB unavailable

INV_STATE_QUEUED = "queued"
INV_STATE_RUNNING = "running"
INV_STATE_COMPLETED = "completed"
INV_STATE_FAILED = "failed"

app = FastAPI(title="Agent Gateway (label-discover)")

# Postgres connection (set via env, defaults valid for local compose)
PG_HOST = os.getenv("PG_HOST", "localhost")
PG_DB = os.getenv("PG_DB", "agent_cp")
PG_USER = os.getenv("PG_USER", "agent")
PG_PASSWORD = os.getenv("PG_PASSWORD", "agent")
DB_POOL: asyncpg.Pool | None = None

try:
    client = docker.DockerClient.from_env()
except Exception as e:
    logger.warning("docker_unavailable", error=str(e))
    client = None

AGENTS = {}  # name -> target URL


def refresh_agents():
    global AGENTS
    AGENTS = {}
    if client is None:
        logger.info("skip_agent_discovery_docker_unavailable")
        return
    for c in client.containers.list(filters={"label": "agent.enabled=true"}):

        name = c.labels.get("com.docker.compose.service", c.name)

        port = c.labels.get("agent.port", "8000")
        AGENTS[name] = f"http://{name}:{port}/invoke"
    logger.info("discovered_agents", agents=AGENTS)


refresh_agents()


# background watcher (optional)
@app.on_event("startup")
async def init_db():
    global DB_POOL
    retries = 10
    while retries:
        try:
            dsn = os.getenv("ACP_AUDIT_DSN")
            pool_kwargs = {"min_size": 1, "max_size": 5}
            if dsn:
                pool_kwargs["dsn"] = dsn
            else:
                pool_kwargs.update(
                    host=PG_HOST,
                    database=PG_DB,
                    user=PG_USER,
                    password=PG_PASSWORD,
                )
            DB_POOL = await asyncpg.create_pool(**pool_kwargs)
            break
        except Exception as e:
            logger.warning("postgres_not_ready_retry", error=str(e))
            retries -= 1
            await asyncio.sleep(2)
    if DB_POOL is None:
        logger.warning("audit_disabled_postgres_unreachable")
        return
    # Create extension/table if absent (idempotent)
    async with DB_POOL.acquire() as conn:
        await conn.execute(
            """
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

        -- async invocation tracking (simple for now; progress/result as JSONB)
        CREATE TABLE IF NOT EXISTS invocations (
          thread_id UUID PRIMARY KEY,
          agent TEXT NOT NULL,
          user_token TEXT NOT NULL,
          state TEXT NOT NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          started_at TIMESTAMPTZ,
          ended_at TIMESTAMPTZ,
          result JSONB,
          error JSONB,
          progress JSONB
        );
        """
        )


@app.on_event("startup")
async def watch_docker():
    if client is None:
        return
    loop = asyncio.get_event_loop()

    def _watch():
        for _ in client.events(decode=True):
            refresh_agents()

    loop.run_in_executor(None, _watch)


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


# ----------------------------------------------------------------------------
# Async invocation endpoints
# ----------------------------------------------------------------------------


async def _update_job_record(thread_id: str, **fields):
    """Helper to update a job row in DB or in-memory store."""
    if DB_POOL:
        sets = ", ".join(f"{k} = ${i+2}" for i, k in enumerate(fields))
        await DB_POOL.execute(
            f"UPDATE invocations SET {sets} WHERE thread_id = $1",
            thread_id,
            *fields.values(),
        )
    else:
        JOBS.setdefault(thread_id, {}).update(fields)


async def _insert_job_row(thread_id: str, agent: str, user_token: str):
    if DB_POOL:
        await DB_POOL.execute(
            """
            INSERT INTO invocations (thread_id, agent, user_token, state)
            VALUES ($1,$2,$3,$4)
            """,
            thread_id,
            agent,
            user_token,
            INV_STATE_QUEUED,
        )
    else:
        JOBS[thread_id] = {
            "thread_id": thread_id,
            "agent": agent,
            "user_token": user_token,
            "state": INV_STATE_QUEUED,
            "created_at": asyncio.get_event_loop().time(),
        }


async def _get_job(thread_id: str):
    if DB_POOL:
        row = await DB_POOL.fetchrow(
            "SELECT * FROM invocations WHERE thread_id=$1", thread_id
        )
        return dict(row) if row else None
    return JOBS.get(thread_id)


@app.post("/invoke/{agent}")
async def invoke_async(agent: str, request: Request):
    """Async-first invocation.

    Returns immediately with thread_id and status URL. A background task will
    forward the call to the target agent and persist the result.
    """
    if agent not in AGENTS:
        raise HTTPException(status_code=404, detail="unknown agent")

    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=400, detail="missing bearer token")

    payload = await request.json()
    # Extract optional sync flag; default False (async)
    sync_flag = bool(payload.pop("sync", False))

    thread_id = str(uuid.uuid4())
    await _insert_job_row(thread_id, agent, auth)

    async def _run_invocation():
        async with httpx.AsyncClient() as cli:
            return await cli.post(
                AGENTS[agent],
                json=payload,
                headers={"X-Thread-Id": thread_id},
                timeout=60,
            )

    # ------------------------------------------------------------------
    # SYNC mode: run inline and return full agent response immediately
    # ------------------------------------------------------------------
    if sync_flag:
        await _update_job_record(
            thread_id,
            state=INV_STATE_RUNNING,
            started_at=datetime.datetime.utcnow().isoformat(),
        )
        try:
            r = await _run_invocation()
            resp_json = r.json()
            await _update_job_record(
                thread_id,
                state=INV_STATE_COMPLETED,
                ended_at=datetime.datetime.utcnow().isoformat(),
                result=resp_json,
            )
            # Ensure thread id for compatibility
            resp_json.setdefault("thread_id", thread_id)
            return resp_json
        except Exception as e:
            await _update_job_record(
                thread_id,
                state=INV_STATE_FAILED,
                ended_at=datetime.datetime.utcnow().isoformat(),
                error={"message": str(e)},
            )
            raise

    # ------------------------------------------------------------------
    # ASYNC mode (default): fire worker and return handle
    # ------------------------------------------------------------------
    async def _worker():
        await _update_job_record(
            thread_id,
            state=INV_STATE_RUNNING,
            started_at=datetime.datetime.utcnow().isoformat(),
        )
        async with httpx.AsyncClient() as cli:
            try:
                r = await cli.post(
                    AGENTS[agent],
                    json=payload,
                    headers={"X-Thread-Id": thread_id},
                    timeout=60,
                )
                await _update_job_record(
                    thread_id,
                    state=INV_STATE_COMPLETED,
                    ended_at=datetime.datetime.utcnow().isoformat(),
                    result=r.json(),
                )
            except Exception as e:
                await _update_job_record(
                    thread_id,
                    state=INV_STATE_FAILED,
                    ended_at=datetime.datetime.utcnow().isoformat(),
                    error={"message": str(e)},
                )

    asyncio.create_task(_worker())

    return {
        "thread_id": thread_id,
        "status_url": f"/status/{thread_id}",
        "result_url": f"/result/{thread_id}",
    }


@app.get("/status/{thread_id}")
async def get_status(thread_id: str):
    """Lightweight polling endpoint – returns state & progress only."""
    job = await _get_job(thread_id)
    if not job:
        raise HTTPException(status_code=404, detail="unknown thread_id")
    return {
        "thread_id": thread_id,
        "state": job.get("state"),
        "progress": job.get("progress"),
        "error": job.get("error"),
    }


@app.get("/result/{thread_id}")
async def get_result(thread_id: str):
    """Return final result payload – large artefacts allowed."""
    job = await _get_job(thread_id)
    if not job:
        raise HTTPException(status_code=404, detail="unknown thread_id")
    return {
        "thread_id": thread_id,
        "result": job.get("result"),
        "error": job.get("error"),
    }


@app.post("/progress/{thread_id}")
async def post_progress(thread_id: str, request: Request):
    body = await request.json()
    if "progress" not in body:
        raise HTTPException(status_code=400, detail="missing progress field")
    job = await _get_job(thread_id)
    if not job:
        raise HTTPException(status_code=404, detail="unknown thread_id")
    await _update_job_record(thread_id, progress=body["progress"])
    return {"ok": True}


# ----------------------------------------------------------------------------


@app.get("/health")
async def health():
    return {"status": "ok", "agents": list(AGENTS.keys())}
