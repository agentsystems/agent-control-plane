"""Common exception patterns for the Agent Gateway."""

from fastapi import HTTPException


def agent_not_found(agent: str) -> HTTPException:
    """Return 404 error for unknown agent."""
    return HTTPException(status_code=404, detail=f"unknown agent: {agent}")


def thread_not_found(thread_id: str) -> HTTPException:
    """Return 404 error for unknown thread ID."""
    return HTTPException(status_code=404, detail=f"unknown thread_id: {thread_id}")


def bad_request(detail: str) -> HTTPException:
    """Return 400 error for bad requests."""
    return HTTPException(status_code=400, detail=detail)


def forbidden(detail: str) -> HTTPException:
    """Return 403 error for forbidden requests."""
    return HTTPException(status_code=403, detail=detail)


def bad_gateway(detail: str) -> HTTPException:
    """Return 502 error for upstream failures."""
    return HTTPException(status_code=502, detail=detail)
