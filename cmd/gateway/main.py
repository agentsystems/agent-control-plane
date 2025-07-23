import asyncio
import os
import uuid

import asyncpg
import threading
import docker
import httpx
import fnmatch
from urllib.parse import urlparse
import re
import yaml
import structlog
import datetime
import time
from pydantic import BaseModel
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
AGENT_LOCK = threading.Lock()
# Map container IP -> agent name for proxy enforcement without headers
AGENT_IP_MAP: dict[str, str] = {}

# ---------------------------------------------------------------------------
# Proxy settings
PROXY_PORT = int(os.getenv("ACP_PROXY_PORT", "3128"))
PROXY_SERVER: asyncio.base_events.Server | None = None

# Outbound egress allowlist per agent (loaded from agentsystems-config.yml)
# ---------------------------------------------------------------------------
CONFIG_PATH = os.getenv(
    "AGENTSYSTEMS_CONFIG_PATH", "/etc/agentsystems/agentsystems-config.yml"
)
EGRESS_ALLOWLIST: dict[str, list[str]] = {}
IDLE_TIMEOUTS: dict[str, int] = {}
LAST_SEEN: dict[str, datetime.datetime] = {}
GLOBAL_IDLE_TIMEOUT = int(os.getenv("ACP_IDLE_TIMEOUT_MIN", "15"))


def _load_egress_allowlist(path: str) -> None:
    """Populate EGRESS_ALLOWLIST from YAML if present."""
    global EGRESS_ALLOWLIST
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
    except FileNotFoundError:
        logger.warning("config_not_found", path=path)
        return
    except Exception as e:
        logger.warning("config_read_failed", error=str(e))
        return

    allowlist: dict[str, list[str]] = {}
    for agent in raw.get("agents", []):
        name = agent.get("name")
        patterns = agent.get("egress_allowlist", []) or []
        if name:
            allowlist[name] = patterns
    EGRESS_ALLOWLIST = allowlist
    # extract per-agent idle timeout configurations
    idle_map: dict[str, int] = {}
    for agent in raw.get("agents", []):
        name = agent.get("name")
        if name and agent.get("idle_timeout") is not None:
            try:
                idle_map[name] = int(agent["idle_timeout"])
            except ValueError:
                logger.warning("config_idle_timeout_invalid", agent=name)
    global IDLE_TIMEOUTS
    IDLE_TIMEOUTS = idle_map
    logger.info(
        "config_loaded",
        egress_entries=len(EGRESS_ALLOWLIST),
        idle_entries=len(IDLE_TIMEOUTS),
    )


def _is_allowed(agent: str, url: str) -> bool:
    patterns = EGRESS_ALLOWLIST.get(agent, [])
    if not patterns:
        return False

    host = urlparse(url).hostname or ""
    for pat in patterns:
        # Allow matching on full URL or just hostname
        if fnmatch.fnmatch(url, pat) or fnmatch.fnmatch(host, pat):
            return True
    return False


def refresh_agents():
    """Discover agent containers and update the in-memory cache atomically."""
    global AGENTS
    if client is None:
        logger.info("skip_agent_discovery_docker_unavailable")
        return

    discovered: dict[str, str] = {}
    ip_map: dict[str, str] = {}
    for c in client.containers.list(filters={"label": "agent.enabled=true"}):
        name = c.labels.get("com.docker.compose.service", c.name)
        port = c.labels.get("agent.port", "8000")
        discovered[name] = f"http://{name}:{port}/invoke"
        # Capture container IPv4 (first network entry)
        try:
            net_info = next(
                iter(c.attrs.get("NetworkSettings", {}).get("Networks", {}).values())
            )
            ip_addr = net_info.get("IPAddress")
            if ip_addr:
                ip_map[ip_addr] = name
        except Exception:
            pass

    # Atomic swap under a lock to avoid readers seeing a partially built dict
    with AGENT_LOCK:
        AGENTS = discovered
        global AGENT_IP_MAP
        AGENT_IP_MAP = ip_map

    logger.info("discovered_agents", agents=AGENTS)


# ---------------------------------------------------------------------------
# Lazy start helper and idle reaper
# ---------------------------------------------------------------------------


def ensure_agent_running(agent: str) -> bool:
    """Start container if stopped; returns True if agent running."""
    if client is None:
        return False

    refresh_agents()
    if agent in AGENTS:
        return True

    try:
        # Find container by compose service label
        containers = client.containers.list(
            all=True,
            filters={
                "label": ["agent.enabled=true", f"com.docker.compose.service={agent}"]
            },
        )
        if not containers:
            logger.warning("agent_container_not_found", agent=agent)
            return False
        c = containers[0]
        c.start()
        logger.info("agent_started_lazy", agent=agent)
    except Exception as e:
        logger.warning("agent_lazy_start_failed", agent=agent, error=str(e))
        return False

    # Poll for readiness (up to 30 s)
    for _ in range(30):
        time.sleep(1)
        refresh_agents()
        if agent in AGENTS:
            return True
    logger.warning("agent_lazy_start_timeout", agent=agent)
    return False


