"""Unit tests for Agent Gateway main FastAPI app."""

import sys
from pathlib import Path
import importlib.util as _util
import types

from fastapi.testclient import TestClient

# Ensure repository root (parent of 'cmd') is on sys.path when tests run from workspace root
repo_root = Path(__file__).resolve().parents[1]
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

# Register package structure BEFORE loading the module
pkg = types.ModuleType("cmd")
pkg.__path__ = [str(repo_root / "cmd")]
subpkg = types.ModuleType("cmd.gateway")
subpkg.__path__ = [str(repo_root / "cmd" / "gateway")]
sys.modules["cmd"] = pkg
sys.modules["cmd.gateway"] = subpkg

# Pre-register and load all gateway submodules so imports work
# Load modules in dependency order
modules_to_load = [
    "models",  # No dependencies
    "exceptions",  # No dependencies
    "egress",  # No dependencies
    "database",  # No dependencies
    "docker_discovery",  # Depends on models
    "proxy",  # Depends on egress
    "lifecycle",  # Depends on docker_discovery and egress
]

for module_name in modules_to_load:
    module_path = repo_root / "cmd" / "gateway" / f"{module_name}.py"
    if module_path.exists():
        spec = _util.spec_from_file_location(f"cmd.gateway.{module_name}", module_path)
        if spec and spec.loader:
            module = _util.module_from_spec(spec)
            sys.modules[f"cmd.gateway.{module_name}"] = module
            try:
                spec.loader.exec_module(module)
            except ImportError as e:
                # Skip modules with unmet dependencies for now
                print(f"Warning: Could not load {module_name}: {e}")
                pass

# Now we can safely load the module since the package structure exists
_gateway_path = repo_root / "cmd" / "gateway" / "main.py"
_spec = _util.spec_from_file_location("cmd.gateway.main", _gateway_path)
assert _spec and _spec.loader  # ensure module spec found
gw = _util.module_from_spec(_spec)  # type: ignore
sys.modules["cmd.gateway.main"] = gw

# Execute the module now that all the module hierarchy is set up
_spec.loader.exec_module(gw)

# Also register as subpkg.main for backward compatibility
subpkg.main = gw


class _StubContainer:
    labels = {
        "agent.enabled": "true",
        "com.docker.compose.service": "foo",
        "agent.port": "7000",
    }
    name = "foo_container"


class _StubContainers:
    def list(self, filters=None):  # noqa: D401 – simple stub
        return [_StubContainer()]


class _StubClient:
    containers = _StubContainers()


def test_refresh_agents_updates_cache(monkeypatch):
    """refresh_agents should populate AGENTS dict from Docker labels."""
    monkeypatch.setattr(gw.docker_discovery, "client", _StubClient())
    gw.docker_discovery.refresh_agents()
    assert gw.docker_discovery.AGENTS == {"foo": "http://foo:7000/invoke"}


def test_health_endpoint(monkeypatch):
    """/health should return status ok and list of agents without running startup events."""
    # Skip heavy startup handlers (DB, Docker event watcher)
    gw.app.router.on_startup.clear()

    # Pre-populate agent cache
    gw.docker_discovery.AGENTS = {"foo": "http://foo:7000/invoke"}

    with TestClient(gw.app) as client:
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "foo" in data["agents"]
