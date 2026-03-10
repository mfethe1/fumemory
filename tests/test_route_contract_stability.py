from __future__ import annotations

import importlib
import re
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from memu.auth import verify_api_key


@pytest.fixture
def memu_app(monkeypatch):
    """Import memu.api with a minimal Temporal stub so route contracts can be inspected."""

    temporalio = types.ModuleType("temporalio")

    class _WorkflowModule:
        @staticmethod
        def defn(cls):
            return cls

        @staticmethod
        def run(fn):
            return fn

        @staticmethod
        async def execute_activity(*args, **kwargs):  # pragma: no cover - import shim only
            return None

    class _Client:
        @staticmethod
        async def connect(host):  # pragma: no cover - import shim only
            return _Client()

        async def start_workflow(self, *args, **kwargs):  # pragma: no cover - import shim only
            class _Handle:
                id = "wf-test"

            return _Handle()

        async def execute_workflow(self, *args, **kwargs):  # pragma: no cover - import shim only
            return []

    client_mod = types.ModuleType("temporalio.client")
    client_mod.Client = _Client
    temporalio.client = client_mod
    temporalio.workflow = _WorkflowModule()

    monkeypatch.setitem(sys.modules, "temporalio", temporalio)
    monkeypatch.setitem(sys.modules, "temporalio.client", client_mod)

    sys.modules.pop("memu.temporal_client", None)
    sys.modules.pop("memu.temporal_worker.workflows", None)
    sys.modules.pop("memu.web_search_ingest", None)
    sys.modules.pop("memu.api", None)

    module = importlib.import_module("memu.api")
    return module.app


@pytest.mark.parametrize(
    ("headers", "expected_status"),
    [
        ({"X-API-Key": "memu-dev-key"}, 200),
        ({"Authorization": "Bearer memu-dev-key"}, 200),
        ({"X-API-Key": "wrong"}, 401),
    ],
)
def test_auth_header_compatibility(headers, expected_status):
    app = FastAPI()

    @app.get("/protected")
    async def protected(_key: str = Depends(verify_api_key)):
        return {"ok": True, "key": str(_key)}

    client = TestClient(app)
    response = client.get("/protected", headers=headers)
    assert response.status_code == expected_status


def test_canonical_and_legacy_routes_are_both_present(memu_app):
    route_map = {
        (method, route.path): route
        for route in memu_app.routes
        for method in getattr(route, "methods", set())
        if method in {"GET", "POST", "DELETE"}
    }

    expected_paths = {
        ("GET", "/api/v1/memu/health"),
        ("GET", "/health"),
        ("POST", "/api/v1/memu/upsert"),
        ("POST", "/memories"),
        ("POST", "/api/v1/memu/search"),
        ("POST", "/search"),
        ("GET", "/api/v1/memu/search-text"),
        ("POST", "/api/v1/memu/search-text"),
        ("GET", "/search-text"),
        ("POST", "/search-text"),
        ("POST", "/api/v1/memu/chat"),
        ("POST", "/chat"),
        ("POST", "/api/v1/memu/bulk"),
        ("POST", "/memories/bulk"),
        ("POST", "/api/v1/memu/memories/async"),
        ("POST", "/memories/async"),
        ("POST", "/api/v1/memu/search/async"),
        ("POST", "/search/async"),
        ("GET", "/api/v1/memu/{memory_id}"),
        ("DELETE", "/api/v1/memu/{memory_id}"),
    }

    missing = sorted(expected_paths - set(route_map))
    assert not missing, f"Missing route contracts: {missing}"


def test_openapi_marks_legacy_aliases_deprecated_and_keeps_canonical_routes(memu_app):
    schema = memu_app.openapi()

    assert "/api/v1/memu/health" in schema["paths"]
    assert "/api/v1/memu/upsert" in schema["paths"]
    assert "/api/v1/memu/search" in schema["paths"]
    assert "/api/v1/memu/search-text" in schema["paths"]
    assert "/api/v1/memu/chat" in schema["paths"]
    assert "/api/v1/memu/bulk" in schema["paths"]
    assert "/api/v1/memu/memories/async" in schema["paths"]
    assert "/api/v1/memu/search/async" in schema["paths"]

    assert schema["paths"]["/health"]["get"]["deprecated"] is True
    assert schema["paths"]["/memories"]["post"]["deprecated"] is True
    assert schema["paths"]["/search"]["post"]["deprecated"] is True
    assert schema["paths"]["/search-text"]["get"]["deprecated"] is True
    assert schema["paths"]["/search-text"]["post"]["deprecated"] is True
    assert schema["paths"]["/chat"]["post"]["deprecated"] is True
    assert schema["paths"]["/memories/bulk"]["post"]["deprecated"] is True
    assert schema["paths"]["/memories/async"]["post"]["deprecated"] is True
    assert schema["paths"]["/search/async"]["post"]["deprecated"] is True


def test_api_reference_only_mentions_live_routes(memu_app):
    schema = memu_app.openapi()
    documented = Path(__file__).resolve().parents[1] / "docs" / "API.md"
    text = documented.read_text()

    mentioned = set(re.findall(r"\b(?:GET|POST|PUT|DELETE)\s+(/[^\s`]+)", text))
    missing = sorted(path for path in mentioned if path not in schema["paths"])

    assert not missing, f"Documented routes missing from OpenAPI: {missing}"
