import asyncio
import sqlite3
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import pytest
import pytest_asyncio
from fastapi import HTTPException
from starlette.requests import Request

import app as app_module
import auth
import models
from migration_session_version import migrate


USER_ID = 42
LIVE_SESSION_VERSION = 7


def _request_with_session(token: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/refresh-session",
            "headers": [(b"cookie", f"session={token}".encode("utf-8"))],
        }
    )


def _websocket_with_session(token: str):
    return SimpleNamespace(cookies={"session": token})


def _token_user_info(*, session_version=LIVE_SESSION_VERSION) -> dict:
    user_info = {
        "id": USER_ID,
        "username": "forged_admin",
        "is_admin": True,
        "is_user": False,
        "is_customer": False,
        "is_enabled": True,
        "can_send_files": True,
        "can_generate_images": True,
        "current_prompt_id": 999,
        "uses_magic_link": True,
        "voice_id": 999,
        "voice_code": "forged-voice",
        "all_prompts_access": True,
        "public_prompts_access": True,
        "authentication_mode": "magic_link_only",
        "can_change_password": True,
        "role_id": 1,
        "used_magic_link": False,
    }
    if session_version is not None:
        user_info["session_version"] = session_version
    return user_info


def _session_token(*, session_version=LIVE_SESSION_VERSION) -> str:
    return auth.create_access_token(
        {
            "sub": "forged_admin",
            "user_info": _token_user_info(session_version=session_version),
        },
        expires_delta=timedelta(minutes=5),
    )


async def _create_auth_database(db_path) -> None:
    async with aiosqlite.connect(db_path) as conn:
        await conn.executescript(
            """
            CREATE TABLE USER_ROLES (
                id INTEGER PRIMARY KEY,
                role_name TEXT UNIQUE NOT NULL
            );
            CREATE TABLE USERS (
                id INTEGER PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                password BLOB,
                role_id INTEGER,
                is_enabled INTEGER NOT NULL DEFAULT 1,
                google_id TEXT,
                auth_provider TEXT DEFAULT 'local',
                session_version INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE USER_DETAILS (
                user_id INTEGER PRIMARY KEY,
                current_prompt_id INTEGER,
                allow_file_upload INTEGER DEFAULT 0,
                allow_image_generation INTEGER DEFAULT 0,
                all_prompts_access INTEGER DEFAULT 0,
                public_prompts_access INTEGER DEFAULT 0,
                authentication_mode TEXT DEFAULT 'password_only',
                can_change_password INTEGER DEFAULT 0,
                voice_id INTEGER
            );
            CREATE TABLE VOICES (
                id INTEGER PRIMARY KEY,
                voice_code TEXT
            );
            CREATE TABLE magic_links (
                user_id INTEGER,
                expires_at TEXT
            );

            INSERT INTO USER_ROLES (id, role_name) VALUES
                (1, 'admin'),
                (2, 'user'),
                (3, 'customer');
            INSERT INTO USERS (
                id, username, password, role_id, is_enabled,
                google_id, auth_provider, session_version
            ) VALUES (
                42, 'live_customer', NULL, 3, 1,
                NULL, 'local', 7
            );
            INSERT INTO USER_DETAILS (
                user_id, current_prompt_id, allow_file_upload,
                allow_image_generation, all_prompts_access,
                public_prompts_access, authentication_mode,
                can_change_password, voice_id
            ) VALUES (
                42, 12, 0, 0, 0, 0, 'password_only', 0, NULL
            );
            """
        )
        await conn.commit()


@pytest_asyncio.fixture
async def auth_database(tmp_path, monkeypatch):
    db_path = tmp_path / "auth.db"
    await _create_auth_database(db_path)

    @asynccontextmanager
    async def get_test_connection(readonly=False):
        conn = await aiosqlite.connect(db_path)
        conn.row_factory = aiosqlite.Row
        try:
            yield conn
        finally:
            await conn.close()

    monkeypatch.setattr(auth, "get_db_connection", get_test_connection)
    monkeypatch.setattr(models, "get_db_connection", get_test_connection)
    monkeypatch.setattr(auth, "is_user_revoked", AsyncMock(return_value=False))
    auth.decode_jwt_cached.cache_clear()
    yield db_path, get_test_connection
    auth.decode_jwt_cached.cache_clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["http", "websocket"])
