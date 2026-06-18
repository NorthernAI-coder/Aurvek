import json
import os
from pathlib import Path
import re
import subprocess
import sys
import textwrap


EXPECTED_ROUTE_MODULES = {
    ("GET", "/chat"): "chat.routes.pages",
    ("POST", "/chat"): "chat.routes.pages",
    ("GET", "/api/conversations"): "chat.routes.conversations",
    ("POST", "/api/conversations/new"): "chat.routes.conversations",
    ("GET", "/api/conversations/{conversation_id}/details"): "chat.routes.pages",
    ("PATCH", "/api/conversations/{conversation_id}/model"): "chat.routes.pages",
    ("PATCH", "/api/conversations/{conversation_id}/extension"): "chat.routes.conversations",
    ("POST", "/api/conversations/{conversation_id}/rename"): "chat.routes.conversations",
    ("GET", "/api/conversations/{conversation_id}/last_message_id"): "chat.routes.conversations",
    ("POST", "/api/conversations/{conversation_id}/stop"): "chat.routes.conversations",
    ("DELETE", "/api/conversations/{conversation_id}"): "chat.routes.conversations",
    ("POST", "/api/conversations/{conversation_id}/incognito/close"): "chat.routes.conversations",
    ("GET", "/api/conversations/{conversation_id}/status"): "chat.routes.conversations",
    ("GET", "/api/conversations/{conversation_id}/web-search-status"): "chat.routes.conversations",
    ("POST", "/api/user/web-search-toggle"): "chat.routes.conversations",
    ("POST", "/api/user/web-search-mode"): "chat.routes.conversations",
    ("GET", "/api/user/web-search-settings"): "chat.routes.conversations",
    ("POST", "/api/user/web-search-settings"): "chat.routes.conversations",
    ("GET", "/api/conversations/{conversation_id}/messages"): "chat.routes.messages",
    ("POST", "/api/conversations/{conversation_id}/messages"): "chat.routes.messages",
    ("POST", "/api/conversations/{conversation_id}/warmup"): "chat.routes.warmup",
    ("GET", "/api/chat-folders"): "chat.routes.folders",
    ("POST", "/api/chat-folders"): "chat.routes.folders",
    ("PUT", "/api/chat-folders/{folder_id}"): "chat.routes.folders",
    ("DELETE", "/api/chat-folders/{folder_id}"): "chat.routes.folders",
    ("POST", "/api/conversations/{conversation_id}/move-to-folder"): "chat.routes.folders",
    ("POST", "/api/conversations/{conversation_id}/bookmark"): "chat.routes.bookmarks",
    ("GET", "/api/bookmarks"): "chat.routes.bookmarks",
    ("GET", "/api/messages/search"): "chat.routes.search",
    ("POST", "/api/conversations/{conversation_id}/rollback"): "chat.routes.branching",
    ("POST", "/api/conversations/{conversation_id}/branch"): "chat.routes.branching",
    ("POST", "/api/conversations/{conversation_id}/attachments/chunk"): "chat.routes.attachments",
    ("POST", "/api/conversations/{conversation_id}/attachments/complete"): "chat.routes.attachments",
    ("GET", "/api/conversations/{conversation_id}/attachments/status"): "chat.routes.attachments",
    ("POST", "/api/conversations/{conversation_id}/attachments/discard"): "chat.routes.attachments",
    ("GET", "/api/attachments/{public_id}/content"): "chat.routes.attachments",
    ("GET", "/api/attachments/{public_id}/download"): "chat.routes.attachments",
    ("DELETE", "/api/attachments/{public_id}"): "chat.routes.attachments",
    ("GET", "/media-gallery"): "chat.routes.media",
    ("GET", "/get-pdfs"): "chat.routes.media",
    ("GET", "/get-mp3s"): "chat.routes.media",
    ("GET", "/download-pdf"): "chat.routes.media",
    ("GET", "/download-mp3"): "chat.routes.media",
    ("GET", "/list-files"): "chat.routes.media",
    ("GET", "/auth-file"): "chat.routes.media",
    ("POST", "/delete-pdf"): "chat.routes.media",
    ("POST", "/delete-mp3"): "chat.routes.media",
    ("POST", "/delete-pdfs"): "chat.routes.media",
    ("POST", "/delete-mp3s"): "chat.routes.media",
    ("POST", "/api/get-tts-audio"): "chat.routes.voice_io",
    ("POST", "/api/transcribe-web"): "chat.routes.voice_io",
    ("GET", "/download-pdf/{conversation_id}"): "chat.routes.voice_io",
    ("GET", "/download-mp3/{conversation_id}"): "chat.routes.voice_io",
    ("GET", "/serve-mp3/{conversation_id}"): "chat.routes.voice_io",
    ("GET", "/get-audio/{path:path}"): "chat.routes.voice_io",
    ("GET", "/admin/chat"): "chat.routes.admin",
    ("GET", "/api/admin/conversations"): "chat.routes.admin",
    ("GET", "/api/admin/users/autocomplete"): "chat.routes.admin",
    ("POST", "/api/conversations/{conversation_id}/lock"): "chat.routes.admin",
    ("POST", "/admin/api/conversations/bulk_lock"): "chat.routes.admin",
    ("DELETE", "/admin/api/conversations/{conversation_id}"): "chat.routes.admin",
    ("POST", "/admin/api/conversations/bulk_delete"): "chat.routes.admin",
}


