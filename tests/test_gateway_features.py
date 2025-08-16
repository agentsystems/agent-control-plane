"""Additional unit tests for new idle reaper & on-demand launch features of gateway."""

from __future__ import annotations

import sys
from pathlib import Path
import importlib.util as _util
import types
import datetime

import pytest
from fastapi.testclient import TestClient

# Dynamically load the gateway module from source path (avoids install step)
_repo_root = Path(__file__).resolve().parents[1]
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

# Register package structure BEFORE loading the module
pkg = types.ModuleType("cmd")
pkg.__path__ = [str(_repo_root / "cmd")]
subpkg = types.ModuleType("cmd.gateway")
subpkg.__path__ = [str(_repo_root / "cmd" / "gateway")]
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
    module_path = _repo_root / "cmd" / "gateway" / f"{module_name}.py"
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
_gateway_path = _repo_root / "cmd" / "gateway" / "main.py"
_spec = _util.spec_from_file_location("cmd.gateway.main", _gateway_path)
assert _spec and _spec.loader, "Failed to locate gateway source file"
gw = _util.module_from_spec(_spec)  # type: ignore
sys.modules["cmd.gateway.main"] = gw

# Execute the module now that all the module hierarchy is set up
_spec.loader.exec_module(gw)  # type: ignore[attr-defined]

# Also register as subpkg.main for backward compatibility
subpkg.main = gw
pkg.gateway = subpkg


class _StubContainer:
    """Simple stub mimicking Docker SDK Container object."""

    def __init__(self, name: str, port: str = "8000") -> None:
        self.labels = {
            "agent.enabled": "true",
            "com.docker.compose.service": name,
            "agent.port": port,
        }
        self.name = f"{name}_container"
        self.short_id = f"{name[:6]}_id"
        self._started = False
        self.attrs = {
            "NetworkSettings": {
                "Networks": {
                    "agents-int": {"IPAddress": f"172.20.0.{ord(name[0]) % 10 + 2}"}
                }
            }
        }

    # Gateway only calls .start() on ensure_agent_running
    def start(self):  # noqa: D401 – stub method
        self._started = True


class _StubContainers:
    """Return different data based on `all` param to simulate running/idle sets."""

    def __init__(self, running: set[str], all_agents: set[str]):
        self._running = running
        self._all = all_agents

    def list(self, *_, **kwargs):  # noqa: D401 – duck type signature
        is_all = kwargs.get("all", False)
        agents = self._all if is_all else self._running
        return [_StubContainer(name) for name in agents]


class _StubClient:
    def __init__(self, running: set[str], all_agents: set[str]):
        self.containers = _StubContainers(running, all_agents)


@pytest.fixture()
def restore_globals():
    """Reset mutable global state on gw after each test."""
    original_agents = gw.docker_discovery.AGENTS.copy()
    original_client = gw.docker_discovery.client
    original_last_seen = gw.lifecycle.LAST_SEEN.copy()
    yield
    gw.docker_discovery.AGENTS = original_agents
    gw.docker_discovery.client = original_client
    gw.lifecycle.LAST_SEEN = original_last_seen


def _mount_testclient():
    """Return TestClient with heavy startup handlers removed for unit test speed."""
    gw.app.router.on_startup.clear()
    gw.app.router.on_shutdown.clear()
    return TestClient(gw.app)


def test_agents_filtered_endpoint(monkeypatch, restore_globals):
    """/agents POST should respect state filter values."""
    # Simulate one running agent (foo) and second stopped agent (bar)
    running = {"foo"}
    all_agents = {"foo", "bar"}

    stub = _StubClient(running, all_agents)
    monkeypatch.setattr(gw.docker_discovery, "client", stub)

    # Pre-populate cached running agents
    gw.docker_discovery.AGENTS = {"foo": "http://foo:8000/invoke"}

    with _mount_testclient() as client:
        # running only
        resp = client.post("/agents", json={"state": "running"})
        assert resp.status_code == 200
        assert resp.json() == {"agents": ["foo"]}

        # stopped only
        resp = client.post("/agents", json={"state": "stopped"})
        assert resp.status_code == 200
        assert resp.json() == {"agents": ["bar"]}

        # all
        resp = client.post("/agents", json={"state": "all"})
        assert resp.status_code == 200
        assert set(resp.json()["agents"]) == {"foo", "bar"}


def test_ensure_agent_running_checks_container(monkeypatch, restore_globals):
    """ensure_agent_running should check if container is running."""
    target = "baz"

    # Test when container is running
    running_container = _StubContainer(target)

    class _Containers:
        def list(self, *_, **kwargs):
            filters = kwargs.get("filters", {})
            if "status" in filters and filters["status"] == "running":
                # Return container when checking for running status
                return [running_container]
            return []

    class _Client:
        def __init__(self):
            self.containers = _Containers()

    monkeypatch.setattr(gw.docker_discovery, "client", _Client())

    # Should return True when container is running
    is_running = gw.docker_discovery.ensure_agent_running(target)
    assert is_running, "Agent should be considered running"

    # Test when container is not running
    class _EmptyContainers:
        def list(self, *_, **kwargs):
            return []  # No running containers

    class _ClientNoRunning:
        def __init__(self):
            self.containers = _EmptyContainers()

    monkeypatch.setattr(gw.docker_discovery, "client", _ClientNoRunning())

    # Should return False when container is not running
    is_running = gw.docker_discovery.ensure_agent_running(target)
    assert not is_running, "Agent should not be considered running"


def test_idle_reaper_stops_idle_containers(monkeypatch, restore_globals):
    """_idle_reaper should stop containers idle past timeout when invoked once."""
    target = "qux"
    stub_container = _StubContainer(target)
    stopped = {"flag": False}

    def _stop():  # noqa: D401
        stopped["flag"] = True

    stub_container.stop = _stop  # type: ignore

    class _Containers:
        def list(self, *_, **kwargs):  # noqa: D401
            return [stub_container]

    class _Client:
        def __init__(self):
            self.containers = _Containers()

    monkeypatch.setattr(gw.docker_discovery, "client", _Client())

    # Mark last_seen far in the past
    gw.lifecycle.LAST_SEEN[target] = datetime.datetime.now(
        datetime.timezone.utc
    ) - datetime.timedelta(minutes=30)
    # Configure timeout low to ensure it triggers
    gw.egress.IDLE_TIMEOUTS[target] = 5  # minutes

    # Patch asyncio.sleep to coroutine that yields using original sleep to allow task scheduling
    _orig_sleep = __import__("asyncio").sleep

    async def _fast_sleep(_):  # noqa: D401
        # Always yield control immediately without real delay
        await _orig_sleep(0)

    monkeypatch.setattr(gw.lifecycle.asyncio, "sleep", _fast_sleep)

    # Run _idle_reaper coroutine for a single iteration via asyncio.run
    import asyncio

    async def _run_once():
        """Run _idle_reaper briefly allowing at least one iteration."""
        task = asyncio.create_task(gw.lifecycle.idle_reaper())
        # Yield control a few times so the reaper has a chance to execute and call stop()
        for _ in range(10):
            await asyncio.sleep(0)
            if stopped["flag"]:
                break
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(_run_once())

    assert stopped["flag"], "Idle container should have been stopped by reaper"
