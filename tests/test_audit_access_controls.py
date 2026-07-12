import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiosqlite
import jwt
import pytest
import pytest_asyncio
from fastapi import HTTPException
from fastapi.responses import FileResponse

import app as app_module
import prompts
from billing.usage_reservations import InsufficientBalanceError
from chat.routes import conversations as conversation_routes
from chat.routes import voice_io
from common import ALGORITHM, SECRET_KEY, generate_user_hash
from integrations import media
from save_images import generate_img_token


def _user_path(username: str, suffix: str) -> str:
    prefix1, prefix2, user_hash = generate_user_hash(username)
    return f"users/{prefix1}/{prefix2}/{user_hash}/{suffix}"


@pytest.mark.asyncio
async def test_auth_image_token_is_bound_to_the_exact_path():
    user = SimpleNamespace(username="image_owner_a")
    path_a = _user_path(user.username, "profile/a_128.webp")
    path_b = _user_path("image_owner_b", "profile/b_128.webp")
    token = generate_img_token(
        path_a,
        datetime.now(timezone.utc) + timedelta(minutes=5),
        user,
    )

    response = await app_module.auth_image(None, token=token, request_uri=f"/{path_a}")
    assert response.status_code == 200

    with pytest.raises(HTTPException) as denied:
        await app_module.auth_image(None, token=token, request_uri=f"/{path_b}")
    assert denied.value.status_code == 403


@pytest.mark.asyncio
async def test_legacy_image_token_cannot_cross_user_prefixes():
    username = "legacy_image_owner"
    own_path = _user_path(username, "profile/own_128.webp")
    other_path = _user_path("different_image_owner", "profile/other_128.webp")
    token = jwt.encode(
        {
            "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
            "username": username,
        },
        SECRET_KEY,
        algorithm=ALGORITHM,
    )

    response = await app_module.auth_image(None, token=token, request_uri=f"/{own_path}")
    assert response.status_code == 200

    with pytest.raises(HTTPException) as denied:
        await app_module.auth_image(None, token=token, request_uri=f"/{other_path}")
    assert denied.value.status_code == 403


class _WelcomeConnection:
    async def cursor(self):
        return object()


@pytest.mark.asyncio
async def test_welcome_assets_require_the_same_prompt_access(monkeypatch, tmp_path):
    @asynccontextmanager
    async def get_connection(readonly=False):
        yield _WelcomeConnection()

    world_dir = tmp_path / "prompt-world"
    static_dir = world_dir / "welcome" / "static"
    static_dir.mkdir(parents=True)
    asset = static_dir / "style.css"
    asset.write_text("body {}", encoding="utf-8")
    sibling_dir = world_dir / "welcome" / "static-private"
    sibling_dir.mkdir()
    (sibling_dir / "secret.txt").write_text("private", encoding="utf-8")

    access = AsyncMock(side_effect=lambda user, entity_id, cursor: user.id == 1)
    monkeypatch.setattr(app_module, "get_db_connection", get_connection)
    monkeypatch.setattr(app_module, "can_user_access_prompt", access)
    monkeypatch.setattr(
        app_module,
        "get_prompt_info",
        AsyncMock(return_value={"name": "World", "created_by_username": "owner"}),
    )
    monkeypatch.setattr(app_module, "get_prompt_path", lambda entity_id, info: str(world_dir))

    owner_response = await app_module.serve_welcome_static_scoped(
        "p7",
        "style.css",
        SimpleNamespace(),
        SimpleNamespace(id=1),
    )
    assert isinstance(owner_response, FileResponse)
    assert str(owner_response.path) == str(asset)

    with pytest.raises(HTTPException) as stranger:
        await app_module.serve_welcome_static_scoped(
            "p7",
            "style.css",
            SimpleNamespace(),
            SimpleNamespace(id=2),
        )
    with pytest.raises(HTTPException) as traversal:
        await app_module.serve_welcome_static_scoped(
            "p7",
            "../static-private/secret.txt",
            SimpleNamespace(),
            SimpleNamespace(id=1),
        )
    with pytest.raises(HTTPException) as missing:
        await app_module.serve_welcome_static_scoped(
            "p999",
            "style.css",
            SimpleNamespace(),
            SimpleNamespace(id=2),
        )

    assert (stranger.value.status_code, stranger.value.detail) == (
        missing.value.status_code,
        missing.value.detail,
    ) == (404, "Welcome resource not found")
    assert traversal.value.status_code == 403


