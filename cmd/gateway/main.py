import asyncio
import os
import uuid
import json
from fastapi import UploadFile

import httpx
import structlog
import datetime
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from cmd.gateway.models import (
    AgentsFilter,
    INV_STATE_RUNNING,
    INV_STATE_COMPLETED,
    INV_STATE_FAILED,
)
from cmd.gateway.exceptions import (
    agent_not_found,
    bad_request,
)
from cmd.gateway import database, docker_discovery, egress, lifecycle, proxy

logger = structlog.get_logger()

app = FastAPI(title="Agent Gateway (label-discover)")


# ---------------------------------------------------------------------------
# Lazy start helper and idle reaper
# ---------------------------------------------------------------------------


docker_discovery.refresh_agents()

# ---------------------------------------------------------------------------
# Forward proxy implementation (HTTP/1.1 CONNECT support)
# ---------------------------------------------------------------------------


# background watcher (optional)
@app.on_event("startup")
async def init_db():
    # Load outbound egress allowlist into memory once the app starts
    egress.load_egress_allowlist()

    # Start proxy server with the loaded allowlist
    proxy.set_egress_allowlist(egress.get_allowlist())
    asyncio.create_task(proxy._proxy_bg())

    # Initialize database connection pool
    if not await database.init_pool():
        logger.warning("audit_disabled_postgres_unreachable")
        return

    # Create extension/table if absent (idempotent)
    async with database.DB_POOL.acquire() as conn:
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
async def startup_event():
    # Start Docker watching in the background
    asyncio.create_task(docker_discovery.watch_docker())

    # Start idle reaper background task
    asyncio.create_task(lifecycle.idle_reaper())


@app.get("/{agent}/docs", response_class=HTMLResponse)
async def proxy_docs(agent: str):
    """Serve the FastAPI Swagger UI of a given agent through the gateway."""
    if agent not in docker_discovery.AGENTS:
        raise agent_not_found(agent)
    async with httpx.AsyncClient() as cli:
        r = await cli.get(f"http://{agent}:8000/docs", timeout=10)
        html = r.text.replace('url: "/openapi.json"', f'url: "/{agent}/openapi.json"')
        return HTMLResponse(content=html, status_code=r.status_code)


@app.get("/{agent}/openapi.json", response_class=JSONResponse)
async def proxy_openapi(agent: str):
    """Expose the agent's OpenAPI schema so Swagger UI loads correctly."""
    if agent not in docker_discovery.AGENTS:
        raise agent_not_found(agent)
    async with httpx.AsyncClient() as cli:
        r = await cli.get(f"http://{agent}:8000/openapi.json", timeout=10)
        return JSONResponse(content=r.json(), status_code=r.status_code)


@app.get("/agents")
async def list_agents():
    """Return the names of every discoverable agent.

    We refresh on-demand to avoid a race where the background Docker event
    listener temporarily clears docker_discovery.AGENTS right before a request.
    """
    docker_discovery.refresh_agents()

    configured_set = docker_discovery.CONFIGURED_AGENT_NAMES
    running_set = set(docker_discovery.AGENTS.keys())

    # Determine stopped/not-created sets using Docker info when available
    stopped_set: set[str]
    if docker_discovery.client is not None:
        all_ctrs = {
            c.labels.get("com.docker.compose.service", c.name)
            for c in docker_discovery.client.containers.list(
                all=True, filters={"label": "agent.enabled=true"}
            )
        }
        stopped_set = all_ctrs - running_set
    else:
        # Docker unavailable – nothing running or stopped
        stopped_set = set()

    # Build response objects with state per agent
    names_union = configured_set.union(running_set).union(stopped_set)
    agents_info: list[dict[str, str]] = []
    for name in sorted(names_union):
        if name in running_set:
            state = "running"
        elif name in stopped_set:
            state = "stopped"
        else:
            state = "not-created"
        agents_info.append({"name": name, "state": state})

    return {"agents": agents_info}


