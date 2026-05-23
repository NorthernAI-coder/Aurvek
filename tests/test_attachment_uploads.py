from __future__ import annotations

import io

import orjson
import pytest
from fastapi import UploadFile

from chat.routes import attachments as attachment_routes
from chat.services import attachment_uploads
import file_storage


class DummyUser:
    id = 31
    can_send_files = True


async def _seed_user_conversation(conn, *, user_id: int = 31, conversation_id: int = 41) -> None:
    await conn.execute(
        "INSERT INTO USERS (id, username) VALUES (?, ?)",
        (user_id, f"user{user_id}"),
    )
    await conn.execute(
        "INSERT INTO CONVERSATIONS (id, user_id, role_id) VALUES (?, ?, 1)",
        (conversation_id, user_id),
    )
    await conn.commit()


def _upload_file(data: bytes) -> UploadFile:
    return UploadFile(filename="chunk.bin", file=io.BytesIO(data))


@pytest.mark.asyncio
async def test_chunked_text_upload_creates_scoped_pending_attachment(mock_db, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(attachment_routes, "get_db_connection", mock_db)
    monkeypatch.setattr(attachment_uploads, "get_db_connection", mock_db)
    monkeypatch.setattr(attachment_routes, "ATTACHMENT_UPLOAD_CHUNK_ROOT", tmp_path / "upload_chunks")
    monkeypatch.setattr(attachment_uploads, "ATTACHMENT_UPLOAD_CHUNK_ROOT", tmp_path / "upload_chunks")
    monkeypatch.setattr(attachment_routes, "ATTACHMENT_UPLOAD_CHUNK_SIZE_BYTES", 4)
    monkeypatch.setattr(attachment_uploads, "ATTACHMENT_UPLOAD_CHUNK_SIZE_BYTES", 4)
    monkeypatch.setattr(file_storage, "FILE_BLOB_ROOT", tmp_path / "file_blobs")

    user = DummyUser()
    conversation_id = 41
    data = b"hello world"
    chunks = [data[0:4], data[4:8], data[8:]]
    upload_id = "upload_text_123"

    async with mock_db() as conn:
        await _seed_user_conversation(conn, user_id=user.id, conversation_id=conversation_id)

    for index, chunk in enumerate(chunks):
        response = await attachment_routes.upload_attachment_chunk(
            conversation_id=conversation_id,
            current_user=user,
            upload_id=upload_id,
            chunk_index=index,
            total_chunks=len(chunks),
            filename="notes.txt",
            content_type="text/plain",
            total_size=len(data),
            chunk=_upload_file(chunk),
        )
        assert response.status_code == 200

    complete = await attachment_routes.complete_attachment_upload(
        conversation_id=conversation_id,
        current_user=user,
        upload_id=upload_id,
        total_chunks=len(chunks),
        filename="notes.txt",
        content_type="text/plain",
        total_size=len(data),
    )
    payload = orjson.loads(complete.body)

    assert complete.status_code == 200
    assert payload["success"] is True
    assert payload["attachment_type"] == "text"
    assert payload["filename"] == "notes.txt"
    assert payload["attachment_ref"].startswith("att_")
    assert not (tmp_path / "upload_chunks" / str(user.id) / str(conversation_id) / upload_id).exists()

    pending = await file_storage.read_pending_attachment_bytes(
        payload["attachment_ref"],
        user_id=user.id,
        conversation_id=conversation_id,
    )
    assert pending is not None
    pending_data, attachment = pending
    assert pending_data == data
    assert attachment["status"] == "pending"


@pytest.mark.asyncio
async def test_chunk_upload_rejects_metadata_mismatch(mock_db, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(attachment_routes, "get_db_connection", mock_db)
    monkeypatch.setattr(attachment_uploads, "get_db_connection", mock_db)
    monkeypatch.setattr(attachment_routes, "ATTACHMENT_UPLOAD_CHUNK_ROOT", tmp_path / "upload_chunks")
    monkeypatch.setattr(attachment_uploads, "ATTACHMENT_UPLOAD_CHUNK_ROOT", tmp_path / "upload_chunks")
    monkeypatch.setattr(attachment_routes, "ATTACHMENT_UPLOAD_CHUNK_SIZE_BYTES", 4)
    monkeypatch.setattr(attachment_uploads, "ATTACHMENT_UPLOAD_CHUNK_SIZE_BYTES", 4)

    user = DummyUser()
    async with mock_db() as conn:
        await _seed_user_conversation(conn, user_id=user.id, conversation_id=42)

    response = await attachment_routes.upload_attachment_chunk(
        conversation_id=42,
        current_user=user,
        upload_id="upload_bad_123",
        chunk_index=0,
        total_chunks=3,
        filename="notes.txt",
        content_type="text/plain",
        total_size=11,
        chunk=_upload_file(b"hello"),
    )

    assert response.status_code == 400
    payload = orjson.loads(response.body)
    assert "Chunk" in payload["message"]


@pytest.mark.asyncio
async def test_discard_uploaded_attachments_is_scoped(mock_db, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(attachment_routes, "get_db_connection", mock_db)
    monkeypatch.setattr(attachment_uploads, "get_db_connection", mock_db)
    monkeypatch.setattr(file_storage, "FILE_BLOB_ROOT", tmp_path / "file_blobs")

    user = DummyUser()
    async with mock_db() as conn:
        await _seed_user_conversation(conn, user_id=user.id, conversation_id=43)
        await _seed_user_conversation(conn, user_id=99, conversation_id=44)

    own = await file_storage.create_pending_text_attachment(
        user_id=user.id,
        conversation_id=43,
        text_content="own",
        filename="own.txt",
    )
    other = await file_storage.create_pending_text_attachment(
        user_id=99,
        conversation_id=44,
        text_content="other",
        filename="other.txt",
    )

    response = await attachment_routes.discard_uploaded_attachments(
        conversation_id=43,
        current_user=user,
        attachment_refs=orjson.dumps([own.public_id, other.public_id]).decode(),
    )
    payload = orjson.loads(response.body)

    assert response.status_code == 200
    assert payload["discarded"] == 1
    assert await file_storage.read_pending_attachment_bytes(own.public_id, user_id=user.id, conversation_id=43) is None
    assert await file_storage.read_pending_attachment_bytes(other.public_id, user_id=99, conversation_id=44) is not None


@pytest.mark.asyncio
async def test_prune_stale_attachment_upload_chunks(mock_db, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(attachment_uploads, "ATTACHMENT_UPLOAD_CHUNK_ROOT", tmp_path / "upload_chunks")
    monkeypatch.setattr(attachment_uploads, "ATTACHMENT_UPLOAD_TTL_SECONDS", 60)

    stale = tmp_path / "upload_chunks" / "1" / "2" / "stale_upload"
    fresh = tmp_path / "upload_chunks" / "1" / "2" / "fresh_upload"
    stale.mkdir(parents=True)
    fresh.mkdir(parents=True)
    (stale / "000000.part").write_bytes(b"stale")
    (fresh / "000000.part").write_bytes(b"fresh")
    old_time = 1_700_000_000
    import os
    os.utime(stale, (old_time, old_time))

    pruned = await attachment_uploads.prune_stale_attachment_upload_chunks()

    assert pruned == 1
    assert not stale.exists()
    assert fresh.exists()