async def test_legacy_session_without_version_is_rejected(auth_database, transport):
    token = _session_token(session_version=None)

    if transport == "http":
        user = await auth.get_current_user(_request_with_session(token))
    else:
        user = await auth.get_current_user_from_websocket(
            _websocket_with_session(token)
        )

    assert user is None


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["http", "websocket"])
async def test_session_version_mismatch_is_rejected(auth_database, transport):
    token = _session_token(session_version=LIVE_SESSION_VERSION - 1)

    if transport == "http":
        user = await auth.get_current_user(_request_with_session(token))
    else:
        user = await auth.get_current_user_from_websocket(
            _websocket_with_session(token)
        )

    assert user is None


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["http", "websocket"])
async def test_matching_version_returns_live_database_identity_and_permissions(
    auth_database,
    transport,
):
    token = _session_token()

    if transport == "http":
        user = await auth.get_current_user(_request_with_session(token))
    else:
        user = await auth.get_current_user_from_websocket(
            _websocket_with_session(token)
        )

    assert user is not None
    assert user.id == USER_ID
    assert user.username == "live_customer"
    assert user.role_id == 3
    assert user.session_version == LIVE_SESSION_VERSION
    assert user.current_prompt_id == 12
    assert user.can_send_files is False
    assert user.can_generate_images is False
    assert user.all_prompts_access is False
    assert user.public_prompts_access is False
    assert user.authentication_mode == "password_only"
    assert user.can_change_password is False
    assert await user.is_admin is False
    assert await user.is_user is False
    assert await user.is_customer is True


@pytest.mark.asyncio
async def test_refresh_cannot_revive_a_stale_session(auth_database):
    request = _request_with_session(
        _session_token(session_version=LIVE_SESSION_VERSION - 1)
    )
    current_user = await auth.get_current_user(request)

    assert current_user is None
    with pytest.raises(HTTPException) as exc_info:
        await app_module.refresh_session(request, current_user=current_user)

    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_session_version_bump_is_atomic_under_concurrency(auth_database):
    db_path, get_test_connection = auth_database

    async def bump_and_commit() -> int:
        async with get_test_connection() as conn:
            version = await auth.bump_session_version(conn, USER_ID)
            await conn.commit()
            return version

    versions = await asyncio.gather(bump_and_commit(), bump_and_commit())

    assert sorted(versions) == [LIVE_SESSION_VERSION + 1, LIVE_SESSION_VERSION + 2]
    async with aiosqlite.connect(db_path) as conn:
        cursor = await conn.execute(
            "SELECT session_version FROM USERS WHERE id = ?",
            (USER_ID,),
        )
        row = await cursor.fetchone()
    assert row[0] == LIVE_SESSION_VERSION + 2


@pytest.mark.asyncio
async def test_session_version_bump_obeys_caller_transaction(auth_database):
    db_path, get_test_connection = auth_database

    async with get_test_connection() as conn:
        assert await auth.bump_session_version(conn, USER_ID) == 8
        await conn.rollback()

    async with aiosqlite.connect(db_path) as conn:
        cursor = await conn.execute(
            "SELECT session_version FROM USERS WHERE id = ?",
            (USER_ID,),
        )
        row = await cursor.fetchone()
    assert row[0] == LIVE_SESSION_VERSION


def test_session_version_migration_is_idempotent_and_normalizes_values(tmp_path):
    db_path = tmp_path / "migration.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE USERS (id INTEGER PRIMARY KEY, username TEXT NOT NULL)"
        )
        conn.execute("INSERT INTO USERS (id, username) VALUES (1, 'first')")

    first = migrate(str(db_path))

    assert first == {"session_version_added": True}
    with sqlite3.connect(db_path) as conn:
        columns = {
            row[1]: row
            for row in conn.execute("PRAGMA table_info(USERS)").fetchall()
        }
        assert columns["session_version"][3] == 1
        assert str(columns["session_version"][4]) == "1"
        assert conn.execute(
            "SELECT session_version FROM USERS WHERE id = 1"
        ).fetchone()[0] == 1
        conn.execute("UPDATE USERS SET session_version = 0 WHERE id = 1")

    second = migrate(str(db_path))

    assert second == {"session_version_added": False}
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT session_version FROM USERS WHERE id = 1"
        ).fetchone()[0] == 1