async def _idle_reaper():
    """Background task that stops idle containers."""
    if client is None:
        return
    while True:
        await asyncio.sleep(60)
        now = datetime.datetime.utcnow()
        for c in client.containers.list(filters={"label": "agent.enabled=true"}):
            name = c.labels.get("com.docker.compose.service", c.name)
            last = LAST_SEEN.get(name)
            timeout_min = IDLE_TIMEOUTS.get(name, GLOBAL_IDLE_TIMEOUT)
            if last is None:
                continue  # never invoked
            if (now - last).total_seconds() >= timeout_min * 60:
                try:
                    c.stop()
                    logger.info(
                        "agent_stopped_idle", agent=name, idle_minutes=timeout_min
                    )
                    # remove from cache so next invoke will restart
                    refresh_agents()
                except Exception as e:
                    logger.warning("agent_idle_stop_failed", agent=name, error=str(e))


refresh_agents()

# ---------------------------------------------------------------------------
# Forward proxy implementation (HTTP/1.1 CONNECT support)
# ---------------------------------------------------------------------------


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    try:
        while True:
            data = await reader.read(4096)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except Exception:
        pass
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


async def _handle_proxy(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    peer = writer.get_extra_info("peername")
    peer_ip = peer[0] if peer else ""
    agent = AGENT_IP_MAP.get(peer_ip)

    try:
        req_line_bytes = await reader.readline()
        if not req_line_bytes:
            writer.close()
            await writer.wait_closed()
            return
        req_line = req_line_bytes.decode("latin1").rstrip("\r\n")
        parts = req_line.split(" ")
        if len(parts) < 3:
            writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            await writer.drain()
            writer.close()
            await writer.wait_closed()
            return
        method, target, _ = parts

        headers = {}
        while True:
            line = await reader.readline()
            if line in {b"\r\n", b""}:
                break
            k, v = line.decode("latin1").rstrip("\r\n").split(":", 1)
            headers[k.strip()] = v.strip()
            if not agent and k.lower() == "x-agent-name":
                agent = v.strip()

        if not agent:
            writer.write(
                b"HTTP/1.1 400 Bad Request\r\n\r\nMissing X-Agent-Name header\r\n"
            )
            await writer.drain()
            writer.close()
            await writer.wait_closed()
            return

        if method.upper() == "CONNECT":
            host_port = target
            m = re.match(r"([^:]+):(\d+)", host_port)
            if not m:
                writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
                await writer.drain()
                writer.close()
                await writer.wait_closed()
                return
            dest_host, dest_port = m.group(1), int(m.group(2))
            test_url = f"https://{dest_host}"
            if not _is_allowed(agent, test_url):
                writer.write(b"HTTP/1.1 403 Forbidden\r\n\r\n")
                await writer.drain()
                writer.close()
                await writer.wait_closed()
                return
            try:
                remote_reader, remote_writer = await asyncio.open_connection(
                    dest_host, dest_port
                )
            except Exception:
                writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                await writer.drain()
                writer.close()
                await writer.wait_closed()
                return
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            await asyncio.gather(
                _pipe(reader, remote_writer),
                _pipe(remote_reader, writer),
            )
            return
        else:
            full_url = target
            if not _is_allowed(agent, full_url):
                writer.write(b"HTTP/1.1 403 Forbidden\r\n\r\n")
                await writer.drain()
                writer.close()
                await writer.wait_closed()
                return

            content_length = int(headers.get("Content-Length", "0"))
            body = b""
            if content_length:
                body = await reader.readexactly(content_length)

            async with httpx.AsyncClient(timeout=30) as hc:
                try:
                    resp = await hc.request(
                        method, full_url, headers=headers, content=body
                    )
                except Exception:
                    writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                    await writer.drain()
                    writer.close()
                    await writer.wait_closed()
                    return

            status_line = (
                f"HTTP/1.1 {resp.status_code} {resp.reason_phrase}\r\n".encode()
            )
            writer.write(status_line)
            for k, v in resp.headers.items():
                if k.lower() in {"transfer-encoding", "connection", "content-encoding"}:
                    continue
                writer.write(f"{k}: {v}\r\n".encode())
            writer.write(b"\r\n")
            async for chunk in resp.aiter_bytes():
                writer.write(chunk)
            await writer.drain()
            writer.close()
            await writer.wait_closed()
            return
    except Exception:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


async def _start_proxy_server():
    global PROXY_SERVER
    PROXY_SERVER = await asyncio.start_server(
        _handle_proxy, host="0.0.0.0", port=PROXY_PORT
    )
    logger.info("proxy_started", port=PROXY_PORT)


@app.on_event("startup")
async def _proxy_bg():
    asyncio.create_task(_start_proxy_server())


# background watcher (optional)
@app.on_event("startup")
async def init_db():
    # Load outbound egress allowlist into memory once the app starts
    _load_egress_allowlist(CONFIG_PATH)
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

    # start idle reaper background task
    asyncio.create_task(_idle_reaper())


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


class AgentsFilter(BaseModel):
    state: str = "running"  # running | idle | all


@app.get("/agents")
async def list_agents():
    """Return the names of every discoverable agent.

    We refresh on-demand to avoid a race where the background Docker event
    listener temporarily clears AGENTS right before a request.
    """
    refresh_agents()
    return {"agents": list(AGENTS.keys())}


@app.post("/agents")
async def list_agents_filtered(filter: AgentsFilter):
    """Return agents filtered by state via JSON body."""
    refresh_agents()
    running_set = set(AGENTS.keys())

    if client is None:
        # Docker unavailable – only running known
        selected = running_set if filter.state in {"running", "all"} else set()
        return {"agents": sorted(selected)}

    # All agent-labeled containers (running or stopped)
    all_ctrs = {
        c.labels.get("com.docker.compose.service", c.name)
        for c in client.containers.list(
            all=True, filters={"label": "agent.enabled=true"}
        )
    }
    idle_set = all_ctrs - running_set

    if filter.state == "running":
        selected = running_set
    elif filter.state == "idle":
        selected = idle_set
    else:
        selected = running_set | idle_set

    return {"agents": sorted(selected)}


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

    # record last activity
    LAST_SEEN[agent] = datetime.datetime.utcnow()

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
                try:
                    parsed = r.json()
                except ValueError:
                    # Provide clearer message when agent returns non-JSON (e.g. 403 text)
                    parsed = None
                if r.status_code >= 400 or parsed is None:
                    await _update_job_record(
                        thread_id,
                        state=INV_STATE_FAILED,
                        ended_at=datetime.datetime.utcnow().isoformat(),
                        error={
                            "status": r.status_code,
                            "body": r.text[:500],  # truncate large bodies
                            "message": (
                                "agent attempted outbound request to non-allowlisted URL"
                                if r.status_code == 403
                                else "agent returned non-JSON or error status"
                            ),
                        },
                    )
                else:
                    await _update_job_record(
                        thread_id,
                        state=INV_STATE_COMPLETED,
                        ended_at=datetime.datetime.utcnow().isoformat(),
                        result=parsed,
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


@app.on_event("shutdown")
async def _graceful_shutdown():
    """Ensure DB pool and Docker client are closed on application shutdown."""
    global DB_POOL
    if DB_POOL is not None:
        try:
            await DB_POOL.close()
            logger.info("db_pool_closed")
        except Exception as e:
            logger.warning("db_pool_close_failed", error=str(e))
    if PROXY_SERVER is not None:
        try:
            PROXY_SERVER.close()
            await PROXY_SERVER.wait_closed()
            logger.info("proxy_server_closed")
        except Exception:
            logger.warning("proxy_server_close_failed")

    if client is not None:
        try:
            client.close()
            logger.info("docker_client_closed")
        except Exception as e:
            logger.warning("docker_client_close_failed", error=str(e))


@app.get("/debug/egress-allowlist")
async def debug_egress_allowlist():
    """Return the current in-memory egress allowlist."""
    return EGRESS_ALLOWLIST


@app.post("/egress")
async def proxy_egress(request: Request):
    """Forward outbound HTTP requests on behalf of an agent after allowlist check."""
    agent_name = request.headers.get("X-Agent-Name") or AGENT_IP_MAP.get(
        request.client.host
    )
    if not agent_name:
        raise HTTPException(status_code=400, detail="missing X-Agent-Name header")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json body")

    url: str | None = body.get("url")
    if not url:
        raise HTTPException(status_code=400, detail="url required in body")
    method: str = body.get("method", "GET").upper()
    payload = body.get("payload")

    patterns = EGRESS_ALLOWLIST.get(agent_name, [])
    if not patterns or not any(fnmatch.fnmatch(url, p) for p in patterns):
        logger.warning("egress_blocked", agent=agent_name, url=url)
        raise HTTPException(status_code=403, detail="destination not allowlisted")

    try:
        async with httpx.AsyncClient(timeout=15) as hc:
            resp = await hc.request(method, url, json=payload)
    except Exception as e:
        logger.warning("egress_upstream_error", error=str(e))
        raise HTTPException(status_code=502, detail="upstream fetch failed")

    # Basic response passthrough (status + text). In future may stream/binary.
    return JSONResponse(
        status_code=resp.status_code,
        content={"status_code": resp.status_code, "body": resp.text},
    )


@app.get("/health")
async def health():
    return {"status": "ok", "agents": list(AGENTS.keys())}