@app.post("/agents")
async def list_agents_filtered(filter: AgentsFilter):
    """Return agents filtered by state via JSON body."""
    docker_discovery.refresh_agents()
    running_set = set(docker_discovery.AGENTS.keys())
    configured_set = docker_discovery.CONFIGURED_AGENT_NAMES

    if docker_discovery.client is None:
        # Docker unavailable – rely on configured list only
        idle_set = configured_set - running_set
        if filter.state == "running":
            selected = running_set
        elif filter.state == "idle":
            selected = idle_set
        else:
            selected = running_set | idle_set
        return {"agents": sorted(selected)}

    # All agent-labeled containers (running or stopped)
    all_ctrs = {
        c.labels.get("com.docker.compose.service", c.name)
        for c in docker_discovery.client.containers.list(
            all=True, filters={"label": "agent.enabled=true"}
        )
    }
    not_created_set = configured_set - all_ctrs
    idle_set = (all_ctrs - running_set) | not_created_set

    if filter.state == "running":
        selected = running_set
    elif filter.state == "idle":
        selected = idle_set
    else:
        selected = running_set | idle_set

    return {"agents": sorted(selected)}


@app.get("/agents/{agent}")
async def agent_detail(agent: str):
    if agent not in docker_discovery.AGENTS:
        return {"error": "unknown agent"}
    async with httpx.AsyncClient() as cli:
        r = await cli.get(f"http://{agent}:8000/metadata", timeout=10)
    return r.json()


# ----------------------------------------------------------------------------
# Async invocation endpoints
# ----------------------------------------------------------------------------