@pytest.mark.asyncio
async def test_welcome_pack_assets_use_pack_access_helper(monkeypatch):
    @asynccontextmanager
    async def get_connection(readonly=False):
        yield _WelcomeConnection()

    pack_access = AsyncMock(return_value=False)
    monkeypatch.setattr(app_module, "get_db_connection", get_connection)
    monkeypatch.setattr(app_module, "can_user_access_pack", pack_access)
    monkeypatch.setattr(
        app_module,
        "can_user_access_prompt",
        AsyncMock(side_effect=AssertionError("prompt access helper should not be used")),
    )

    with pytest.raises(HTTPException) as denied:
        await app_module.serve_welcome_static_scoped(
            "k8",
            "image.webp",
            SimpleNamespace(),
            SimpleNamespace(id=2),
        )

    assert denied.value.status_code == 404
    pack_access.assert_awaited_once()


@pytest.mark.asyncio
async def test_transcribe_preserves_bad_request_http_exception():
    with pytest.raises(HTTPException) as error:
        await voice_io.transcribe(
            SimpleNamespace(headers={}),
            audio=None,
            user_id=1,
        )
    assert error.value.status_code == 400
    assert error.value.detail == "No audio or media URL provided"


@pytest.mark.asyncio
async def test_transcribe_preserves_insufficient_balance(monkeypatch):
    class AudioUpload:
        async def read(self):
            return b"audio"

    monkeypatch.setattr(voice_io, "get_browser", lambda user_agent: "chrome")
    monkeypatch.setattr(
        voice_io.AudioSegment,
        "from_file",
        lambda *args, **kwargs: SimpleNamespace(duration_seconds=60),
    )
    monkeypatch.setattr(
        media,
        "reserve_fixed_usage",
        AsyncMock(side_effect=InsufficientBalanceError("Insufficient balance")),
    )

    with pytest.raises(HTTPException) as error:
        await voice_io.transcribe(
            SimpleNamespace(headers={"user-agent": "test"}),
            audio=AudioUpload(),
            user_id=1,
        )
    assert error.value.status_code == 402
    assert error.value.detail == "Insufficient balance"


@pytest_asyncio.fixture
async def conversation_db(tmp_path):
    db_path = tmp_path / "conversation_access.db"
    async with aiosqlite.connect(db_path) as conn:
        await conn.executescript(
            """
            CREATE TABLE CONVERSATIONS (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL
            );
            CREATE TABLE MESSAGES (
                id INTEGER PRIMARY KEY,
                conversation_id INTEGER NOT NULL,
                date TEXT NOT NULL
            );
            INSERT INTO CONVERSATIONS (id, user_id) VALUES (10, 1);
            INSERT INTO MESSAGES (id, conversation_id, date)
            VALUES (100, 10, '2026-01-01'), (101, 10, '2026-01-02');
            """
        )
        await conn.commit()

    @asynccontextmanager
    async def get_connection(readonly=False):
        conn = await aiosqlite.connect(db_path)
        conn.row_factory = aiosqlite.Row
        try:
            yield conn
        finally:
            await conn.close()

    return get_connection


@pytest.mark.asyncio
async def test_conversation_metadata_is_owner_scoped_and_uniform(
    monkeypatch,
    conversation_db,
):
    monkeypatch.setattr(conversation_routes, "get_db_connection", conversation_db)
    monkeypatch.setattr(conversation_routes, "is_admin", AsyncMock(return_value=False))
    owner = SimpleNamespace(id=1)
    stranger = SimpleNamespace(id=2)

    assert await conversation_routes.get_last_message_id(10, owner) == {
        "message_id": 101
    }
    status = await conversation_routes.conversation_status(10, owner)
    assert json.loads(status.body) == {"isActive": True}

    errors = []
    for conversation_id, user in ((10, stranger), (999, stranger)):
        with pytest.raises(HTTPException) as last_message_error:
            await conversation_routes.get_last_message_id(conversation_id, user)
        with pytest.raises(HTTPException) as status_error:
            await conversation_routes.conversation_status(conversation_id, user)
        errors.extend([last_message_error.value, status_error.value])

    assert {
        (error.status_code, error.detail)
        for error in errors
    } == {(404, "Conversation not found")}


