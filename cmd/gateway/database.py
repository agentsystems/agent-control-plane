"""Database operations for the Agent Gateway."""

import os
import json
from typing import Optional, Dict, Any
import asyncpg
import structlog

from .models import (
    INV_STATE_QUEUED,
)

logger = structlog.get_logger()

# Database connection settings
PG_HOST = os.getenv("PG_HOST", "localhost")
PG_DB = os.getenv("PG_DB", "agent_cp")
PG_USER = os.getenv("PG_USER", "agent")
PG_PASSWORD = os.getenv("PG_PASSWORD", "agent")

# Connection pool
DB_POOL: Optional[asyncpg.Pool] = None

# In-memory job store for when DB is unavailable
JOBS: Dict[str, Dict[str, Any]] = {}


async def init_pool(retries: int = 10) -> bool:
    """Initialize the database connection pool.

    Args:
        retries: Number of connection attempts before giving up

    Returns:
        True if pool was successfully created, False otherwise
    """
    global DB_POOL

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
            logger.info("db_pool_created", host=PG_HOST, database=PG_DB)
            return True
        except Exception as e:
            logger.warning(
                "db_pool_creation_failed", error=str(e), retries_left=retries
            )
            retries -= 1

    return False


async def close_pool() -> None:
    """Close the database connection pool.

    Gracefully closes the connection pool if it exists.
    Logs a warning if the close operation fails.
    """
    global DB_POOL
    if DB_POOL is not None:
        try:
            await DB_POOL.close()
            logger.info("db_pool_closed")
        except Exception as e:
            logger.warning("db_pool_close_failed", error=str(e))


async def check_connection() -> bool:
    """Check if database connection is available.

    Returns:
        True if database is reachable, False otherwise
    """
    if DB_POOL is None:
        return False

    try:
        async with DB_POOL.acquire() as conn:
            await conn.fetchval("SELECT 1")
        return True
    except Exception:
        return False


async def update_job_record(thread_id: str, **fields) -> None:
    """Update job fields in database or memory.

    Args:
        thread_id: UUID of the job to update
        **fields: Arbitrary fields to update (e.g., state, result, error)
    """
    if DB_POOL:
        # Build the SET clause dynamically
        await DB_POOL.execute(
            f"UPDATE invocations SET {', '.join(f'{k} = ${i+2}' for i, k in enumerate(fields))} WHERE thread_id = $1",
            thread_id,
            *fields.values(),
        )
    else:
        # Fallback to in-memory storage
        if thread_id not in JOBS:
            JOBS[thread_id] = {"thread_id": thread_id}
        JOBS[thread_id].update(fields)


async def insert_job_row(
    thread_id: str, agent: str, user_token: str, payload: Dict[str, Any] = None
) -> None:
    """Insert a new job record.

    Args:
        thread_id: UUID for the new job
        agent: Name of the agent handling this job
        user_token: Bearer token of the user making the request
        payload: Original request payload sent to the agent
    """
    if DB_POOL:
        await DB_POOL.execute(
            """
            INSERT INTO invocations (thread_id, agent, user_token, state, created_at, payload)
            VALUES ($1, $2, $3, $4, NOW(), $5)
            """,
            thread_id,
            agent,
            user_token,
            INV_STATE_QUEUED,
            json.dumps(payload) if payload else None,
        )
    else:
        # Fallback to in-memory storage
        JOBS[thread_id] = {
            "thread_id": thread_id,
            "agent": agent,
            "user_token": user_token,
            "state": INV_STATE_QUEUED,
            "payload": payload,
        }


async def get_job(thread_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve job record from database or memory.

    Args:
        thread_id: UUID of the job to retrieve

    Returns:
        Dictionary containing job data or None if not found
    """
    if DB_POOL:
        row = await DB_POOL.fetchrow(
            "SELECT * FROM invocations WHERE thread_id = $1", thread_id
        )
        return dict(row) if row else None
    else:
        # Fallback to in-memory storage
        return JOBS.get(thread_id)


def get_memory_jobs() -> Dict[str, Dict[str, Any]]:
    """Get all jobs from memory store (for debugging/status).

    Returns:
        Copy of all jobs stored in memory when database is unavailable
    """
    return JOBS.copy()


async def audit_invoke_request(
    user_token: str, thread_id: str, agent: str, payload: Dict[str, Any]
) -> None:
    """Log an incoming agent invocation request to audit_log.

    Args:
        user_token: Bearer token of the user making the request
        thread_id: UUID of the invocation
        agent: Name of the agent being invoked
        payload: Request payload to be logged
    """
    if DB_POOL:
        try:
            await DB_POOL.execute(
                """INSERT INTO audit_log (user_token, thread_id, actor, action, resource, status_code, payload)
                     VALUES ($1,$2,'gateway','invoke_request',$3,0,$4)""",
                user_token,
                thread_id,
                f"{agent}/invoke",
                json.dumps(payload) if payload else None,
            )
        except Exception as e:
            logger.warning(
                "audit_log_insert_failed", error=str(e), action="invoke_request"
            )


async def audit_invoke_response(
    user_token: str,
    thread_id: str,
    agent: str,
    status_code: int,
    payload: Optional[Dict[str, Any]] = None,
    error_msg: Optional[str] = None,
) -> None:
    """Log an agent invocation response to audit_log.

    Args:
        user_token: Bearer token of the user making the request
        thread_id: UUID of the invocation
        agent: Name of the agent that responded
        status_code: HTTP status code of the response
        payload: Response payload to be logged (optional)
        error_msg: Error message if the invocation failed (optional)
    """
    if DB_POOL:
        try:
            await DB_POOL.execute(
                """INSERT INTO audit_log (user_token, thread_id, actor, action, resource, status_code, payload, error_msg)
                     VALUES ($1,$2,$3,'invoke_response',$4,$5,$6,$7)""",
                user_token,
                thread_id,
                agent,
                f"{agent}/invoke",
                status_code,
                json.dumps(payload) if payload else None,
                error_msg,
            )
        except Exception as e:
            logger.warning(
                "audit_log_insert_failed", error=str(e), action="invoke_response"
            )
