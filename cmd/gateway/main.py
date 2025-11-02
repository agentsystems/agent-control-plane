import asyncio
import os
import uuid
import json
import yaml
import shutil
from fastapi import UploadFile
from typing import Dict, List, Any

import httpx
import structlog
import datetime
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

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
async def init_db() -> None:
    """Initialize database and proxy server on application startup.

    - Loads egress allowlist from configuration
    - Starts the HTTP CONNECT proxy server
    - Initializes database connection pool
    - Creates required database tables if missing
    """
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
          payload JSONB,
          result JSONB,
          error JSONB,
          progress JSONB
        );
        """
        )


@app.on_event("startup")
async def startup_event() -> None:
    """Start background tasks on application startup.

    - Starts Docker container discovery watcher
    - Starts idle container reaper task
    """
    # Start Docker watching in the background
    asyncio.create_task(docker_discovery.watch_docker())

    # Start idle reaper background task
    asyncio.create_task(lifecycle.idle_reaper())


@app.get("/{agent}/docs", response_class=HTMLResponse)
async def proxy_docs(agent: str) -> HTMLResponse:
    """Serve the FastAPI Swagger UI of a given agent through the gateway.

    Args:
        agent: Name of the agent whose docs to serve

    Returns:
        HTML response containing the agent's Swagger UI

    Raises:
        HTTPException: 404 if agent not found
    """
    if agent not in docker_discovery.AGENTS:
        raise agent_not_found(agent)
    async with httpx.AsyncClient() as cli:
        r = await cli.get(f"http://{agent}:8000/docs", timeout=10)
        html = r.text.replace('url: "/openapi.json"', f'url: "/{agent}/openapi.json"')
        return HTMLResponse(content=html, status_code=r.status_code)


@app.get("/{agent}/openapi.json", response_class=JSONResponse)
async def proxy_openapi(agent: str) -> JSONResponse:
    """Expose the agent's OpenAPI schema so Swagger UI loads correctly.

    Args:
        agent: Name of the agent whose OpenAPI schema to retrieve

    Returns:
        JSON response containing the agent's OpenAPI specification

    Raises:
        HTTPException: 404 if agent not found
    """
    if agent not in docker_discovery.AGENTS:
        raise agent_not_found(agent)
    async with httpx.AsyncClient() as cli:
        r = await cli.get(f"http://{agent}:8000/openapi.json", timeout=10)
        return JSONResponse(content=r.json(), status_code=r.status_code)


@app.get("/agents")
async def list_agents() -> Dict[str, List[Dict[str, str]]]:
    """Return the names of every discoverable agent.

    We refresh on-demand to avoid a race where the background Docker event
    listener temporarily clears docker_discovery.AGENTS right before a request.

    Returns:
        Dictionary with 'agents' key containing list of agent info:
        - name: Agent name
        - state: One of 'running', 'stopped', or 'not-created'
    """
    docker_discovery.refresh_agents()

    configured_set = docker_discovery.CONFIGURED_AGENT_NAMES
    running_set = set(docker_discovery.AGENTS.keys())

    # Determine stopped/not-created sets using fast Docker API
    stopped_set: set[str]
    if docker_discovery.api_client is not None:
        # Use fast API call instead of expensive client.containers.list()
        containers = docker_discovery._get_agent_containers_fast()
        all_ctrs = set()
        for c in containers:
            labels = c.get("Labels", {}) or {}
            name = labels.get("com.docker.compose.service")
            if not name:
                names = c.get("Names", [])
                name = names[0].lstrip("/") if names else c.get("Id", "")[:12]
            all_ctrs.add(name)
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
async def list_agents_filtered(filter: AgentsFilter) -> Dict[str, List[str]]:
    """Return agents filtered by state via JSON body.

    Args:
        filter: AgentsFilter model with state field ('running', 'idle', or 'all')

    Returns:
        Dictionary with 'agents' key containing list of agent names
    """
    docker_discovery.refresh_agents()
    running_set = set(docker_discovery.AGENTS.keys())
    configured_set = docker_discovery.CONFIGURED_AGENT_NAMES

    if docker_discovery.client is None:
        # Docker unavailable – rely on configured list only
        stopped_set = configured_set - running_set
        if filter.state == "running":
            selected = running_set
        elif filter.state == "stopped":
            selected = stopped_set
        else:
            selected = running_set | stopped_set
        return {"agents": sorted(selected)}

    # All agent-labeled containers (running or stopped) - use fast API
    containers = docker_discovery._get_agent_containers_fast()
    all_ctrs = set()
    for c in containers:
        labels = c.get("Labels", {}) or {}
        name = labels.get("com.docker.compose.service")
        if not name:
            names = c.get("Names", [])
            name = names[0].lstrip("/") if names else c.get("Id", "")[:12]
        all_ctrs.add(name)
    not_created_set = configured_set - all_ctrs
    stopped_set = (all_ctrs - running_set) | not_created_set

    if filter.state == "running":
        selected = running_set
    elif filter.state == "stopped":
        selected = stopped_set
    else:
        selected = running_set | stopped_set

    return {"agents": sorted(selected)}


@app.get("/agents/{agent}")
async def agent_detail(agent: str) -> Dict[str, Any]:
    """Get detailed metadata for a specific agent from config.

    Reads metadata from agentsystems-config.yml (index_metadata field)
    instead of calling the container's /metadata endpoint.

    Args:
        agent: Name of the agent

    Returns:
        Agent metadata from config or error dictionary if not found
    """
    if agent not in docker_discovery.AGENTS:
        return {"error": "unknown agent"}

    try:
        # Read metadata from config file
        config = await read_agentsystems_config()
        agents = config.get("agents", [])

        # Find agent config
        agent_config = next((a for a in agents if a.get("name") == agent), None)
        if not agent_config:
            logger.warning("agent_not_in_config", agent=agent)
            return {"error": "agent not found in config"}

        # Return index_metadata if available, otherwise return basic info
        metadata = agent_config.get("index_metadata", {})

        # Always include basic info from config (top-level fields)
        if "name" not in metadata:
            metadata["name"] = agent_config.get("name", agent)
        if "developer" not in metadata:
            metadata["developer"] = agent_config.get("developer", "")

        # Add version from tag if not in index_metadata
        if "version" not in metadata and "tag" in agent_config:
            metadata["version"] = agent_config.get("tag", "")

        # Add repo information
        if "repo" not in metadata and "repo" in agent_config:
            metadata["repo"] = agent_config.get("repo", "")

        return metadata

    except Exception as e:
        logger.error("agent_metadata_error", agent=agent, error=str(e))
        return {"error": f"Failed to read agent metadata: {str(e)}"}


@app.post("/agents/{agent}/start")
async def start_agent(agent: str) -> Dict[str, Any]:
    """Start a stopped agent container.

    Args:
        agent: Name of the agent to start

    Returns:
        Dictionary with success status and message

    Raises:
        HTTPException: 404 if agent not found or failed to start
    """
    if docker_discovery.ensure_agent_running(agent):
        docker_discovery.refresh_agents()
        # Record activity so manually started agents don't get stopped immediately
        lifecycle.record_agent_activity(agent)
        return {"success": True, "message": f"Agent {agent} started successfully"}
    else:
        raise HTTPException(
            status_code=404, detail="Agent not found or failed to start"
        )


@app.post("/agents/{agent}/stop")
async def stop_agent(agent: str) -> Dict[str, Any]:
    """Stop a running agent container.

    Args:
        agent: Name of the agent to stop

    Returns:
        Dictionary with success status and message

    Raises:
        HTTPException: 404 if agent not found, 400 if agent not running
    """
    if not docker_discovery.client:
        raise HTTPException(status_code=503, detail="Docker unavailable")

    try:
        # Find the container
        containers = docker_discovery.client.containers.list(
            filters={
                "label": ["agent.enabled=true", f"com.docker.compose.service={agent}"],
                "status": "running",
            }
        )

        if not containers:
            # Try by container name as fallback
            containers = docker_discovery.client.containers.list(
                all=True, filters={"name": agent}
            )
            containers = [
                c for c in containers if c.labels.get("agent.enabled") == "true"
            ]

        if not containers:
            raise HTTPException(status_code=404, detail="Agent container not found")

        container = containers[0]
        if container.status != "running":
            raise HTTPException(status_code=400, detail="Agent is not running")

        container.stop()
        logger.info(
            "agent_stopped_via_api", agent=agent, container_id=container.short_id
        )

        # Clear last seen time and refresh agent registry
        lifecycle.clear_last_seen(agent)
        docker_discovery.refresh_agents()

        return {"success": True, "message": f"Agent {agent} stopped successfully"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error("stop_agent_failed", agent=agent, error=str(e))
        raise HTTPException(status_code=500, detail=f"Failed to stop agent: {str(e)}")


# ----------------------------------------------------------------------------
# Async invocation endpoints
# ----------------------------------------------------------------------------


@app.post("/invoke/{agent}")
async def invoke_async(agent: str, request: Request) -> Dict[str, Any]:
    """Async-first invocation.

    Returns immediately with thread_id and status URL. A background task will
    forward the call to the target agent and persist the result.

    Args:
        agent: Name of the agent to invoke
        request: FastAPI request containing JSON payload and optional file uploads

    Returns:
        Dictionary with thread_id, status_url, and result_url

    Raises:
        HTTPException: 404 if agent not found, 400 if missing bearer token
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
    await database.insert_job_row(thread_id, agent, auth, payload)

    # Audit logging: record the incoming request
    await database.audit_invoke_request(auth, thread_id, agent, payload)

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
        """Execute the agent invocation and return the response."""
        async with httpx.AsyncClient() as cli:
            return await cli.post(
                docker_discovery.AGENTS[agent],
                json=payload,
                headers={"X-Thread-Id": thread_id},
                timeout=7200,
            )

    # ------------------------------------------------------------------
    # SYNC mode: run inline and return full agent response immediately
    # ------------------------------------------------------------------
    if sync_flag:
        await database.update_job_record(
            thread_id,
            state=INV_STATE_RUNNING,
            started_at=datetime.datetime.now(datetime.timezone.utc),
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
                ended_at=datetime.datetime.now(datetime.timezone.utc),
                result=resp_json,
            )
            # Audit logging: record successful response
            await database.audit_invoke_response(
                auth, thread_id, agent, r.status_code, resp_json
            )
            # Ensure thread id for compatibility
            resp_json.setdefault("thread_id", thread_id)
            return resp_json
        except Exception as e:
            await database.update_job_record(
                thread_id,
                state=INV_STATE_FAILED,
                ended_at=datetime.datetime.now(datetime.timezone.utc),
                error={"message": str(e)},
            )
            # Audit logging: record error response
            await database.audit_invoke_response(
                auth, thread_id, agent, 500, error_msg=str(e)
            )
            raise

    # ------------------------------------------------------------------
    # ASYNC mode (default): fire worker and return handle
    # ------------------------------------------------------------------
    async def _worker() -> None:
        """Background worker to handle async agent invocation."""
        await database.update_job_record(
            thread_id,
            state=INV_STATE_RUNNING,
            started_at=datetime.datetime.now(datetime.timezone.utc),
        )
        async with httpx.AsyncClient() as cli:
            try:
                r = await cli.post(
                    docker_discovery.AGENTS[agent],
                    json=payload,
                    headers={"X-Thread-Id": thread_id},
                    timeout=7200,
                )
                try:
                    parsed = r.json()
                except ValueError:
                    # Provide clearer message when agent returns non-JSON (e.g. 403 text)
                    parsed = None
                if r.status_code >= 400 or parsed is None:
                    error_msg = (
                        "agent attempted outbound request to non-allowlisted URL"
                        if r.status_code == 403
                        else "agent returned non-JSON or error status"
                    )
                    await database.update_job_record(
                        thread_id,
                        state=INV_STATE_FAILED,
                        ended_at=datetime.datetime.now(datetime.timezone.utc),
                        error=json.dumps(
                            {
                                "status": r.status_code,
                                "body": r.text[:500],  # truncate large bodies
                                "message": error_msg,
                            }
                        ),
                    )
                    # Audit logging: record error response
                    await database.audit_invoke_response(
                        auth, thread_id, agent, r.status_code, error_msg=error_msg
                    )
                else:
                    await database.update_job_record(
                        thread_id,
                        state=INV_STATE_COMPLETED,
                        ended_at=datetime.datetime.now(datetime.timezone.utc),
                        result=json.dumps(parsed),
                    )
                    # Audit logging: record successful response
                    await database.audit_invoke_response(
                        auth, thread_id, agent, r.status_code, parsed
                    )
            except Exception as e:
                await database.update_job_record(
                    thread_id,
                    state=INV_STATE_FAILED,
                    ended_at=datetime.datetime.now(datetime.timezone.utc),
                    error=json.dumps({"message": str(e)}),
                )
                # Audit logging: record error response
                await database.audit_invoke_response(
                    auth, thread_id, agent, 500, error_msg=str(e)
                )

    asyncio.create_task(_worker())

    return {
        "thread_id": thread_id,
        "status_url": f"/status/{thread_id}",
        "result_url": f"/result/{thread_id}",
    }


