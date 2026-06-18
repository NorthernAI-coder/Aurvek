from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest


EXPECTED_ROUTE_MODULES = {
    ("GET", "/admin/memory"): "memory.routes",
    ("GET", "/admin/mem0"): "memory.routes",
    ("POST", "/admin/memory/provider"): "memory.routes",
    ("POST", "/admin/memory/no-memory-context"): "memory.routes",
    ("POST", "/admin/memory/mem0"): "memory.routes",
    ("POST", "/admin/memory/mem0/defaults"): "memory.routes",
    ("POST", "/admin/memory/mem0/test-connection"): "memory.routes",
    ("POST", "/admin/memory/mem0/sync"): "memory.routes",
    ("GET", "/admin/memory/mem0/sync-status"): "memory.routes",
}


def test_memory_routes_are_registered_from_memory_package():
    code = textwrap.dedent(
        """
        import json
        import app

        expected = EXPECTED_ROUTE_MODULES_PLACEHOLDER
        found = {}
        for method, path in expected:
            matches = [
                route.endpoint.__module__
                for route in app.app.routes
                if getattr(route, "path", None) == path
                and method in getattr(route, "methods", set())
            ]
            if not matches:
                raise SystemExit(f"Route not found: {method} {path}")
            found[f"{method} {path}"] = matches[0]
        print("ROUTE_MODULES=" + json.dumps(found, sort_keys=True))
        """
    ).replace("EXPECTED_ROUTE_MODULES_PLACEHOLDER", repr(EXPECTED_ROUTE_MODULES))
    result = subprocess.run(
        [sys.executable, "-c", code],
        text=True,
        capture_output=True,
        check=True,
    )
    route_modules_line = next(
        line for line in result.stdout.splitlines() if line.startswith("ROUTE_MODULES=")
    )
    route_modules = json.loads(route_modules_line.removeprefix("ROUTE_MODULES="))

    assert route_modules == {
        f"{method} {path}": module
        for (method, path), module in EXPECTED_ROUTE_MODULES.items()
    }


def test_memory_selector_uses_exclusive_mem0_page():
    selector = Path("templates/admin_memory.html").read_text(encoding="utf-8")
    mem0_page = Path("templates/admin_mem0.html").read_text(encoding="utf-8")

    assert 'id="noneContextMaxTokens"' in selector
    assert 'href="/admin/mem0"' in selector
    assert 'href="/admin/atagia"' in selector
    assert 'id="mem0BaseUrl"' not in selector
    assert 'id="mem0BaseUrl"' in mem0_page


@pytest.mark.asyncio
async def test_mem0_test_connection_route_returns_json_on_provider_failure(
    mock_db,
    monkeypatch,
):
    import memory.routes as memory_routes

    class RequestStub:
        async def json(self):
            return {"base_url": "http://127.0.0.1:8888"}

    class AdminStub:
        @property
        async def is_admin(self):
            return True

    class FailingMem0Provider:
        def __init__(self, _config):
            pass

        async def test_connection(self):
            return False, "Mem0 OSS server is not reachable."

    monkeypatch.setattr(memory_routes, "Mem0Provider", FailingMem0Provider)

    response = await memory_routes.admin_memory_mem0_test(RequestStub(), AdminStub())
    body = json.loads(response.body)

    assert response.status_code == 200
    assert body == {
        "success": False,
        "message": "Mem0 OSS server is not reachable.",
    }
