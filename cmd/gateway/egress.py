"""Egress allowlist management for agent outbound requests."""

import os
import re
from typing import Dict, List
import yaml
import structlog

from . import docker_discovery

logger = structlog.get_logger()

# Configuration
CONFIG_PATH = os.getenv(
    "AGENTSYSTEMS_CONFIG_PATH", "/etc/agentsystems/agentsystems-config.yml"
)

# Egress allowlist patterns per agent
EGRESS_ALLOWLIST: Dict[str, List[str]] = {}

# Idle timeout configuration
IDLE_TIMEOUTS: Dict[str, int] = {}
GLOBAL_IDLE_TIMEOUT = int(os.getenv("ACP_IDLE_TIMEOUT_MIN", "15"))

# Agent metadata mapping: name -> {registry_connection, repo, registry_url}
AGENT_METADATA: Dict[str, Dict[str, str]] = {}


def load_egress_allowlist(path: str = CONFIG_PATH) -> None:
    """Load egress allowlist, idle timeouts, and agent metadata from YAML configuration."""
    global EGRESS_ALLOWLIST, IDLE_TIMEOUTS, AGENT_METADATA

    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
    except FileNotFoundError:
        logger.warning("config_not_found", path=path)
        return
    except Exception as e:
        logger.warning("config_read_failed", error=str(e))
        return

    # Extract registry connections for URL resolution
    registry_connections = raw.get("registry_connections", {})

    # Extract egress allowlists
    allowlist: Dict[str, List[str]] = {}
    for agent in raw.get("agents", []):
        name = agent.get("name")
        patterns = agent.get("egress_allowlist", []) or []
        if name:
            allowlist[name] = patterns
    EGRESS_ALLOWLIST = allowlist

    # Extract per-agent idle timeout configurations
    idle_map: Dict[str, int] = {}
    for agent in raw.get("agents", []):
        name = agent.get("name")
        if name and agent.get("idle_timeout") is not None:
            try:
                idle_map[name] = int(agent["idle_timeout"])
            except ValueError:
                logger.warning("config_idle_timeout_invalid", agent=name)
    IDLE_TIMEOUTS = idle_map

    # Extract agent metadata (registry_connection + repo -> full identifier)
    metadata: Dict[str, Dict[str, str]] = {}
    for agent in raw.get("agents", []):
        name = agent.get("name")
        repo = agent.get("repo")
        registry_conn = agent.get("registry_connection")

        if name and repo and registry_conn:
            # Look up registry URL from registry_connections
            registry_config = registry_connections.get(registry_conn, {})
            registry_url = registry_config.get("url", "")

            metadata[name] = {
                "registry_connection": registry_conn,
                "repo": repo,
                "registry_url": registry_url,
            }
    AGENT_METADATA = metadata

    # Capture configured agent names for visibility in /agents endpoints
    configured_names = {
        agent.get("name") for agent in raw.get("agents", []) if agent.get("name")
    }
    docker_discovery.set_configured_agent_names(configured_names)

    logger.info(
        "config_loaded",
        egress_entries=len(EGRESS_ALLOWLIST),
        idle_entries=len(IDLE_TIMEOUTS),
        agent_metadata_entries=len(AGENT_METADATA),
        configured_agents=len(configured_names),
    )


def is_allowed(agent: str, url: str) -> bool:
    """Check if an agent is allowed to access the given URL."""
    patterns = EGRESS_ALLOWLIST.get(agent, [])
    if not patterns:
        return False

    for pattern in patterns:
        # Convert glob pattern to regex
        # Replace * with .* and escape other regex special chars
        regex_pattern = pattern.replace(".", r"\.")
        regex_pattern = regex_pattern.replace("*", ".*")
        regex_pattern = f"^{regex_pattern}"

        if re.match(regex_pattern, url):
            return True

    return False


def get_allowlist() -> Dict[str, List[str]]:
    """Get the current egress allowlist."""
    return EGRESS_ALLOWLIST.copy()


def get_idle_timeouts() -> Dict[str, int]:
    """Get the current idle timeout configuration."""
    return IDLE_TIMEOUTS.copy()


def get_idle_timeout(agent: str) -> int:
    """Get idle timeout for a specific agent."""
    return IDLE_TIMEOUTS.get(agent, GLOBAL_IDLE_TIMEOUT)


def get_agent_identifier(agent_name: str) -> str:
    """Get the full registry/repo identifier for an agent.

    Args:
        agent_name: The agent container name (from config)

    Returns:
        Full identifier in format "registry_url/repo" (e.g., "docker.io/ironbirdlabs/demo-agent")
        Falls back to agent_name if metadata not found
    """
    metadata = AGENT_METADATA.get(agent_name)
    if not metadata:
        # Fallback to agent name if metadata not available
        logger.warning("agent_metadata_not_found", agent=agent_name)
        return agent_name

    registry_url = metadata.get("registry_url", "")
    repo = metadata.get("repo", "")

    if registry_url and repo:
        return f"{registry_url}/{repo}"
    else:
        # Fallback if incomplete metadata
        logger.warning("agent_metadata_incomplete", agent=agent_name)
        return agent_name