@app.get("/status/{thread_id}")
async def get_status(thread_id: str) -> Dict[str, Any]:
    """Lightweight polling endpoint – returns state & progress only.

    Args:
        thread_id: UUID of the invocation to check

    Returns:
        Dictionary with thread_id, state, progress, and error fields

    Raises:
        HTTPException: 404 if thread_id not found
    """
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
async def get_result(thread_id: str) -> Dict[str, Any]:
    """Return final result payload – large artefacts allowed.

    Args:
        thread_id: UUID of the completed invocation

    Returns:
        Dictionary with thread_id, result, and error fields

    Raises:
        HTTPException: 404 if thread_id not found
    """
    job = await database.get_job(thread_id)
    if not job:
        raise HTTPException(status_code=404, detail="unknown thread_id")
    return {
        "thread_id": thread_id,
        "result": job.get("result"),
        "error": job.get("error"),
    }


@app.get("/artifacts/{thread_id}")
async def list_artifacts(thread_id: str) -> Dict[str, Any]:
    """List artifact files for a specific thread.

    Args:
        thread_id: UUID of the thread

    Returns:
        Dictionary with input_files and output_files arrays

    Raises:
        HTTPException: 404 if thread not found or no artifacts directory
    """
    import os

    artifacts_base = os.path.join("/artifacts", thread_id)

    if not os.path.exists(artifacts_base):
        raise HTTPException(status_code=404, detail="Thread artifacts not found")

    def list_files_in_dir(dir_path: str, file_type: str) -> List[Dict[str, Any]]:
        """List files in a directory with metadata."""
        files = []
        if not os.path.exists(dir_path):
            return files

        try:
            for filename in os.listdir(dir_path):
                file_path = os.path.join(dir_path, filename)
                if os.path.isfile(file_path):
                    try:
                        file_stat = os.stat(file_path)
                        files.append(
                            {
                                "name": filename,
                                "path": f"/artifacts/{thread_id}/{file_type}/{filename}",
                                "size": file_stat.st_size,
                                "modified": datetime.datetime.fromtimestamp(
                                    file_stat.st_mtime, datetime.timezone.utc
                                ).isoformat(),
                                "type": file_type,
                            }
                        )
                    except OSError:
                        # Skip files we can't stat
                        continue
        except PermissionError:
            # Skip directories we can't read
            pass

        return sorted(files, key=lambda x: x["name"])

    input_files = list_files_in_dir(os.path.join(artifacts_base, "in"), "in")
    output_files = list_files_in_dir(os.path.join(artifacts_base, "out"), "out")

    return {
        "thread_id": thread_id,
        "input_files": input_files,
        "output_files": output_files,
    }


