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


def load_egress_allowlist(path: str = CONFIG_PATH) -> None:
    """Load egress allowlist and idle timeouts from YAML configuration."""
    global EGRESS_ALLOWLIST, IDLE_TIMEOUTS

    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
    except FileNotFoundError:
        logger.warning("config_not_found", path=path)
        return
    except Exception as e:
        logger.warning("config_read_failed", error=str(e))
        return

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

    # Capture configured agent names for visibility in /agents endpoints
    configured_names = {
        agent.get("name") for agent in raw.get("agents", []) if agent.get("name")
    }
    docker_discovery.set_configured_agent_names(configured_names)

    logger.info(
        "config_loaded",
        egress_entries=len(EGRESS_ALLOWLIST),
        idle_entries=len(IDLE_TIMEOUTS),
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