@app.post("/invoke/{agent}")
async def invoke_async(agent: str, request: Request):
    """Async-first invocation.

    Returns immediately with thread_id and status URL. A background task will
    forward the call to the target agent and persist the result.
    """
    # Refresh cache; if agent not running, attempt lazy start. Only containers
    # labelled as agents will be started – if no such container exists we 404.
    docker_discovery.refresh_agents()
    if agent not in docker_discovery.AGENTS:
        if docker_discovery.ensure_agent_running(agent):
            docker_discovery.refresh_agents()
        else:
            raise HTTPException(status_code=404, detail="unknown agent")

    # record last activity
    lifecycle.record_agent_activity(agent)

    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise bad_request("missing bearer token")

    # ------------------------------------------------------------------
    # Parse request body – JSON or multipart with optional file upload
    # ------------------------------------------------------------------
    content_type = request.headers.get("content-type", "").lower()
    payload: dict
    uploaded_files: list[UploadFile] = []
    if content_type.startswith("multipart/"):
        form = await request.form()
        # Gather every UploadFile across all fields (supports multiple files per field)
        for field in form:
            for item in form.getlist(field):
                # Check if item is an UploadFile (either by type or duck typing)
                if isinstance(item, UploadFile) or (
                    hasattr(item, "filename") and hasattr(item, "read")
                ):
                    uploaded_files.append(item)
        # Look for JSON body part named 'json'
        json_part = form.get("json")
        if json_part:
            try:
                payload = json.loads(json_part)
            except Exception:
                payload = {}
        else:
            payload = {}
    else:
        payload = await request.json()
        uploaded_files = []

    # Extract optional sync flag; default False (async)
    sync_flag = bool(payload.pop("sync", False))

    thread_id = str(uuid.uuid4())
    await database.insert_job_row(thread_id, agent, auth)

    # ------------------------------------------------------------------
    # Stage uploaded file(s) into artifacts volume if present
    # ------------------------------------------------------------------
    MAX_MB = int(os.getenv("ACP_MAX_UPLOAD_MB", "200"))
    MAX_BYTES = MAX_MB * 1024 * 1024

    # Always create thread base directory with correct ownership (thread-centric structure)
    thread_base_dir = os.path.join("/artifacts", thread_id)
    os.makedirs(thread_base_dir, exist_ok=True, mode=0o777)
    try:
        os.chmod(thread_base_dir, 0o777)
    except PermissionError:
        pass
    try:
        import shutil

        shutil.chown(thread_base_dir, user=1001, group=1001)
    except (OSError, PermissionError):
        # If chown fails, continue - init container should have set base permissions
        pass

    # ------------------------------------------------------------------
    # Ensure thread-centric input/output subdirectories exist
    # These are required even for JSON-only requests so that agent
    # containers running as UID 1001 can write their outputs without
    # hitting PermissionError.  We create both `in` and `out` folders.
    # ------------------------------------------------------------------
    in_dir = os.path.join("/artifacts", thread_id, "in")
    out_dir = os.path.join("/artifacts", thread_id, "out")
    for _d in (in_dir, out_dir):
        os.makedirs(_d, exist_ok=True, mode=0o777)
        try:
            shutil.chown(_d, user=1001, group=1001)  # type: ignore[arg-type]
        except (OSError, PermissionError):
            pass
        # Ensure world-writable if chown failed or gateway runs non-root
        try:
            os.chmod(_d, 0o777)
        except PermissionError:
            pass

    # If there are uploaded files, stage them into the `in` directory
    if uploaded_files:
        artifacts_dir = in_dir
        try:
            shutil.chown(artifacts_dir, user=1001, group=1001)
        except (OSError, PermissionError):
            # If chown fails, continue - init container should have set base permissions
            pass
        for up in uploaded_files:
            # Sanitize filename to prevent path traversal
            fname = os.path.basename(up.filename or "input.bin")
            if fname in {"", ".", ".."}:
                continue
            data = await up.read()
            if len(data) > MAX_BYTES:
                raise HTTPException(
                    status_code=413, detail=f"file '{fname}' exceeds {MAX_MB} MB limit"
                )
            with open(os.path.join(artifacts_dir, fname), "wb") as fh:
                fh.write(data)

    async def _run_invocation():
        async with httpx.AsyncClient() as cli:
            return await cli.post(
                docker_discovery.AGENTS[agent],
                json=payload,
                headers={"X-Thread-Id": thread_id},
                timeout=60,
            )

    # ------------------------------------------------------------------
    # SYNC mode: run inline and return full agent response immediately
    # ------------------------------------------------------------------
    if sync_flag:
        await database.update_job_record(
            thread_id,
            state=INV_STATE_RUNNING,
            started_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        )
        try:
            r = await _run_invocation()
            # Check if response is successful and contains JSON
            if r.status_code != 200:
                raise Exception(f"Agent returned status {r.status_code}: {r.text}")

            try:
                resp_json = r.json()
            except Exception as json_err:
                raise Exception(
                    f"Agent returned non-JSON response: {r.text[:200]}..."
                ) from json_err
            await database.update_job_record(
                thread_id,
                state=INV_STATE_COMPLETED,
                ended_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                result=resp_json,
            )
            # Ensure thread id for compatibility
            resp_json.setdefault("thread_id", thread_id)
            return resp_json
        except Exception as e:
            await database.update_job_record(
                thread_id,
                state=INV_STATE_FAILED,
                ended_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                error={"message": str(e)},
            )
            raise

    # ------------------------------------------------------------------
    # ASYNC mode (default): fire worker and return handle
    # ------------------------------------------------------------------
    async def _worker():
        await database.update_job_record(
            thread_id,
            state=INV_STATE_RUNNING,
            started_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        )
        async with httpx.AsyncClient() as cli:
            try:
                r = await cli.post(
                    docker_discovery.AGENTS[agent],
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
                    await database.update_job_record(
                        thread_id,
                        state=INV_STATE_FAILED,
                        ended_at=datetime.datetime.now(
                            datetime.timezone.utc
                        ).isoformat(),
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
                    await database.update_job_record(
                        thread_id,
                        state=INV_STATE_COMPLETED,
                        ended_at=datetime.datetime.now(
                            datetime.timezone.utc
                        ).isoformat(),
                        result=parsed,
                    )
            except Exception as e:
                await database.update_job_record(
                    thread_id,
                    state=INV_STATE_FAILED,
                    ended_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
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
    job = await database.get_job(thread_id)
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
    job = await database.get_job(thread_id)
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
    job = await database.get_job(thread_id)
    if not job:
        raise HTTPException(status_code=404, detail="unknown thread_id")
    await database.update_job_record(thread_id, progress=body["progress"])
    return {"ok": True}


# ----------------------------------------------------------------------------


@app.on_event("shutdown")
async def _graceful_shutdown():
    """Ensure DB pool and Docker client are closed on application shutdown."""
    await database.close_pool()
    if proxy.PROXY_SERVER is not None:
        try:
            proxy.PROXY_SERVER.close()
            await proxy.PROXY_SERVER.wait_closed()
            logger.info("proxy_server_closed")
        except Exception:
            logger.warning("proxy_server_close_failed")

    if docker_discovery.client is not None:
        try:
            docker_discovery.client.close()
            logger.info("docker_client_closed")
        except Exception as e:
            logger.warning("docker_client_close_failed", error=str(e))


@app.get("/debug/egress-allowlist")
async def debug_egress_allowlist():
    """Return the current in-memory egress allowlist."""
    return egress.get_allowlist()


@app.post("/egress")
async def proxy_egress(request: Request):
    """Forward outbound HTTP requests on behalf of an agent after allowlist check."""
    agent_name = request.headers.get(
        "X-Agent-Name"
    ) or docker_discovery.AGENT_IP_MAP.get(request.client.host)
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

    if not egress.is_allowed(agent_name, url):
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
    return {"status": "ok", "agents": list(docker_discovery.AGENTS.keys())}