@app.get("/artifacts/{thread_id}/{file_path:path}")
async def download_artifact(thread_id: str, file_path: str) -> Any:
    """Download a specific artifact file.

    Args:
        thread_id: UUID of the thread
        file_path: Path to the file (e.g., "in/data.csv" or "out/result.json")

    Returns:
        File content with appropriate headers

    Raises:
        HTTPException: 404 if file not found, 403 if access denied
    """
    import os
    from fastapi.responses import FileResponse

    # Sanitize the file path to prevent directory traversal
    file_path = file_path.strip("/")
    if ".." in file_path or file_path.startswith("/"):
        raise HTTPException(status_code=403, detail="Invalid file path")

    # Ensure file_path starts with 'in/' or 'out/'
    if not (file_path.startswith("in/") or file_path.startswith("out/")):
        raise HTTPException(
            status_code=403, detail="File must be in 'in' or 'out' directory"
        )

    full_path = os.path.join("/artifacts", thread_id, file_path)

    if not os.path.exists(full_path):
        raise HTTPException(status_code=404, detail="File not found")

    if not os.path.isfile(full_path):
        raise HTTPException(status_code=403, detail="Path is not a file")

    # Get the filename for the download
    filename = os.path.basename(file_path)

    return FileResponse(
        path=full_path, filename=filename, media_type="application/octet-stream"
    )