def test_chat_routes_are_registered_from_chat_package():
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

        websocket_matches = [
            route.endpoint.__module__
            for route in app.app.routes
            if getattr(route, "path", None) == "/ws"
        ]
        if websocket_matches != ["chat.routes.voice_io"]:
            raise SystemExit(f"Unexpected websocket registrations: {websocket_matches}")

        if duplicates:
            raise SystemExit("Duplicate chat routes: " + "; ".join(duplicates))

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


def test_app_and_ai_calls_no_longer_own_chat_routes_or_deleted_root_modules():
    repo_root = Path(__file__).resolve().parents[1]

    for relative_path in ("chat_warmup.py", "conversation_privacy.py", "message_search.py"):
        assert not (repo_root / relative_path).exists(), relative_path

    app_text = (repo_root / "app.py").read_text(encoding="utf-8")
    removed_ai_runtime_file = "ai_calls" + ".py"
    assert not (repo_root / removed_ai_runtime_file).exists()

    removed_decorators = [
        '@app.get("/chat"',
        '@app.post("/chat"',
        '@app.get("/api/conversations"',
        '@app.post("/api/conversations/new"',
        '@app.get("/api/conversations/{conversation_id}/messages"',
        '@app.get("/api/messages/search"',
        '@app.get("/api/chat-folders"',
        '@app.get("/media-gallery"',
        '@app.post("/api/transcribe-web"',
        '@app.websocket("/ws"',
        '@app.get("/admin/chat"',
        '@router.post("/api/conversations/{conversation_id}/messages"',
        '@router.post("/api/conversations/{conversation_id}/warmup"',
        '@router.post("/api/conversations/{conversation_id}/attachments/chunk"',
    ]
    for decorator in removed_decorators:
        assert decorator not in app_text

    stale_imports = re.compile(
        r"^\s*(?:from\s+(?:conversation_privacy|chat_warmup|message_search)\s+import\b"
        r"|import\s+(?:conversation_privacy|chat_warmup|message_search)\b)",
        re.MULTILINE,
    )
    checked_paths = [
        repo_root / "app.py",
        repo_root / "atagia_sync.py",
        repo_root / "gransabio_service.py",
    ]
    checked_paths.extend((repo_root / "tests").glob("test_*.py"))
    for path in checked_paths:
        assert not stale_imports.search(path.read_text(encoding="utf-8")), path.relative_to(repo_root)


def test_ai_runtime_boundary_has_no_ai_calls_imports():
    repo_root = Path(__file__).resolve().parents[1]
    stale_ai_runtime_imports = re.compile(
        r"^\s*(?:from\s+ai_calls\s+import\b|import\s+ai_calls\b)",
        re.MULTILINE,
    )

    excluded_dirs = {".git", "__pycache__", ".venv"}
    for root, dirs, files in os.walk(repo_root):
        dirs[:] = [
            name
            for name in dirs
            if name not in excluded_dirs and not name.startswith(".venv")
        ]
        for filename in files:
            if not filename.endswith(".py"):
                continue
            path = Path(root) / filename
            text = path.read_text(encoding="utf-8")
            assert not stale_ai_runtime_imports.search(text), path.relative_to(repo_root)
