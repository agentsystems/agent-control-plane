"""Docker container discovery and management for the Agent Gateway."""

import asyncio
import threading
from typing import Dict, Set, Optional, Any, List
import docker
from docker import APIClient
import structlog

logger = structlog.get_logger()

# Global state for discovered agents
AGENTS: Dict[str, str] = {}  # name -> target URL
AGENT_LOCK = threading.Lock()
# Map container IP -> agent name for proxy enforcement without headers
AGENT_IP_MAP: Dict[str, str] = {}
# Names of agents defined in agentsystems-config.yml (may have no container yet)
CONFIGURED_AGENT_NAMES: Set[str] = set()

# Docker client instances
client: Optional[docker.DockerClient]
api_client: Optional[APIClient]
try:
    client = docker.DockerClient.from_env()
    api_client = APIClient(base_url="unix://var/run/docker.sock", version="auto")
except Exception as e:
    logger.warning("docker_unavailable", error=str(e))
    client = None
    api_client = None


def _get_agent_containers_fast() -> List[Dict[str, Any]]:
    """Get all agent containers using fast low-level API.

    Returns raw container data from Docker API without expensive per-container inspect.
    Much faster than client.containers.list() + c.attrs for each container.
    """
    if not api_client:
        logger.info("skip_fast_discovery_api_unavailable")
        return []

    try:
        # Single fast API call - gets all container info including networks, labels, state
        containers = api_client.containers(
            all=True, filters={"label": ["agent.enabled=true"]}
        )
        logger.debug("fast_containers_retrieved", count=len(containers))
        return containers
    except Exception as e:
        logger.error("fast_containers_failed", error=str(e))
        return []


def refresh_agents() -> None:
    """Scan all containers for 'agent.enabled=true' labels and populate AGENTS."""
    global AGENTS, AGENT_IP_MAP
    if not api_client:
        logger.info("skip_agent_discovery_api_unavailable")
        return

    discovered: Dict[str, str] = {}
    ip_map: Dict[str, str] = {}

    try:
        # Use fast API call instead of expensive client.containers.list() + c.attrs
        containers = _get_agent_containers_fast()

        for c in containers:
            # Only process running containers for AGENTS registry
            state = c.get("State", "")
            if state != "running":
                continue

            # Extract name from compose service label or container name
            labels = c.get("Labels", {}) or {}
            name = labels.get("com.docker.compose.service")
            if not name:
                # Fallback to first name without leading slash
                names = c.get("Names", [])
                name = names[0].lstrip("/") if names else c.get("Id", "")[:12]

            port = labels.get("agent.port", "8000")

            # Try to get container IP from NetworkSettings (already included in response)
            container_ip = None
            network_settings = c.get("NetworkSettings", {})
            if network_settings:
                networks = network_settings.get("Networks", {})
                agents_int = networks.get("agents-int", {})
                container_ip = agents_int.get("IPAddress")

            if container_ip:
                discovered[name] = f"http://{container_ip}:{port}/invoke"
                ip_map[container_ip] = name
                logger.debug(
                    "agent_discovered_fast",
                    name=name,
                    target=discovered[name],
                    container_id=c.get("Id", "")[:12],
                )
            else:
                # Fallback to container name if not on agents-int network
                discovered[name] = f"http://{name}:{port}/invoke"
                logger.debug(
                    "agent_name_fallback_fast",
                    name=name,
                    container_id=c.get("Id", "")[:12],
                )

    except Exception as e:
        logger.error("refresh_agents_fast_failed", error=str(e))
        return

    with AGENT_LOCK:
        AGENTS = discovered
        AGENT_IP_MAP = ip_map

    logger.info(
        "agents_refreshed",
        count=len(discovered),
        agents=list(discovered.keys()),
        ip_map=ip_map,
    )


def ensure_agent_running(agent: str) -> bool:
    """Start the agent container if stopped and return True if running, else False.

    Args:
        agent: Name of the agent to ensure is running

    Returns:
        True if agent is running (or was successfully started), False otherwise
    """
    if not client:
        return agent in AGENTS

    try:
        # First check if already running
        containers = client.containers.list(
            filters={
                "label": ["agent.enabled=true", f"com.docker.compose.service={agent}"],
                "status": "running",
            }
        )
        if containers:
            logger.info(
                "agent_already_running",
                agent=agent,
                container_id=containers[0].short_id,
            )
            return True

        # Check if container exists but is stopped
        all_containers = client.containers.list(
            all=True,
            filters={
                "label": ["agent.enabled=true", f"com.docker.compose.service={agent}"],
            },
        )

        if all_containers:
            container = all_containers[0]
            if container.status != "running":
                logger.info(
                    "starting_agent_container",
                    agent=agent,
                    container_id=container.short_id,
                )
                container.start()
                # Wait a moment for container to start
                import time

                time.sleep(2)
                return True

        # Also check by container name
        containers = client.containers.list(all=True, filters={"name": agent})
        for container in containers:
            if container.labels.get("agent.enabled") == "true":
                if container.status != "running":
                    logger.info("starting_agent_by_name", agent=agent)
                    container.start()
                    import time

                    time.sleep(2)
                return True

    except Exception as e:
        logger.error("ensure_agent_running_failed", agent=agent, error=str(e))

    return False


async def watch_docker() -> None:
    """Periodically refresh the agent registry from Docker labels.

    This coroutine runs in a background task and refreshes the agent
    registry every 5 seconds by scanning Docker containers for those
    with 'agent.enabled=true' labels.
    """
    while True:
        try:
            refresh_agents()
        except Exception as e:
            logger.error("watch_docker_error", error=str(e))
        await asyncio.sleep(5)


def set_configured_agent_names(names: Set[str]) -> None:
    """Update the set of configured agent names from agentsystems-config.yml.

    Args:
        names: Set of agent names defined in the configuration file
    """
    global CONFIGURED_AGENT_NAMES
    CONFIGURED_AGENT_NAMES = names


def get_all_agent_info() -> Dict[str, Dict[str, Any]]:
    """Get information about all agents (both running and configured).

    Returns:
        Dictionary mapping agent names to their information including:
        - name: Agent name
        - state: Either 'running' or 'configured'
        - url: Target URL for invocation (None if not running)
    """
    agent_info = {}

    # Add running agents
    with AGENT_LOCK:
        for name, url in AGENTS.items():
            agent_info[name] = {
                "name": name,
                "state": "running",
                "url": url,
            }

    # Add configured but not running agents
    for name in CONFIGURED_AGENT_NAMES:
        if name not in agent_info:
            agent_info[name] = {
                "name": name,
                "state": "configured",
                "url": None,
            }

    return agent_info