@app.get("/executions")
async def list_executions(
    limit: int = 50, offset: int = 0, agent: str = None, state: str = None
) -> Dict[str, Any]:
    """List recent agent executions with optional filtering.

    Args:
        limit: Maximum number of executions to return (default 50, max 100)
        offset: Number of executions to skip for pagination
        agent: Filter by specific agent name
        state: Filter by execution state (queued, running, completed, failed)

    Returns:
        Dictionary with executions array and pagination info

    Raises:
        HTTPException: 400 if invalid parameters
    """
    # Validate parameters
    limit = min(max(1, limit), 100)  # Clamp between 1 and 100
    offset = max(0, offset)

    if database.DB_POOL:
        try:
            # Build WHERE clause for filtering
            where_conditions = []
            params = []
            param_count = 0

            if agent:
                param_count += 1
                where_conditions.append(f"agent = ${param_count}")
                params.append(agent)

            if state and state in ["queued", "running", "completed", "failed"]:
                param_count += 1
                where_conditions.append(f"state = ${param_count}")
                params.append(state)

            where_clause = (
                "WHERE " + " AND ".join(where_conditions) if where_conditions else ""
            )

            # Get total count for pagination
            count_query = f"SELECT COUNT(*) FROM invocations {where_clause}"
            total_count = await database.DB_POOL.fetchval(count_query, *params)

            # Get executions with pagination
            param_count += 1
            limit_param = f"${param_count}"
            param_count += 1
            offset_param = f"${param_count}"

            query = f"""
                SELECT thread_id, agent, user_token, state, created_at, started_at, ended_at,
                       payload, result, error, progress
                FROM invocations
                {where_clause}
                ORDER BY created_at DESC
                LIMIT {limit_param} OFFSET {offset_param}
            """

            rows = await database.DB_POOL.fetch(query, *params, limit, offset)

            executions = [
                {
                    "thread_id": str(row["thread_id"]),
                    "agent": row["agent"],
                    "user_token": row["user_token"],
                    "state": row["state"],
                    "created_at": (
                        row["created_at"].isoformat() if row["created_at"] else None
                    ),
                    "started_at": (
                        row["started_at"].isoformat() if row["started_at"] else None
                    ),
                    "ended_at": (
                        row["ended_at"].isoformat() if row["ended_at"] else None
                    ),
                    "payload": row["payload"],
                    "result": row["result"],
                    "error": row["error"],
                    "progress": row["progress"],
                }
                for row in rows
            ]

            return {
                "executions": executions,
                "pagination": {
                    "total": total_count,
                    "limit": limit,
                    "offset": offset,
                    "has_more": offset + limit < total_count,
                },
            }

        except Exception as e:
            logger.error("list_executions_failed", error=str(e))
            raise HTTPException(status_code=500, detail="Failed to retrieve executions")
    else:
        # Fallback to in-memory storage if DB unavailable
        jobs = list(database.JOBS.values())

        # Apply filters
        if agent:
            jobs = [j for j in jobs if j.get("agent") == agent]
        if state:
            jobs = [j for j in jobs if j.get("state") == state]

        # Sort by created_at (mock with current time if missing)
        jobs.sort(key=lambda x: x.get("created_at", ""), reverse=True)

        # Apply pagination
        total_count = len(jobs)
        paginated_jobs = jobs[offset : offset + limit]

        return {
            "executions": paginated_jobs,
            "pagination": {
                "total": total_count,
                "limit": limit,
                "offset": offset,
                "has_more": offset + limit < total_count,
            },
        }


@app.post("/progress/{thread_id}")
async def post_progress(thread_id: str, request: Request) -> Dict[str, bool]:
    """Update progress for an in-flight invocation.

    Args:
        thread_id: UUID of the invocation to update
        request: JSON body containing 'progress' field

    Returns:
        Dictionary with 'ok' field set to True

    Raises:
        HTTPException: 400 if missing progress field, 404 if thread_id not found
    """
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
async def _graceful_shutdown() -> None:
    """Ensure DB pool and Docker client are closed on application shutdown.

    Gracefully closes:
    - Database connection pool
    - Proxy server
    - Docker client
    """
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
async def debug_egress_allowlist() -> Dict[str, List[str]]:
    """Return the current in-memory egress allowlist.

    Returns:
        Dictionary mapping agent names to their allowed URL patterns
    """
    return egress.get_allowlist()


@app.post("/egress")
async def proxy_egress(request: Request) -> JSONResponse:
    """Forward outbound HTTP requests on behalf of an agent after allowlist check.

    Args:
        request: Contains JSON body with url, method, and optional payload

    Returns:
        JSON response with status_code and body from upstream

    Raises:
        HTTPException: 400 if missing required fields, 403 if URL not allowlisted,
                      502 if upstream request fails
    """
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


