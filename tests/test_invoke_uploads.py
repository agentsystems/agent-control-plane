"""Integration tests for /invoke endpoint (JSON and multipart upload paths)."""

import sys
import json as _json
from pathlib import Path
import importlib.util as _util
import types

import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Dynamically load the gateway module under the expected package path so we
# can monkeypatch its globals easily without altering PYTHONPATH.
# ---------------------------------------------------------------------------
_repo_root = Path(__file__).resolve().parents[1]
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

# Register package structure BEFORE loading the module
_pkg = types.ModuleType("cmd")
_pkg.__path__ = [str(_repo_root / "cmd")]
_subpkg = types.ModuleType("cmd.gateway")
_subpkg.__path__ = [str(_repo_root / "cmd" / "gateway")]
sys.modules.setdefault("cmd", _pkg)
sys.modules.setdefault("cmd.gateway", _subpkg)

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
assert _spec and _spec.loader
gw = _util.module_from_spec(_spec)  # type: ignore
sys.modules.setdefault("cmd.gateway.main", gw)

# Execute the module now that all the module hierarchy is set up
_spec.loader.exec_module(gw)  # type: ignore

# Also register as _subpkg.main for backward compatibility
_subpkg.main = gw
_pkg.gateway = _subpkg


# ---------------------------------------------------------------------------
# Dummy httpx.AsyncClient stub so invoke() never performs real HTTP requests.
# ---------------------------------------------------------------------------
class _DummyAsyncClient:
    def __init__(self, *_, **__):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, *args, **kwargs):  # noqa: D401 – simple stub
        class _Resp:
            status_code = 200

            def json(self):
                return {"ok": True}

            text = "{}"

        return _Resp()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _patch_gateway(monkeypatch, tmp_path):
    """Patch gateway globals for controlled unit tests."""
    # Skip heavy startup hooks
    gw.app.router.on_startup.clear()

    # Monkeypatch Docker client to avoid actual Docker calls
    monkeypatch.setattr(gw, "client", None)

    # Provide a dummy agent mapping so /invoke/foo is accepted
    gw.AGENTS = {"foo": "http://foo:8000/invoke"}
    monkeypatch.setattr(gw, "ensure_agent_running", lambda name: True)

    # Patch httpx client used for forwarding calls
    monkeypatch.setattr(
        gw, "httpx", types.SimpleNamespace(AsyncClient=_DummyAsyncClient)
    )

    # Mount /artifacts to a temp dir
    artifacts_root = tmp_path / "artifacts"
    artifacts_root.mkdir()

    # Patch os functions to redirect /artifacts paths
    orig_makedirs = gw.os.makedirs
    orig_path_join = gw.os.path.join

    def _patched_makedirs(path, exist_ok=True, mode=None):
        if str(path).startswith("/artifacts"):
            path = str(artifacts_root) + str(path)[len("/artifacts") :]
        if mode is not None:
            return orig_makedirs(path, exist_ok=exist_ok, mode=mode)
        return orig_makedirs(path, exist_ok=exist_ok)

    def _patched_join(*args):
        result = orig_path_join(*args)
        if result.startswith("/artifacts"):
            return str(artifacts_root) + result[len("/artifacts") :]
        return result

    monkeypatch.setattr(gw.os, "makedirs", _patched_makedirs)
    monkeypatch.setattr(gw.os.path, "join", _patched_join)

    # Patch open function to redirect /artifacts file writes
    orig_open = open

    def _patched_open(file, mode="r", *args, **kwargs):
        if str(file).startswith("/artifacts"):
            file = str(artifacts_root) + str(file)[len("/artifacts") :]
        return orig_open(file, mode, *args, **kwargs)

    monkeypatch.setattr("builtins.open", _patched_open)

    yield


def _client():
    client = TestClient(gw.app)
    client.headers.update({"Authorization": "Bearer testtoken"})
    return client


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_invoke_json_only():
    with _client() as client:
        resp = client.post("/invoke/foo", json={"sync": True})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "thread_id" in body


def test_invoke_single_file(tmp_path):
    with _client() as client:
        file_content = b"hello world"
        files = {"file": ("greeting.txt", file_content)}
        data = {"json": _json.dumps({"sync": True})}
        resp = client.post("/invoke/foo", files=files, data=data)
        assert resp.status_code == 200, resp.text
        tid = resp.json()["thread_id"]
        # Check file staged correctly (thread-centric structure)
        staged = Path("/artifacts") / tid / "in" / "greeting.txt"
        assert staged.exists() and staged.read_bytes() == file_content


def test_invoke_multiple_files(tmp_path):
    with _client() as client:
        files = [
            ("file", ("a.txt", b"a")),
            ("file", ("b.txt", b"b")),
        ]
        data = {"json": _json.dumps({"sync": True})}
        resp = client.post("/invoke/foo", files=files, data=data)
        assert resp.status_code == 200, resp.text
        tid = resp.json()["thread_id"]
        for fname, content in [("a.txt", b"a"), ("b.txt", b"b")]:
            # Thread-centric structure: /artifacts/{thread_id}/in/{filename}
            staged = Path("/artifacts") / tid / "in" / fname
            assert staged.read_bytes() == content


def test_invoke_file_size_limit(monkeypatch):
    # Set max upload size to 1 MB to force error
    monkeypatch.setenv("ACP_MAX_UPLOAD_MB", "1")
    big = b"x" * (2 * 1024 * 1024)  # 2 MB
    with _client() as client:
        files = {"file": ("big.bin", big)}
        data = {"json": _json.dumps({"sync": True})}
        resp = client.post("/invoke/foo", files=files, data=data)
        assert resp.status_code == 413
