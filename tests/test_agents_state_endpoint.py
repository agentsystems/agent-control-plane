"""Tests for GET /agents enhanced state response.

The gateway should report every configured agent with a `state` field that
indicates whether the agent's container is running, stopped, or not yet
created.
"""

from fastapi.testclient import TestClient


class _StubContainer:  # minimal fake docker container object
    def __init__(self, service_name: str):
        self.name = service_name  # fallback if label missing
        # labels the gateway looks at
        self.labels = {
            "com.docker.compose.service": service_name,
            "agent.enabled": "true",
            "agent.port": "8000",
        }
        # container.attrs not used by the /agents endpoint logic


class _StubDockerClient:
    """Docker client exposing only what the gateway needs (containers.list)."""

    class _Containers:
        def __init__(self, items):
            self._items = items

        def list(self, *_, **__):
            # *args, **kwargs swallowed; filters not applied for simplicity
            return self._items

    def __init__(self, containers):
        self.containers = self._Containers(containers)


def test_list_agents_states(monkeypatch):
    """GET /agents returns correct state per agent name."""
    # Import gateway by path to avoid stdlib `cmd` module name clash.
    import importlib.util
    import pathlib
    import sys
    import types

    repo_root = pathlib.Path(__file__).resolve().parents[1]  # agent-control-plane/

    # Add repo root to path
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    # Register package structure BEFORE loading the module
    pkg = types.ModuleType("cmd")
    pkg.__path__ = [str(repo_root / "cmd")]
    subpkg = types.ModuleType("cmd.gateway")
    subpkg.__path__ = [str(repo_root / "cmd" / "gateway")]
    sys.modules["cmd"] = pkg
    sys.modules["cmd.gateway"] = subpkg

    # Pre-register and load all gateway submodules
    modules_to_load = [
        "models",
        "exceptions",
        "egress",
        "database",
        "docker_discovery",
        "proxy",
        "lifecycle",
    ]

    for module_name in modules_to_load:
        module_path = repo_root / "cmd" / "gateway" / f"{module_name}.py"
        if module_path.exists():
            spec = importlib.util.spec_from_file_location(
                f"cmd.gateway.{module_name}", module_path
            )
            if spec and spec.loader:
                module = importlib.util.module_from_spec(spec)
                sys.modules[f"cmd.gateway.{module_name}"] = module
                try:
                    spec.loader.exec_module(module)
                except ImportError:
                    pass

    # Now load the main module
    gw_path = repo_root / "cmd" / "gateway" / "main.py"
    spec = importlib.util.spec_from_file_location("cmd.gateway.main", gw_path)
    gw = importlib.util.module_from_spec(spec)
    sys.modules["cmd.gateway.main"] = gw
    assert spec.loader is not None
    spec.loader.exec_module(gw)

    # ---------------------- Arrange ----------------------
    running_name = "running_agent"
    stopped_name = "stopped_agent"
    not_created_name = "uncreated_agent"

    # Patch configured agent names
    monkeypatch.setattr(
        gw.docker_discovery,
        "CONFIGURED_AGENT_NAMES",
        {running_name, stopped_name, not_created_name},
        raising=False,
    )

    # Patch in-memory running set (only running_agent is running)
    monkeypatch.setattr(
        gw.docker_discovery,
        "AGENTS",
        {running_name: "http://running_agent:8000/invoke"},
        raising=False,
    )

    # Stub Docker client so `containers.list(all=True)` returns running + stopped
    stub_client = _StubDockerClient(
        [
            _StubContainer(running_name),
            _StubContainer(stopped_name),  # exists but not in AGENTS -> "stopped"
        ]
    )
    monkeypatch.setattr(gw.docker_discovery, "client", stub_client, raising=False)

    # Skip refresh_agents to keep patched state intact
    monkeypatch.setattr(
        gw.docker_discovery, "refresh_agents", lambda: None, raising=False
    )

    # ----------------------- Act -----------------------
    client = TestClient(gw.app)
    resp = client.get("/agents")

    # ---------------------- Assert ----------------------
    assert resp.status_code == 200
    body = resp.json()

    # Convert list -> dict for easy assertions
    mapping = {item["name"]: item["state"] for item in body["agents"]}

    assert mapping == {
        running_name: "running",
        stopped_name: "stopped",
        not_created_name: "not-created",
    }