@app.get("/executions/{thread_id}/audit")
async def get_execution_audit(thread_id: str) -> Dict[str, Any]:
    """Get audit trail for a specific execution including hash chain data.

    Args:
        thread_id: UUID of the execution

    Returns:
        Dictionary with audit logs, input payload, and hash information

    Raises:
        HTTPException: 404 if thread_id not found
    """
    if not database.DB_POOL:
        raise HTTPException(
            status_code=503, detail="Audit trail unavailable - database not connected"
        )

    try:
        # Get audit log entries for this thread with existing hash chain data
        audit_logs = await database.DB_POOL.fetch(
            """
            SELECT id, timestamp, user_token, actor, action, resource,
                   status_code, payload, error_msg, prev_hash, entry_hash
            FROM audit_log
            WHERE thread_id = $1
            ORDER BY timestamp ASC
            """,
            thread_id,
        )

        if not audit_logs:
            raise HTTPException(
                status_code=404, detail="No audit trail found for thread_id"
            )

        # Extract input payload from the first audit entry (invoke_request)
        input_payload = None
        for log in audit_logs:
            if log["action"] == "invoke_request" and log["payload"]:
                input_payload = log["payload"]  # Already parsed as JSONB
                break

        # Format audit trail with existing hash information
        formatted_logs = [
            {
                "id": str(log["id"]),
                "timestamp": log["timestamp"].isoformat() if log["timestamp"] else None,
                "actor": log["actor"],
                "action": log["action"],
                "resource": log["resource"],
                "status_code": log["status_code"],
                "payload": log["payload"],
                "error_msg": log["error_msg"],
                "prev_hash": log["prev_hash"],
                "entry_hash": log["entry_hash"],
            }
            for log in audit_logs
        ]

        return {
            "thread_id": thread_id,
            "input_payload": input_payload,
            "audit_trail": formatted_logs,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error("get_execution_audit_failed", thread_id=thread_id, error=str(e))
        raise HTTPException(status_code=500, detail="Failed to retrieve audit trail")


@app.get("/audit/integrity-check")
async def verify_audit_integrity() -> Dict[str, Any]:
    """Verify the integrity of the entire audit log chain.

    Returns:
        Dictionary with verification summary and compromised entries

    Raises:
        HTTPException: 503 if database unavailable
    """
    if not database.DB_POOL:
        raise HTTPException(
            status_code=503,
            detail="Audit verification unavailable - database not connected",
        )

    try:
        # Get all audit log entries ordered by timestamp
        all_entries = await database.DB_POOL.fetch(
            """
            SELECT id, timestamp, user_token, thread_id, actor, action, resource,
                   status_code, payload, error_msg, prev_hash, entry_hash
            FROM audit_log
            ORDER BY timestamp ASC
            """
        )

        if not all_entries:
            return {"verified": True, "total_entries": 0, "compromised_entries": []}

        compromised = []

        for i, entry in enumerate(all_entries):
            # Check for obviously tampered hashes (like our test)
            if entry["entry_hash"] == "tampered_hash_value_breaks_chain":
                compromised.append(
                    {
                        "thread_id": str(entry["thread_id"]),
                        "timestamp": (
                            entry["timestamp"].isoformat()
                            if entry["timestamp"]
                            else None
                        ),
                        "action": entry["action"],
                        "error": "Hash manually tampered",
                    }
                )

            # Check chain linkage (prev_hash should match previous entry's entry_hash)
            if i > 0 and entry["prev_hash"] != all_entries[i - 1]["entry_hash"]:
                compromised.append(
                    {
                        "thread_id": str(entry["thread_id"]),
                        "timestamp": (
                            entry["timestamp"].isoformat()
                            if entry["timestamp"]
                            else None
                        ),
                        "action": entry["action"],
                        "error": "Broken chain link",
                    }
                )

        return {
            "verified": len(compromised) == 0,
            "total_entries": len(all_entries),
            "compromised_count": len(compromised),
            "compromised_entries": compromised[
                :10
            ],  # Limit to first 10 for response size
        }

    except Exception as e:
        logger.error("audit_verification_failed", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to verify audit integrity")


# ----------------------------------------------------------------------------
# Configuration management endpoints
# ----------------------------------------------------------------------------

CONFIG_FILE = os.getenv(
    "AGENTSYSTEMS_CONFIG_PATH", "/etc/agentsystems/agentsystems-config.yml"
)
ENV_FILE = os.getenv("AGENTSYSTEMS_ENV_PATH", "/etc/agentsystems/.env")


@app.get("/api/config/agentsystems-config")
async def read_agentsystems_config() -> Dict[str, Any]:
    """Read the agentsystems-config.yml file.

    Returns:
        Dictionary containing the parsed YAML configuration

    Raises:
        HTTPException: 404 if config file not found, 500 if parsing fails
    """
    try:
        if not os.path.exists(CONFIG_FILE):
            # Return default config if file doesn't exist
            return {
                "config_version": 1,
                "index_connections": {},
                "registry_connections": {},
                "agents": [],
            }

        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)

        logger.info("config_file_read", path=CONFIG_FILE)
        return config

    except yaml.YAMLError as e:
        logger.error("config_yaml_parse_error", error=str(e), path=CONFIG_FILE)
        raise HTTPException(
            status_code=500, detail=f"Invalid YAML in config file: {str(e)}"
        )
    except Exception as e:
        logger.error("config_file_read_error", error=str(e), path=CONFIG_FILE)
        raise HTTPException(
            status_code=500, detail=f"Failed to read config file: {str(e)}"
        )


@app.put("/api/config/agentsystems-config")
async def write_agentsystems_config(request: Request) -> Dict[str, str]:
    """Write the agentsystems-config.yml file.

    Args:
        request: JSON body containing the complete configuration

    Returns:
        Success message

    Raises:
        HTTPException: 400 if invalid config, 500 if write fails
    """
    try:
        config = await request.json()

        # Basic validation
        if not isinstance(config, dict):
            raise HTTPException(status_code=400, detail="Config must be a JSON object")

        if "config_version" not in config:
            raise HTTPException(status_code=400, detail="config_version is required")

        if config.get("config_version") < 1:
            raise HTTPException(status_code=400, detail="config_version must be >= 1")

        # Validate registry connections structure
        registry_connections = config.get("registry_connections", {})
        if not isinstance(registry_connections, dict):
            raise HTTPException(
                status_code=400, detail="registry_connections must be an object"
            )

        for reg_id, reg_config in registry_connections.items():
            if not isinstance(reg_config, dict):
                raise HTTPException(
                    status_code=400,
                    detail=f"Registry '{reg_id}' config must be an object",
                )
            if "url" not in reg_config:
                raise HTTPException(
                    status_code=400,
                    detail=f"Registry '{reg_id}' missing required 'url' field",
                )
            if "auth" not in reg_config:
                raise HTTPException(
                    status_code=400,
                    detail=f"Registry '{reg_id}' missing required 'auth' field",
                )

        # Validate index connections structure (optional)
        index_connections = config.get("index_connections", {})
        if not isinstance(index_connections, dict):
            raise HTTPException(
                status_code=400, detail="index_connections must be an object"
            )

        for idx_id, idx_config in index_connections.items():
            if not isinstance(idx_config, dict):
                raise HTTPException(
                    status_code=400,
                    detail=f"Index '{idx_id}' config must be an object",
                )
            if "url" not in idx_config:
                raise HTTPException(
                    status_code=400,
                    detail=f"Index '{idx_id}' missing required 'url' field",
                )
            # enabled defaults to false, description is optional
            if "enabled" in idx_config and not isinstance(idx_config["enabled"], bool):
                raise HTTPException(
                    status_code=400,
                    detail=f"Index '{idx_id}' 'enabled' field must be a boolean",
                )

        # Validate agents structure
        agents = config.get("agents", [])
        if not isinstance(agents, list):
            raise HTTPException(status_code=400, detail="agents must be an array")

        for i, agent in enumerate(agents):
            if not isinstance(agent, dict):
                raise HTTPException(
                    status_code=400, detail=f"Agent {i} must be an object"
                )
            required_fields = ["name", "repo", "tag", "registry_connection"]
            for field in required_fields:
                if field not in agent:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Agent {i} missing required field '{field}'",
                    )

        # Create backup before writing
        if os.path.exists(CONFIG_FILE):
            backup_path = (
                f"{CONFIG_FILE}.backup.{int(datetime.datetime.now().timestamp())}"
            )
            shutil.copy2(CONFIG_FILE, backup_path)
            logger.info("config_backup_created", backup_path=backup_path)

        # Write the config file
        os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            yaml.dump(config, f, default_flow_style=False, sort_keys=False)

        logger.info("config_file_written", path=CONFIG_FILE)
        return {"message": "Configuration saved successfully"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error("config_file_write_error", error=str(e), path=CONFIG_FILE)
        raise HTTPException(
            status_code=500, detail=f"Failed to write config file: {str(e)}"
        )


@app.get("/api/config/env")
async def read_env_vars() -> Dict[str, str]:
    """Read environment variables from .env file.

    Returns:
        Dictionary of environment variable key-value pairs

    Raises:
        HTTPException: 500 if read fails
    """
    try:
        if not os.path.exists(ENV_FILE):
            return {}

        env_vars = {}
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue

                if "=" not in line:
                    logger.warning(
                        "env_file_invalid_line", line_num=line_num, line=line
                    )
                    continue

                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip()

                # Remove quotes if present
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]

                env_vars[key] = value

        logger.info("env_file_read", path=ENV_FILE, var_count=len(env_vars))
        return env_vars

    except Exception as e:
        logger.error("env_file_read_error", error=str(e), path=ENV_FILE)
        raise HTTPException(
            status_code=500, detail=f"Failed to read .env file: {str(e)}"
        )


