import time

import pytest
from fastapi import HTTPException

from chat.routes import media, voice_io


class DummyRequest:
    headers = {"user-agent": "Chrome"}


class DummyUser:
    def __init__(self, user_id: int, *, admin: bool = False):
        self.id = user_id
        self._admin = admin

    @property
    async def is_admin(self):
        return self._admin


async def _seed_conversation(conn, *, user_id: int, conversation_id: int) -> None:
    await conn.execute(
        "INSERT INTO USERS (id, username) VALUES (?, ?)",
        (user_id, f"user{user_id}"),
    )
    await conn.execute(
        "INSERT INTO CONVERSATIONS (id, user_id, role_id) VALUES (?, ?, 1)",
        (conversation_id, user_id),
    )
    await conn.commit()


@pytest.mark.asyncio
async def test_transcribe_web_requires_conversation_access_before_transcribing(mock_db, monkeypatch):
    monkeypatch.setattr(voice_io, "get_db_connection", mock_db)
    async with mock_db() as conn:
        await _seed_conversation(conn, user_id=2, conversation_id=10)

    async def fail_transcribe(*args, **kwargs):
        raise AssertionError("transcribe should not run without conversation access")

    monkeypatch.setattr(voice_io, "transcribe", fail_transcribe)

    with pytest.raises(HTTPException) as exc_info:
        await voice_io.transcribe_web(
            DummyRequest(),
            audio=None,
            conversation_id="10",
            current_user=DummyUser(1),
        )

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_transcribe_web_bills_authenticated_owner(mock_db, monkeypatch):
    monkeypatch.setattr(voice_io, "get_db_connection", mock_db)
    async with mock_db() as conn:
        await _seed_conversation(conn, user_id=1, conversation_id=11)

    seen = {}

    async def fake_transcribe(request, audio, user_id):
        seen["user_id"] = user_id
        return "hola"

    monkeypatch.setattr(voice_io, "transcribe", fake_transcribe)

    response = await voice_io.transcribe_web(
        DummyRequest(),
        audio=None,
        conversation_id="11",
        current_user=DummyUser(1),
    )

    assert response.status_code == 200
    assert seen == {"user_id": 1}


@pytest.mark.asyncio
async def test_download_pdf_checks_access_before_redis_lock(mock_db, monkeypatch):
    monkeypatch.setattr(voice_io, "get_db_connection", mock_db)
    async with mock_db() as conn:
        await _seed_conversation(conn, user_id=2, conversation_id=12)

    class RedisShouldNotRun:
        async def set(self, *args, **kwargs):
            raise AssertionError("redis lock should not be created without conversation access")

    monkeypatch.setattr(voice_io, "redis_client", RedisShouldNotRun())

    with pytest.raises(HTTPException) as exc_info:
        await voice_io.initiate_download_pdf(
            conversation_id=12,
            request=DummyRequest(),
            current_user=DummyUser(1),
        )

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_auth_file_rejects_expired_token(monkeypatch):
    monkeypatch.setattr(
        media,
        "decode_jwt_cached",
        lambda token, secret: {"username": "alice", "exp": int(time.time()) - 1},
    )

    with pytest.raises(HTTPException) as exc_info:
        await media.auth_file(DummyRequest(), request_uri="files/demo.pdf", token="token")

    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_auth_file_requires_token_user_path_prefix(monkeypatch):
    monkeypatch.setattr(
        media,
        "decode_jwt_cached",
        lambda token, secret: {"username": "alice", "exp": int(time.time()) + 60},
    )
    monkeypatch.setattr(media, "validate_path_within_directory", lambda relative, base: base / relative)

    hash_prefix1, hash_prefix2, user_hash = media.generate_user_hash("alice")
    own_uri = f"users/{hash_prefix1}/{hash_prefix2}/{user_hash}/files/demo.pdf"
    response = await media.auth_file(DummyRequest(), request_uri=own_uri, token="token")
    assert response.status_code == 200

    with pytest.raises(HTTPException) as exc_info:
        await media.auth_file(
            DummyRequest(),
            request_uri="users/aa/bbb/not_alice/files/demo.pdf",
            token="token",
        )

    assert exc_info.value.status_code == 403
