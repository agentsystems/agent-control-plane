"""Container lifecycle management for idle agents."""

import asyncio
import datetime
from typing import Dict
import structlog

from . import docker_discovery, egress

logger = structlog.get_logger()

# Track last seen time for each agent
LAST_SEEN: Dict[str, datetime.datetime] = {}


def record_agent_activity(agent: str) -> None:
    """Record that an agent was just invoked."""
    LAST_SEEN[agent] = datetime.datetime.now(datetime.timezone.utc)


async def idle_reaper() -> None:
    """Background task that stops idle containers based on configured timeouts."""
    if docker_discovery.client is None:
        logger.info("idle_reaper_disabled_no_docker")
        return

    while True:
        await asyncio.sleep(60)  # Check every minute
        try:
            await _check_and_stop_idle_containers()
        except Exception as e:
            logger.error("idle_reaper_error", error=str(e))


async def _check_and_stop_idle_containers() -> None:
    """Check all running containers and stop those that have been idle too long."""
    now = datetime.datetime.now(datetime.timezone.utc)

    containers = docker_discovery.client.containers.list(
        filters={"label": "agent.enabled=true"}
    )

    for container in containers:
        name = container.labels.get("com.docker.compose.service", container.name)
        last_activity = LAST_SEEN.get(name)

        if last_activity is None:
            # Never invoked, don't stop
            continue

        timeout_minutes = egress.get_idle_timeout(name)
        idle_seconds = (now - last_activity).total_seconds()
        idle_minutes = idle_seconds / 60

        if idle_minutes >= timeout_minutes:
            try:
                container.stop()
                logger.info(
                    "agent_stopped_idle",
                    agent=name,
                    idle_minutes=round(idle_minutes, 1),
                    timeout_minutes=timeout_minutes,
                )
                # Refresh agents to update the registry
                docker_discovery.refresh_agents()
            except Exception as e:
                logger.warning("agent_idle_stop_failed", agent=name, error=str(e))


def get_last_seen() -> Dict[str, datetime.datetime]:
    """Get the last seen times for all agents (for debugging/monitoring)."""
    return LAST_SEEN.copy()


def clear_last_seen(agent: str) -> None:
    """Clear the last seen time for an agent (e.g., when manually stopped)."""
    LAST_SEEN.pop(agent, None)