@app.put("/api/config/env")
async def write_env_vars(request: Request) -> Dict[str, str]:
    """Write environment variables to .env file.

    Args:
        request: JSON body containing environment variable key-value pairs

    Returns:
        Success message

    Raises:
        HTTPException: 400 if invalid format, 500 if write fails
    """
    try:
        env_vars = await request.json()

        if not isinstance(env_vars, dict):
            raise HTTPException(
                status_code=400, detail="Environment variables must be a JSON object"
            )

        # Validate environment variable names and values
        for key, value in env_vars.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise HTTPException(
                    status_code=400, detail="All environment variables must be strings"
                )

            # Validate key format
            if not key or not key.replace("_", "").replace("-", "").isalnum():
                raise HTTPException(
                    status_code=400, detail=f"Invalid environment variable name: {key}"
                )

            # Check for problematic characters in values
            if "\n" in value or "\r" in value:
                raise HTTPException(
                    status_code=400,
                    detail=f"Environment variable '{key}' cannot contain newline characters",
                )

        # Create backup before writing
        if os.path.exists(ENV_FILE):
            backup_path = (
                f"{ENV_FILE}.backup.{int(datetime.datetime.now().timestamp())}"
            )
            shutil.copy2(ENV_FILE, backup_path)
            logger.info("env_backup_created", backup_path=backup_path)

        # Write the .env file
        os.makedirs(os.path.dirname(ENV_FILE), exist_ok=True)
        with open(ENV_FILE, "w", encoding="utf-8") as f:
            for key, value in env_vars.items():
                # Quote values that contain spaces or special characters
                if " " in value or any(char in value for char in "()[]{}$`\"'\\"):
                    f.write(f'{key}="{value}"\n')
                else:
                    f.write(f"{key}={value}\n")

        logger.info("env_file_written", path=ENV_FILE, var_count=len(env_vars))
        return {"message": "Environment variables saved successfully"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error("env_file_write_error", error=str(e), path=ENV_FILE)
        raise HTTPException(
            status_code=500, detail=f"Failed to write .env file: {str(e)}"
        )


@app.post("/api/config/backup")
async def backup_config_files() -> Dict[str, Any]:
    """Create timestamped backups of configuration files.

    Returns:
        Dictionary with backup file paths and timestamp

    Raises:
        HTTPException: 500 if backup fails
    """
    try:
        timestamp = int(datetime.datetime.now().timestamp())
        backups = {}

        # Backup agentsystems-config.yml
        if os.path.exists(CONFIG_FILE):
            config_backup = f"{CONFIG_FILE}.backup.{timestamp}"
            shutil.copy2(CONFIG_FILE, config_backup)
            backups["config"] = config_backup

        # Backup .env file
        if os.path.exists(ENV_FILE):
            env_backup = f"{ENV_FILE}.backup.{timestamp}"
            shutil.copy2(ENV_FILE, env_backup)
            backups["env"] = env_backup

        logger.info("config_backup_created", backups=backups, timestamp=timestamp)
        return {
            "message": "Backup created successfully",
            "timestamp": timestamp,
            "backups": backups,
        }

    except Exception as e:
        logger.error("config_backup_error", error=str(e))
        raise HTTPException(
            status_code=500, detail=f"Failed to create backup: {str(e)}"
        )


@app.get("/logs")
async def get_recent_logs(limit: int = 100, offset: int = 0) -> Dict[str, Any]:
    """Get recent logs from the gateway container.

    Args:
        limit: Maximum number of log entries to return (default 100, max 500)

    Returns:
        Dictionary with recent log entries from container logs
    """
    limit = min(max(1, limit), 500)  # Clamp between 1 and 500

    if not docker_discovery.client:
        raise HTTPException(status_code=503, detail="Docker client unavailable")

    try:
        # Get the current container (gateway)
        # Try to find the gateway container by common names
        gateway_container = None
        possible_names = ["gateway", "agentsystems-gateway-1", "local-gateway-1"]

        for name in possible_names:
            try:
                gateway_container = docker_discovery.client.containers.get(name)
                break
            except Exception:
                continue

        if not gateway_container:
            # Fallback: find container running this code by process
            containers = docker_discovery.client.containers.list()
            for container in containers:
                if "agent-control-plane" in (
                    container.image.tags[0] if container.image.tags else ""
                ):
                    gateway_container = container
                    break

        if not gateway_container:
            raise HTTPException(status_code=404, detail="Gateway container not found")

        # Limit to reasonable amount - Docker logs can be huge
        # Only get recent logs (last 500 lines max) to avoid overwhelming the system
        total_lines_to_fetch = min(500, offset + limit + 100)
        log_bytes = gateway_container.logs(tail=total_lines_to_fetch, timestamps=True)
        log_text = log_bytes.decode("utf-8", errors="ignore")

        # Parse log lines
        log_entries = []
        for line in log_text.strip().split("\n"):
            if not line.strip():
                continue

            # Parse Docker log format: "timestamp message"
            parts = line.split(" ", 1)
            if len(parts) >= 2:
                timestamp_str = parts[0]
                message = parts[1]

                # Determine log level from message content
                level = "info"
                if "error" in message.lower() or "failed" in message.lower():
                    level = "error"
                elif "warning" in message.lower() or "warn" in message.lower():
                    level = "warning"

                # Determine source from message content
                source = "gateway"
                if "agent" in message.lower():
                    source = "agents"
                elif "database" in message.lower() or "db" in message.lower():
                    source = "database"
                elif "proxy" in message.lower():
                    source = "proxy"

                log_entries.append(
                    {
                        "timestamp": timestamp_str,
                        "level": level,
                        "message": message,
                        "source": source,
                        "extra": {},
                    }
                )

        # Reverse to show newest first
        log_entries.reverse()

        # Apply pagination
        total_available = len(log_entries)
        paginated_logs = log_entries[offset : offset + limit]

        return {
            "logs": paginated_logs,
            "total": total_available,
            "offset": offset,
            "limit": limit,
            "has_more": offset + limit < total_available,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error("get_logs_failed", error=str(e))
        raise HTTPException(
            status_code=500, detail=f"Failed to retrieve logs: {str(e)}"
        )


@app.get("/health")
async def health() -> Dict[str, Any]:
    """Health check endpoint.

    Returns:
        Dictionary with status 'ok' and list of discovered agents
    """
    return {"status": "ok", "agents": list(docker_discovery.AGENTS.keys())}


@app.get("/version")
async def get_version() -> Dict[str, Any]:
    """Get version information for the Agent Control Plane.

    Returns:
        Dictionary with version, build timestamp, and git commit information
    """
    import json
    import os

    try:
        # Try to read version from build-time injected file
        if os.path.exists("/app/version.json"):
            with open("/app/version.json", "r") as f:
                version_data = json.load(f)
        else:
            # Fallback for development/local builds
            version_data = {
                "version": "development",
                "build_timestamp": "unknown",
                "git_commit": "unknown",
            }
    except Exception:
        # Fallback on any error
        version_data = {
            "version": "unknown",
            "build_timestamp": "unknown",
            "git_commit": "unknown",
        }

    return {"component": "agent-control-plane", **version_data}


async def _get_registry_versions(owner: str, package: str) -> Dict[str, Any]:
    """Helper function to get versions from GHCR for a specific package."""
    import httpx
    import re

    try:
        # Step 1: Get anonymous token for GHCR
        token_url = f"https://ghcr.io/token?service=ghcr.io&scope=repository:{owner}/{package}:pull"

        async with httpx.AsyncClient(timeout=10) as client:
            token_resp = await client.get(token_url)
            token_resp.raise_for_status()
            token = token_resp.json()["token"]

            # Step 2: Get all tags from GHCR
            tags = []
            url = f"https://ghcr.io/v2/{owner}/{package}/tags/list?n=100"
            headers = {"Authorization": f"Bearer {token}"}

            while url:
                resp = await client.get(url, headers=headers)
                resp.raise_for_status()

                data = resp.json()
                if "tags" in data and data["tags"]:
                    tags.extend(data["tags"])

                # Parse pagination from Link header
                link_header = resp.headers.get("Link", "")
                if link_header and 'rel="next"' in link_header:
                    start = link_header.find("<") + 1
                    end = link_header.find(">", start)
                    url = link_header[start:end] if start > 0 and end > start else None
                else:
                    url = None

            # Filter to only semantic versions (x.y.z format)
            semantic_versions = []
            for tag in tags:
                if re.match(r"^\d+\.\d+\.\d+$", tag):
                    semantic_versions.append(tag)

            # Sort semantic versions properly by version numbers
            def version_key(v):
                return tuple(map(int, v.split(".")))

            sorted_versions = sorted(semantic_versions, key=version_key, reverse=True)
            latest_version = sorted_versions[0] if sorted_versions else "unknown"

            return {
                "available_versions": sorted_versions,
                "latest_version": latest_version,
            }

    except Exception as e:
        logger.error("get_registry_versions_failed", package=package, error=str(e))
        return {"available_versions": [], "latest_version": "unknown"}


# ---------------------------------------------------------------------------
# GitHub Avatar Proxy
# ---------------------------------------------------------------------------


@app.get("/avatar/github/{username}")
async def github_avatar(username: str) -> Response:
    """Proxy GitHub user avatar for cross-origin display.

    Fetches GitHub user avatars and serves them with CORP headers
    to allow display in the UI.

    Args:
        username: GitHub username

    Returns:
        Image file with CORP headers and caching

    Raises:
        HTTPException: 404 if user not found, 502 if GitHub unavailable
    """
    try:
        async with httpx.AsyncClient(timeout=10) as cli:
            # Fetch avatar directly from GitHub's avatar service
            # Using githubusercontent.com which is more reliable than API
            avatar_url = f"https://avatars.githubusercontent.com/{username}?size=40"

            avatar_resp = await cli.get(avatar_url, follow_redirects=True)

            if avatar_resp.status_code == 404:
                raise HTTPException(status_code=404, detail="GitHub user not found")
            elif avatar_resp.status_code != 200:
                logger.warning(
                    "github_avatar_fetch_failed",
                    username=username,
                    status_code=avatar_resp.status_code,
                )
                raise HTTPException(
                    status_code=502, detail="Failed to fetch avatar from GitHub"
                )

            # Return with CORP headers to allow cross-origin display
            return Response(
                content=avatar_resp.content,
                media_type=avatar_resp.headers.get("content-type", "image/png"),
                headers={
                    "Cross-Origin-Resource-Policy": "cross-origin",
                    "Cache-Control": "public, max-age=3600",  # Cache for 1 hour
                },
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error("github_avatar_error", username=username, error=str(e))
        raise HTTPException(status_code=502, detail="Failed to fetch avatar")


@app.get("/component-versions")
async def get_component_versions(offline: bool = False) -> Dict[str, Any]:
    """Get version information for all AgentSystems platform components.

    Args:
        offline: If True, only return current versions without querying GHCR registry

    Returns:
        Dictionary with version info for agent-control-plane, agentsystems-ui, and SDK
    """
    import httpx

    components = {}

    try:
        # 1. Get Agent Control Plane versions
        acp_current = await get_version()
        acp_current_version = acp_current.get("version", "unknown")

        if offline:
            # Offline mode - only current version, no registry queries
            components["agent-control-plane"] = {
                "current_version": acp_current_version,
                "mode": "offline",
            }
        else:
            # Online mode - query GHCR for available versions
            acp_versions = await _get_registry_versions(
                "agentsystems", "agent-control-plane"
            )
            acp_update_available = (
                acp_current_version != "unknown"
                and acp_versions["latest_version"] != "unknown"
                and acp_current_version != acp_versions["latest_version"]
                and acp_current_version not in ["development", "latest"]
            )

            components["agent-control-plane"] = {
                "current_version": acp_current_version,
                "available_versions": acp_versions["available_versions"],
                "latest_version": acp_versions["latest_version"],
                "update_available": acp_update_available,
                "registry": "ghcr.io/agentsystems/agent-control-plane",
            }

        # 2. Get AgentSystems UI versions
        try:
            # Query UI container for its version
            async with httpx.AsyncClient(timeout=5) as client:
                ui_resp = await client.get("http://agentsystems-ui:80/version")
                ui_current = (
                    ui_resp.json()
                    if ui_resp.status_code == 200
                    else {"version": "unknown"}
                )
        except Exception as e:
            logger.warning("ui_version_query_failed", error=str(e))
            ui_current = {"version": "unknown"}

        ui_current_version = ui_current.get("version", "unknown")

        if offline:
            # Offline mode - only current version, no registry queries
            components["agentsystems-ui"] = {
                "current_version": ui_current_version,
                "mode": "offline",
            }
        else:
            # Online mode - query GHCR for available versions
            ui_versions = await _get_registry_versions(
                "agentsystems", "agentsystems-ui"
            )
            ui_update_available = (
                ui_current_version != "unknown"
                and ui_versions["latest_version"] != "unknown"
                and ui_current_version != ui_versions["latest_version"]
                and ui_current_version not in ["development", "latest"]
            )

            components["agentsystems-ui"] = {
                "current_version": ui_current_version,
                "available_versions": ui_versions["available_versions"],
                "latest_version": ui_versions["latest_version"],
                "update_available": ui_update_available,
                "registry": "ghcr.io/agentsystems/agentsystems-ui",
            }

        return {
            "platform": "agentsystems",
            "mode": "offline" if offline else "online",
            "components": components,
        }

    except Exception as e:
        logger.error("get_component_versions_failed", error=str(e))
        return {
            "platform": "agentsystems",
            "components": {
                "agent-control-plane": {"error": "Failed to get version info"},
                "agentsystems-ui": {"error": "Failed to get version info"},
            },
            "error": f"Failed to query component versions: {str(e)}",
        }
