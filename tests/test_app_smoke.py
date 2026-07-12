import asyncio
import json


def test_application_imports_and_registers_release_critical_routes():
    import app as app_module

    registered = {
        (method, route.path)
        for route in app_module.app.routes
        for method in getattr(route, "methods", set())
    }
    expected = {
        ("GET", "/health"),
        ("GET", "/api/my-usage"),
        ("GET", "/api/admin/storage-quotas/config"),
        ("PUT", "/api/admin/storage-quotas/config"),
        ("GET", "/api/admin/storage-quotas/users"),
        ("POST", "/api/conversations/{conversation_id}/branch"),
        ("GET", "/api/attachments/{public_id}/content"),
        ("GET", "/api/mobile/v1/config"),
    }
    assert expected <= registered

    response = asyncio.run(app_module.public_health_check())
    assert response.status_code == 200
    assert json.loads(response.body)["status"] == "ok"