class _PromptCursor:
    def __init__(self, connection, sql=None, params=()):
        self.connection = connection
        self.row = None
        if sql is not None:
            self._set_query(sql, params)

    def _set_query(self, sql, params):
        normalized = " ".join(sql.split())
        self.connection.queries.append((normalized, params))
        lower = normalized.lower()
        if "select role_name from user_roles" in lower:
            self.row = ("user",)
        elif "select user_id from prompt_permissions" in lower and "permission_level = 'owner'" in lower:
            self.row = (1,)
        elif "select public_id from prompts" in lower:
            self.row = ("public-id",)
        elif "select id from voices" in lower:
            self.row = {"id": 5}
        elif "select pack_notice_period_days, allow_in_packs" in lower:
            self.row = (0, 0)
        else:
            self.row = None

    async def execute(self, sql, params=()):
        self._set_query(sql, params)
        return self

    async def fetchone(self):
        return self.row

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _PromptConnection:
    def __init__(self):
        self.queries = []
        self.commits = 0

    def execute(self, sql, params=()):
        return _PromptCursor(self, sql, params)

    def cursor(self):
        return _PromptCursor(self)

    async def commit(self):
        self.commits += 1


async def _save_prompt_as_editor(monkeypatch, can_manage=True):
    connection = _PromptConnection()

    @asynccontextmanager
    async def get_connection(readonly=False):
        yield connection

    monkeypatch.setattr(prompts, "get_db_connection", get_connection)
    monkeypatch.setattr(
        prompts,
        "get_prompt_info",
        AsyncMock(return_value={"name": "Original"}),
    )
    monkeypatch.setattr(
        prompts,
        "can_manage_prompt",
        AsyncMock(return_value=can_manage),
    )
    monkeypatch.setattr(prompts, "invalidate_landing_cache", Mock())

    response = await prompts.update_prompt(
        request=SimpleNamespace(),
        prompt_id=7,
        current_user=SimpleNamespace(id=2, role_id=2),
        name="Edited",
        prompt="Edited prompt",
        description="Edited description",
        sample_voice_id="voice-code",
        public=False,
        image=None,
        editor_ids=None,
        new_owner_id=None,
        category_ids="[]",
        is_paid=0,
        markup_per_mtokens=0.0,
        llm_mode="any",
        forced_llm_id=None,
        hide_llm_name=False,
        allowed_llms=None,
        disable_web_search=False,
        force_web_search=False,
        enable_moderation=False,
        watchdog_config=None,
        allow_in_packs=False,
        pack_notice_period_days=0,
        extensions_enabled=False,
        extensions_auto_advance=False,
        extensions_free_selection=True,
        purchase_price=None,
        gransabio_enabled=False,
        gransabio_config=None,
    )
    return response, connection


@pytest.mark.asyncio
async def test_prompt_editor_can_save_without_replacing_permissions(monkeypatch):
    response, connection = await _save_prompt_as_editor(monkeypatch)
    queries = [query.lower() for query, _ in connection.queries]

    assert response.status_code == 303
    assert any(query.startswith("update prompts set") for query in queries)
    assert not any(
        query.startswith("delete from prompt_permissions")
        for query in queries
    )
    assert not any(
        query.startswith("update prompt_permissions set user_id")
        for query in queries
    )


@pytest.mark.asyncio
async def test_user_without_prompt_permission_cannot_save(monkeypatch):
    with pytest.raises(HTTPException) as denied:
        await _save_prompt_as_editor(monkeypatch, can_manage=False)
    assert denied.value.status_code == 403
