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
_gateway_path = _repo_root / "cmd" / "gateway" / "main.py"
_spec = _util.spec_from_file_location("agent_gateway", _gateway_path)
assert _spec and _spec.loader
gw = _util.module_from_spec(_spec)  # type: ignore
_spec.loader.exec_module(gw)  # type: ignore

_pkg = types.ModuleType("cmd")
_subpkg = types.ModuleType("cmd.gateway")
_subpkg.main = gw
_pkg.gateway = _subpkg
sys.modules.setdefault("cmd", _pkg)
sys.modules.setdefault("cmd.gateway", _subpkg)
sys.modules.setdefault("cmd.gateway.main", gw)


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
    # Monkeypatch pathlib.Path in gateway so '/artifacts' resolves into temp dir
    orig_path_cls = gw.pathlib.Path

    def _patched(p: str | Path):
        p_str = str(p)
        if p_str.startswith("/artifacts"):
            return orig_path_cls(str(artifacts_root) + p_str[len("/artifacts") :])
        return orig_path_cls(p_str)

    monkeypatch.setattr(gw, "pathlib", types.SimpleNamespace(Path=_patched))

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
        # Check file staged correctly
        staged = Path("/artifacts/foo/input") / tid / "greeting.txt"
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
            staged = Path("/artifacts/foo/input") / tid / fname
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
