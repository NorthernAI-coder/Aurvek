import json
from pathlib import Path
import re
import subprocess
import sys
import textwrap


EXPECTED_ROUTE_MODULES = {
    ("POST", "/api/conversations/{conversation_id}/external-platform"): "integrations.platform_routes",
    ("GET", "/api/platform-mode/{platform}/{conversation_id}"): "integrations.platform_routes",
    ("POST", "/api/platform-mode/{platform}/{conversation_id}"): "integrations.platform_routes",
    ("GET", "/admin/whatsapp"): "integrations.whatsapp.admin_routes",
    ("POST", "/admin/whatsapp"): "integrations.whatsapp.admin_routes",
    ("POST", "/whatsapp"): "integrations.whatsapp.routes",
    ("GET", "/admin/telegram"): "integrations.telegram.admin_routes",
    ("POST", "/admin/telegram"): "integrations.telegram.admin_routes",
    ("POST", "/telegram"): "integrations.telegram.routes",
    ("GET", "/sdk/elevenlabs-client.js"): "integrations.elevenlabs.sdk_routes",
    ("GET", "/sdk/elevenlabs-client.js.map"): "integrations.elevenlabs.sdk_routes",
    ("GET", "/sdk/lib.umd.js"): "integrations.elevenlabs.sdk_routes",
    ("GET", "/sdk/lib.umd.js.map"): "integrations.elevenlabs.sdk_routes",
    ("GET", "/admin/elevenlabs-agents"): "integrations.elevenlabs.admin_routes",
    ("POST", "/admin/elevenlabs-agents"): "integrations.elevenlabs.admin_routes",
    ("POST", "/admin/elevenlabs-agents/{agent_id}/set-default"): "integrations.elevenlabs.admin_routes",
    ("POST", "/admin/elevenlabs-agents/{agent_id}/delete"): "integrations.elevenlabs.admin_routes",
    ("POST", "/admin/elevenlabs-agents/mapping"): "integrations.elevenlabs.admin_routes",
    ("GET", "/admin/elevenlabs-voices"): "integrations.elevenlabs.admin_routes",
    ("GET", "/admin/elevenlabs-tts"): "integrations.elevenlabs.admin_routes",
    ("POST", "/admin/elevenlabs-tts"): "integrations.elevenlabs.admin_routes",
    ("GET", "/api/elevenlabs/voices"): "integrations.elevenlabs.admin_routes",
    ("POST", "/api/elevenlabs/sync"): "integrations.elevenlabs.admin_routes",
    ("GET", "/api/conversations/{conversation_id}/elevenlabs/config"): "integrations.elevenlabs.routes",
    ("POST", "/api/conversations/{conversation_id}/elevenlabs/session"): "integrations.elevenlabs.routes",
    ("POST", "/api/conversations/{conversation_id}/elevenlabs/complete"): "integrations.elevenlabs.routes",
    ("POST", "/api/conversations/{conversation_id}/elevenlabs/stop"): "integrations.elevenlabs.routes",
}


def test_integration_routes_are_registered_from_integration_package():
    code = textwrap.dedent(
        """
        import json
        import app

        expected = EXPECTED_ROUTE_MODULES_PLACEHOLDER
        found = {}
        duplicates = []
        for method, path in expected:
            matches = [
                route.endpoint.__module__
                for route in app.app.routes
                if getattr(route, "path", None) == path
                and method in getattr(route, "methods", set())
            ]
            if not matches:
                raise SystemExit(f"Route not found: {method} {path}")
            if len(matches) > 1:
                duplicates.append(f"{method} {path}: {matches}")
            found[f"{method} {path}"] = matches[0]

        if duplicates:
            raise SystemExit("Duplicate integration routes: " + "; ".join(duplicates))

        removed_aliases = [
            "/api/whatsapp-mode/{conversation_id}",
            "/api/whatsapp-mode/{conversation_id}/",
        ]
        present_removed_aliases = [
            route.path
            for route in app.app.routes
            if getattr(route, "path", None) in removed_aliases
        ]
        if present_removed_aliases:
            raise SystemExit(f"Removed WhatsApp aliases are still registered: {present_removed_aliases}")

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


def test_integration_modules_do_not_import_app_or_deleted_root_modules():
    repo_root = Path(__file__).resolve().parents[1]
    deleted_root_modules = [
        "external_chat_management.py",
        "elevenlabs_sdk_proxy.py",
        "elevenlabs_service.py",
        "telegram.py",
        "whatsapp.py",
    ]
    for relative_path in deleted_root_modules:
        assert not (repo_root / relative_path).exists(), relative_path

    checked_paths = [repo_root / "gransabio_service.py", repo_root / "prompt_access.py"]
    checked_paths.extend((repo_root / "integrations").rglob("*.py"))

    stale_imports = re.compile(
        r"^\s*(?:"
        r"from\s+(?:app|whatsapp|telegram|external_chat_management|elevenlabs_service|elevenlabs_sdk_proxy)\s+import\b"
        r"|import\s+(?:app|whatsapp|telegram|external_chat_management|elevenlabs_service|elevenlabs_sdk_proxy)\b"
        r")",
        re.MULTILINE,
    )

    for path in checked_paths:
        text = path.read_text(encoding="utf-8")
        assert not stale_imports.search(text), path.relative_to(repo_root)


def test_telegram_chunking_preserves_content_under_limit():
    from integrations.delivery import chunk_telegram_response

    text = ("First sentence. " * 250) + ("Second sentence! " * 250)
    chunks = chunk_telegram_response(text, max_len=3800)

    assert "".join(chunks) == text
    assert all(len(chunk) <= 3800 for chunk in chunks)


def test_runtime_schema_covers_external_webhook_tables():
    text = (Path(__file__).resolve().parents[1] / "integrations/runtime.py").read_text(
        encoding="utf-8"
    )

    for table in (
        "WHATSAPP_PROCESSED_MESSAGES",
        "WHATSAPP_LOG",
        "TELEGRAM_PROCESSED_UPDATES",
        "TELEGRAM_LOG",
    ):
        assert table in text
    assert "contact" in text
