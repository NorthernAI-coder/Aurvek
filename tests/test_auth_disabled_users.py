from contextlib import asynccontextmanager
from datetime import timedelta
from unittest.mock import AsyncMock

import aiosqlite
import pytest
from starlette.requests import Request

import auth
import auth_flows
from models import User


def _request_with_session(token: str) -> Request:
    return Request({
        "type": "http",
        "method": "GET",
        "path": "/home",
        "headers": [(b"cookie", f"session={token}".encode("utf-8"))],
    })


def _user_info(user_id: int) -> dict:
    return {
        "id": user_id,
        "username": "disabled_user",
        "is_admin": False,
        "is_user": False,
        "is_customer": True,
        "is_enabled": True,
        "can_send_files": False,
        "can_generate_images": False,
        "current_prompt_id": None,
        "uses_magic_link": False,
        "voice_id": None,
        "voice_code": None,
        "all_prompts_access": False,
        "public_prompts_access": True,
        "authentication_mode": "password_only",
        "can_change_password": False,
        "role_id": 3,
        "used_magic_link": False,
    }


@pytest.mark.asyncio
async def test_current_user_rejects_disabled_account(tmp_path, monkeypatch):
    db_path = tmp_path / "auth_disabled.db"
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute("CREATE TABLE USERS (id INTEGER PRIMARY KEY, is_enabled INTEGER)")
        await conn.execute("INSERT INTO USERS (id, is_enabled) VALUES (42, 0)")
        await conn.commit()

    @asynccontextmanager
    async def get_test_connection(readonly=False):
        conn = await aiosqlite.connect(db_path)
        conn.row_factory = aiosqlite.Row
        try:
            yield conn
        finally:
            await conn.close()

    monkeypatch.setattr(auth, "get_db_connection", get_test_connection)
    monkeypatch.setattr(auth, "is_user_revoked", AsyncMock(return_value=False))

    token = auth.create_access_token(
        {"sub": "disabled_user", "user_info": _user_info(42)},
        expires_delta=timedelta(minutes=5),
    )

    assert await auth.get_current_user(_request_with_session(token)) is None


@pytest.mark.asyncio
async def test_password_login_rejects_disabled_account(monkeypatch):
    body = b"username=disabled_user&password=secret&captcha_token=ok"

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/login",
            "query_string": b"",
            "headers": [(b"content-type", b"application/x-www-form-urlencoded")],
            "session": {},
        },
        receive,
    )

    disabled_user = User(
        id=42,
        username="disabled_user",
        password=b"unused",
        role_id=3,
        is_enabled=False,
        can_send_files=False,
        can_generate_images=False,
        current_prompt_id=None,
        authentication_mode="password_only",
        is_admin=False,
        is_user=False,
    )

    class TemplateResult:
        def __init__(self, name, context):
            self.name = name
            self.context = context

    monkeypatch.setattr(auth_flows, "check_rate_limits", lambda *args, **kwargs: None)
    monkeypatch.setattr(auth_flows, "check_failure_limit", lambda *args, **kwargs: None)
    monkeypatch.setattr(auth_flows, "record_failure", lambda *args, **kwargs: None)
    monkeypatch.setattr(auth_flows.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(auth_flows, "verify_captcha", AsyncMock(return_value=(True, None)))
    monkeypatch.setattr(auth_flows, "get_user_by_username", AsyncMock(return_value=disabled_user))
    monkeypatch.setattr(
        auth_flows.templates,
        "TemplateResponse",
        lambda name, context: TemplateResult(name, context),
    )

    response = await auth_flows.handle_login_request(request)

    assert response.name == "login.html"
    assert response.context["error"] == "This account has been disabled. Contact support for assistance."
