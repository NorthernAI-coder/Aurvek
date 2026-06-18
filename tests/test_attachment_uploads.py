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
        "INSERT INTO USER_DETAILS (user_id, allow_file_upload) VALUES (?, 1)",
        (user_id,),
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
    monkeypatch.setattr(attachment_uploads, "ATTACHMENT_UPLOAD_MIN_CHUNK_SIZE_BYTES", 1)
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
            chunk_size=4,
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
    monkeypatch.setattr(attachment_uploads, "ATTACHMENT_UPLOAD_MIN_CHUNK_SIZE_BYTES", 1)

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
        chunk_size=4,
        chunk=_upload_file(b"hello"),
    )

    assert response.status_code == 400
    payload = orjson.loads(response.body)
    assert "Chunk" in payload["message"]


@pytest.mark.asyncio
async def test_chunk_upload_without_chunk_size_uses_current_default_when_it_matches(mock_db, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(attachment_routes, "get_db_connection", mock_db)
    monkeypatch.setattr(attachment_uploads, "get_db_connection", mock_db)
    monkeypatch.setattr(attachment_routes, "ATTACHMENT_UPLOAD_CHUNK_ROOT", tmp_path / "upload_chunks")
    monkeypatch.setattr(attachment_uploads, "ATTACHMENT_UPLOAD_CHUNK_ROOT", tmp_path / "upload_chunks")
    monkeypatch.setattr(file_storage, "FILE_BLOB_ROOT", tmp_path / "file_blobs")

    user = DummyUser()
    conversation_id = 41
    upload_id = "upload_legacy_001"
    data = b"legacy client payload"

    async with mock_db() as conn:
        await _seed_user_conversation(conn, user_id=user.id, conversation_id=conversation_id)

    response = await attachment_routes.upload_attachment_chunk(
        conversation_id=conversation_id,
        current_user=user,
        upload_id=upload_id,
        chunk_index=0,
        total_chunks=1,
        filename="notes.txt",
        content_type="text/plain",
        total_size=len(data),
        chunk=_upload_file(data),
    )
    assert response.status_code == 200

    upload_dir = tmp_path / "upload_chunks" / str(user.id) / str(conversation_id) / upload_id
    metadata = orjson.loads((upload_dir / "meta.json").read_bytes())
    assert metadata["chunk_size"] == attachment_routes.ATTACHMENT_UPLOAD_CHUNK_SIZE_BYTES

    complete = await _complete(
        user,
        conversation_id=conversation_id,
        upload_id=upload_id,
        total_chunks=1,
        total_size=len(data),
    )
    payload = orjson.loads(complete.body)
    assert complete.status_code == 200

    pending = await file_storage.read_pending_attachment_bytes(
        payload["attachment_ref"],
        user_id=user.id,
        conversation_id=conversation_id,
    )
    assert pending is not None
    pending_data, _attachment = pending
    assert pending_data == data


@pytest.mark.asyncio
async def test_chunk_upload_without_chunk_size_accepts_legacy_four_mb_client(mock_db, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(attachment_routes, "get_db_connection", mock_db)
    monkeypatch.setattr(attachment_uploads, "get_db_connection", mock_db)
    monkeypatch.setattr(attachment_routes, "ATTACHMENT_UPLOAD_CHUNK_ROOT", tmp_path / "upload_chunks")
    monkeypatch.setattr(attachment_uploads, "ATTACHMENT_UPLOAD_CHUNK_ROOT", tmp_path / "upload_chunks")

    user = DummyUser()
    conversation_id = 41
    upload_id = "upload_legacy_4mb_001"
    data = b"x" * (3 * 1024 * 1024)

    async with mock_db() as conn:
        await _seed_user_conversation(conn, user_id=user.id, conversation_id=conversation_id)

    response = await attachment_routes.upload_attachment_chunk(
        conversation_id=conversation_id,
        current_user=user,
        upload_id=upload_id,
        chunk_index=0,
        total_chunks=1,
        filename="capture.png",
        content_type="image/png",
        total_size=len(data),
        chunk=_upload_file(data),
    )
    assert response.status_code == 200

    upload_dir = tmp_path / "upload_chunks" / str(user.id) / str(conversation_id) / upload_id
    metadata = orjson.loads((upload_dir / "meta.json").read_bytes())
    assert metadata["chunk_size"] == attachment_routes.ATTACHMENT_UPLOAD_LEGACY_CHUNK_SIZE_BYTES
    assert (upload_dir / "000000.part").stat().st_size == len(data)


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


async def _send_chunk(
    user,
    *,
    conversation_id: int,
    upload_id: str,
    chunk_index: int,
    total_chunks: int,
    total_size: int,
    chunk_size: int,
    chunk: bytes,
    filename: str = "notes.txt",
    content_type: str = "text/plain",
):
    return await attachment_routes.upload_attachment_chunk(
        conversation_id=conversation_id,
        current_user=user,
        upload_id=upload_id,
        chunk_index=chunk_index,
        total_chunks=total_chunks,
        filename=filename,
        content_type=content_type,
        total_size=total_size,
        chunk_size=chunk_size,
        chunk=_upload_file(chunk),
    )


async def _complete(
    user,
    *,
    conversation_id: int,
    upload_id: str,
    total_chunks: int,
    total_size: int,
    filename: str = "notes.txt",
    content_type: str = "text/plain",
):
    return await attachment_routes.complete_attachment_upload(
        conversation_id=conversation_id,
        current_user=user,
        upload_id=upload_id,
        total_chunks=total_chunks,
        filename=filename,
        content_type=content_type,
        total_size=total_size,
    )


def test_assert_chunk_bounds_consistent_raises_and_passes() -> None:
    # Inconsistent: a 25MB cap at 256KB min chunk needs ~100 chunks but only 10 allowed.
    with pytest.raises(RuntimeError):
        attachment_uploads._assert_chunk_bounds_consistent(25 * 1024 * 1024, 256 * 1024, 10)
    # Consistent: same cap with the default 128 chunk budget.
    attachment_uploads._assert_chunk_bounds_consistent(25 * 1024 * 1024, 256 * 1024, 128)


@pytest.mark.asyncio
async def test_variable_chunk_size_round_trip(mock_db, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(attachment_routes, "get_db_connection", mock_db)
    monkeypatch.setattr(attachment_uploads, "get_db_connection", mock_db)
    monkeypatch.setattr(attachment_routes, "ATTACHMENT_UPLOAD_CHUNK_ROOT", tmp_path / "upload_chunks")
    monkeypatch.setattr(attachment_uploads, "ATTACHMENT_UPLOAD_CHUNK_ROOT", tmp_path / "upload_chunks")
    monkeypatch.setattr(attachment_uploads, "ATTACHMENT_UPLOAD_MIN_CHUNK_SIZE_BYTES", 1)
    monkeypatch.setattr(file_storage, "FILE_BLOB_ROOT", tmp_path / "file_blobs")

    user = DummyUser()
    conversation_id = 41
    data = b"abcdefghijklmnopqrstuvwxyz0123"
    chunk_size = 7
    upload_id = "upload_var_001"
    total_chunks = (len(data) + chunk_size - 1) // chunk_size

    async with mock_db() as conn:
        await _seed_user_conversation(conn, user_id=user.id, conversation_id=conversation_id)

    for index in range(total_chunks):
        start = index * chunk_size
        response = await _send_chunk(
            user,
            conversation_id=conversation_id,
            upload_id=upload_id,
            chunk_index=index,
            total_chunks=total_chunks,
            total_size=len(data),
            chunk_size=chunk_size,
            chunk=data[start:start + chunk_size],
        )
        assert response.status_code == 200

    complete = await _complete(
        user,
        conversation_id=conversation_id,
        upload_id=upload_id,
        total_chunks=total_chunks,
        total_size=len(data),
    )
    payload = orjson.loads(complete.body)
    assert complete.status_code == 200

    pending = await file_storage.read_pending_attachment_bytes(
        payload["attachment_ref"],
        user_id=user.id,
        conversation_id=conversation_id,
    )
    assert pending is not None
    pending_data, _attachment = pending
    assert pending_data == data


@pytest.mark.asyncio
async def test_status_endpoint_reports_received_chunks(mock_db, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(attachment_routes, "get_db_connection", mock_db)
    monkeypatch.setattr(attachment_uploads, "get_db_connection", mock_db)
    monkeypatch.setattr(attachment_routes, "ATTACHMENT_UPLOAD_CHUNK_ROOT", tmp_path / "upload_chunks")
    monkeypatch.setattr(attachment_uploads, "ATTACHMENT_UPLOAD_CHUNK_ROOT", tmp_path / "upload_chunks")
    monkeypatch.setattr(attachment_uploads, "ATTACHMENT_UPLOAD_MIN_CHUNK_SIZE_BYTES", 1)

    user = DummyUser()
    conversation_id = 41
    data = b"hello world!!"
    chunk_size = 4
    upload_id = "upload_status_001"
    total_chunks = (len(data) + chunk_size - 1) // chunk_size

    async with mock_db() as conn:
        await _seed_user_conversation(conn, user_id=user.id, conversation_id=conversation_id)

    # Send only chunks 0 and 2 (a subset).
    for index in (0, 2):
        start = index * chunk_size
        response = await _send_chunk(
            user,
            conversation_id=conversation_id,
            upload_id=upload_id,
            chunk_index=index,
            total_chunks=total_chunks,
            total_size=len(data),
            chunk_size=chunk_size,
            chunk=data[start:start + chunk_size],
        )
        assert response.status_code == 200

    status = await attachment_routes.attachment_upload_status(
        conversation_id=conversation_id,
        current_user=user,
        upload_id=upload_id,
    )
    payload = orjson.loads(status.body)
    assert payload["exists"] is True
    assert payload["received_chunks"] == [0, 2]
    assert payload["chunk_size"] == chunk_size
    assert payload["total_chunks"] == total_chunks
    assert payload["total_size"] == len(data)
    assert payload["filename"] == "notes.txt"

    unknown = await attachment_routes.attachment_upload_status(
        conversation_id=conversation_id,
        current_user=user,
        upload_id="upload_unknown_999",
    )
    unknown_payload = orjson.loads(unknown.body)
    assert unknown_payload["exists"] is False
    assert unknown_payload["received_chunks"] == []


@pytest.mark.asyncio
async def test_out_of_order_resume_completes(mock_db, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(attachment_routes, "get_db_connection", mock_db)
    monkeypatch.setattr(attachment_uploads, "get_db_connection", mock_db)
    monkeypatch.setattr(attachment_routes, "ATTACHMENT_UPLOAD_CHUNK_ROOT", tmp_path / "upload_chunks")
    monkeypatch.setattr(attachment_uploads, "ATTACHMENT_UPLOAD_CHUNK_ROOT", tmp_path / "upload_chunks")
    monkeypatch.setattr(attachment_uploads, "ATTACHMENT_UPLOAD_MIN_CHUNK_SIZE_BYTES", 1)
    monkeypatch.setattr(file_storage, "FILE_BLOB_ROOT", tmp_path / "file_blobs")

    user = DummyUser()
    conversation_id = 41
    data = b"hello world!"
    chunk_size = 4
    upload_id = "upload_oo_001"
    total_chunks = (len(data) + chunk_size - 1) // chunk_size
    assert total_chunks == 3

    async with mock_db() as conn:
        await _seed_user_conversation(conn, user_id=user.id, conversation_id=conversation_id)

    # First arriving chunks are 0 and 2 (skipping 1), meta written by chunk 0; then 1 last.
    for index in (0, 2, 1):
        start = index * chunk_size
        response = await _send_chunk(
            user,
            conversation_id=conversation_id,
            upload_id=upload_id,
            chunk_index=index,
            total_chunks=total_chunks,
            total_size=len(data),
            chunk_size=chunk_size,
            chunk=data[start:start + chunk_size],
        )
        assert response.status_code == 200

    complete = await _complete(
        user,
        conversation_id=conversation_id,
        upload_id=upload_id,
        total_chunks=total_chunks,
        total_size=len(data),
    )
    payload = orjson.loads(complete.body)
    assert complete.status_code == 200

    pending = await file_storage.read_pending_attachment_bytes(
        payload["attachment_ref"],
        user_id=user.id,
        conversation_id=conversation_id,
    )
    assert pending is not None
    pending_data, _attachment = pending
    assert pending_data == data


@pytest.mark.asyncio
async def test_chunk_size_below_min_rejected(mock_db, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(attachment_routes, "get_db_connection", mock_db)
    monkeypatch.setattr(attachment_uploads, "get_db_connection", mock_db)
    monkeypatch.setattr(attachment_routes, "ATTACHMENT_UPLOAD_CHUNK_ROOT", tmp_path / "upload_chunks")
    monkeypatch.setattr(attachment_uploads, "ATTACHMENT_UPLOAD_CHUNK_ROOT", tmp_path / "upload_chunks")

    user = DummyUser()
    async with mock_db() as conn:
        await _seed_user_conversation(conn, user_id=user.id, conversation_id=41)

    # Default MIN is 256KB; a chunk_size of 4 is below it.
    response = await _send_chunk(
        user,
        conversation_id=41,
        upload_id="upload_min_001",
        chunk_index=0,
        total_chunks=1,
        total_size=4,
        chunk_size=4,
        chunk=b"data",
    )
    assert response.status_code == 400
    payload = orjson.loads(response.body)
    assert payload["message"] == "Invalid chunk size"


@pytest.mark.asyncio
async def test_chunk_size_above_max_rejected(mock_db, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(attachment_routes, "get_db_connection", mock_db)
    monkeypatch.setattr(attachment_uploads, "get_db_connection", mock_db)
    monkeypatch.setattr(attachment_routes, "ATTACHMENT_UPLOAD_CHUNK_ROOT", tmp_path / "upload_chunks")
    monkeypatch.setattr(attachment_uploads, "ATTACHMENT_UPLOAD_CHUNK_ROOT", tmp_path / "upload_chunks")

    user = DummyUser()
    async with mock_db() as conn:
        await _seed_user_conversation(conn, user_id=user.id, conversation_id=41)

    over_max = attachment_uploads.ATTACHMENT_UPLOAD_MAX_CHUNK_SIZE_BYTES + 1
    response = await _send_chunk(
        user,
        conversation_id=41,
        upload_id="upload_max_001",
        chunk_index=0,
        total_chunks=1,
        total_size=4,
        chunk_size=over_max,
        chunk=b"data",
    )
    assert response.status_code == 400
    payload = orjson.loads(response.body)
    assert payload["message"] == "Invalid chunk size"


@pytest.mark.asyncio
async def test_chunk_size_mismatch_mid_upload_rejected(mock_db, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(attachment_routes, "get_db_connection", mock_db)
    monkeypatch.setattr(attachment_uploads, "get_db_connection", mock_db)
    monkeypatch.setattr(attachment_routes, "ATTACHMENT_UPLOAD_CHUNK_ROOT", tmp_path / "upload_chunks")
    monkeypatch.setattr(attachment_uploads, "ATTACHMENT_UPLOAD_CHUNK_ROOT", tmp_path / "upload_chunks")
    monkeypatch.setattr(attachment_uploads, "ATTACHMENT_UPLOAD_MIN_CHUNK_SIZE_BYTES", 1)

    user = DummyUser()
    conversation_id = 41
    data = b"hello world!"
    upload_id = "upload_mismatch_001"

    async with mock_db() as conn:
        await _seed_user_conversation(conn, user_id=user.id, conversation_id=conversation_id)

    # First chunk establishes chunk_size=4 (-> total_chunks=3) in meta.
    first = await _send_chunk(
        user,
        conversation_id=conversation_id,
        upload_id=upload_id,
        chunk_index=0,
        total_chunks=3,
        total_size=len(data),
        chunk_size=4,
        chunk=data[0:4],
    )
    assert first.status_code == 200

    # A later chunk claims chunk_size=6 (-> total_chunks=2); comparable_keys mismatch.
    second = await _send_chunk(
        user,
        conversation_id=conversation_id,
        upload_id=upload_id,
        chunk_index=1,
        total_chunks=2,
        total_size=len(data),
        chunk_size=6,
        chunk=data[6:12],
    )
    assert second.status_code == 400
    payload = orjson.loads(second.body)
    assert payload["message"] == "Upload metadata changed during transfer"


@pytest.mark.asyncio
async def test_duplicate_identical_chunk_is_idempotent(mock_db, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(attachment_routes, "get_db_connection", mock_db)
    monkeypatch.setattr(attachment_uploads, "get_db_connection", mock_db)
    monkeypatch.setattr(attachment_routes, "ATTACHMENT_UPLOAD_CHUNK_ROOT", tmp_path / "upload_chunks")
    monkeypatch.setattr(attachment_uploads, "ATTACHMENT_UPLOAD_CHUNK_ROOT", tmp_path / "upload_chunks")
    monkeypatch.setattr(attachment_uploads, "ATTACHMENT_UPLOAD_MIN_CHUNK_SIZE_BYTES", 1)

    user = DummyUser()
    conversation_id = 41
    data = b"hello world!"
    upload_id = "upload_dupe_001"
    chunk_size = 4
    total_chunks = (len(data) + chunk_size - 1) // chunk_size

    async with mock_db() as conn:
        await _seed_user_conversation(conn, user_id=user.id, conversation_id=conversation_id)

    first = await _send_chunk(
        user,
        conversation_id=conversation_id,
        upload_id=upload_id,
        chunk_index=0,
        total_chunks=total_chunks,
        total_size=len(data),
        chunk_size=chunk_size,
        chunk=data[0:4],
    )
    second = await _send_chunk(
        user,
        conversation_id=conversation_id,
        upload_id=upload_id,
        chunk_index=0,
        total_chunks=total_chunks,
        total_size=len(data),
        chunk_size=chunk_size,
        chunk=data[0:4],
    )

    assert first.status_code == 200
    assert second.status_code == 200
    upload_dir = tmp_path / "upload_chunks" / str(user.id) / str(conversation_id) / upload_id
    assert (upload_dir / "000000.part").read_bytes() == data[0:4]
    assert list(upload_dir.glob("*.tmp")) == []


@pytest.mark.asyncio
async def test_duplicate_different_chunk_does_not_overwrite_existing_part(mock_db, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(attachment_routes, "get_db_connection", mock_db)
    monkeypatch.setattr(attachment_uploads, "get_db_connection", mock_db)
    monkeypatch.setattr(attachment_routes, "ATTACHMENT_UPLOAD_CHUNK_ROOT", tmp_path / "upload_chunks")
    monkeypatch.setattr(attachment_uploads, "ATTACHMENT_UPLOAD_CHUNK_ROOT", tmp_path / "upload_chunks")
    monkeypatch.setattr(attachment_uploads, "ATTACHMENT_UPLOAD_MIN_CHUNK_SIZE_BYTES", 1)
    monkeypatch.setattr(file_storage, "FILE_BLOB_ROOT", tmp_path / "file_blobs")

    user = DummyUser()
    conversation_id = 41
    data = b"hello world!"
    upload_id = "upload_dupe_002"
    chunk_size = 4
    total_chunks = (len(data) + chunk_size - 1) // chunk_size

    async with mock_db() as conn:
        await _seed_user_conversation(conn, user_id=user.id, conversation_id=conversation_id)

    first = await _send_chunk(
        user,
        conversation_id=conversation_id,
        upload_id=upload_id,
        chunk_index=0,
        total_chunks=total_chunks,
        total_size=len(data),
        chunk_size=chunk_size,
        chunk=data[0:4],
    )
    conflicting = await _send_chunk(
        user,
        conversation_id=conversation_id,
        upload_id=upload_id,
        chunk_index=0,
        total_chunks=total_chunks,
        total_size=len(data),
        chunk_size=chunk_size,
        chunk=b"HELO",
    )

    assert first.status_code == 200
    assert conflicting.status_code == 400
    payload = orjson.loads(conflicting.body)
    assert payload["message"] == "Upload chunk already exists with different content"

    upload_dir = tmp_path / "upload_chunks" / str(user.id) / str(conversation_id) / upload_id
    assert (upload_dir / "000000.part").read_bytes() == data[0:4]

    for index in range(1, total_chunks):
        start = index * chunk_size
        response = await _send_chunk(
            user,
            conversation_id=conversation_id,
            upload_id=upload_id,
            chunk_index=index,
            total_chunks=total_chunks,
            total_size=len(data),
            chunk_size=chunk_size,
            chunk=data[start:start + chunk_size],
        )
        assert response.status_code == 200

    complete = await _complete(
        user,
        conversation_id=conversation_id,
        upload_id=upload_id,
        total_chunks=total_chunks,
        total_size=len(data),
    )
    complete_payload = orjson.loads(complete.body)
    assert complete.status_code == 200

    pending = await file_storage.read_pending_attachment_bytes(
        complete_payload["attachment_ref"],
        user_id=user.id,
        conversation_id=conversation_id,
    )
    assert pending is not None
    pending_data, _attachment = pending
    assert pending_data == data


@pytest.mark.asyncio
async def test_successful_chunk_leaves_no_tmp_part(mock_db, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(attachment_routes, "get_db_connection", mock_db)
    monkeypatch.setattr(attachment_uploads, "get_db_connection", mock_db)
    monkeypatch.setattr(attachment_routes, "ATTACHMENT_UPLOAD_CHUNK_ROOT", tmp_path / "upload_chunks")
    monkeypatch.setattr(attachment_uploads, "ATTACHMENT_UPLOAD_CHUNK_ROOT", tmp_path / "upload_chunks")
    monkeypatch.setattr(attachment_uploads, "ATTACHMENT_UPLOAD_MIN_CHUNK_SIZE_BYTES", 1)

    user = DummyUser()
    conversation_id = 41
    data = b"hello world!"
    chunk_size = 4
    upload_id = "upload_tmp_001"
    total_chunks = (len(data) + chunk_size - 1) // chunk_size

    async with mock_db() as conn:
        await _seed_user_conversation(conn, user_id=user.id, conversation_id=conversation_id)

    for index in range(total_chunks):
        start = index * chunk_size
        response = await _send_chunk(
            user,
            conversation_id=conversation_id,
            upload_id=upload_id,
            chunk_index=index,
            total_chunks=total_chunks,
            total_size=len(data),
            chunk_size=chunk_size,
            chunk=data[start:start + chunk_size],
        )
        assert response.status_code == 200

    upload_dir = tmp_path / "upload_chunks" / str(user.id) / str(conversation_id) / upload_id
    assert list(upload_dir.glob("*.tmp")) == []
    assert sorted(p.name for p in upload_dir.glob("*.part")) == [
        "000000.part",
        "000001.part",
        "000002.part",
    ]


@pytest.mark.asyncio
async def test_status_corrupted_meta_returns_exists_false(mock_db, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(attachment_routes, "get_db_connection", mock_db)
    monkeypatch.setattr(attachment_uploads, "get_db_connection", mock_db)
    monkeypatch.setattr(attachment_routes, "ATTACHMENT_UPLOAD_CHUNK_ROOT", tmp_path / "upload_chunks")
    monkeypatch.setattr(attachment_uploads, "ATTACHMENT_UPLOAD_CHUNK_ROOT", tmp_path / "upload_chunks")

    user = DummyUser()
    conversation_id = 41
    upload_id = "upload_corrupt_001"

    async with mock_db() as conn:
        await _seed_user_conversation(conn, user_id=user.id, conversation_id=conversation_id)

    upload_dir = tmp_path / "upload_chunks" / str(user.id) / str(conversation_id) / upload_id
    upload_dir.mkdir(parents=True)
    (upload_dir / "meta.json").write_bytes(b"{not valid json")

    status = await attachment_routes.attachment_upload_status(
        conversation_id=conversation_id,
        current_user=user,
        upload_id=upload_id,
    )
    assert status.status_code == 200
    payload = orjson.loads(status.body)
    assert payload["exists"] is False
    assert payload["received_chunks"] == []


@pytest.mark.asyncio
async def test_chunk_corrupted_meta_returns_400_and_removes_upload_dir(mock_db, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(attachment_routes, "get_db_connection", mock_db)
    monkeypatch.setattr(attachment_uploads, "get_db_connection", mock_db)
    monkeypatch.setattr(attachment_routes, "ATTACHMENT_UPLOAD_CHUNK_ROOT", tmp_path / "upload_chunks")
    monkeypatch.setattr(attachment_uploads, "ATTACHMENT_UPLOAD_CHUNK_ROOT", tmp_path / "upload_chunks")
    monkeypatch.setattr(attachment_uploads, "ATTACHMENT_UPLOAD_MIN_CHUNK_SIZE_BYTES", 1)

    user = DummyUser()
    conversation_id = 41
    upload_id = "upload_corrupt_002"

    async with mock_db() as conn:
        await _seed_user_conversation(conn, user_id=user.id, conversation_id=conversation_id)

    upload_dir = tmp_path / "upload_chunks" / str(user.id) / str(conversation_id) / upload_id
    upload_dir.mkdir(parents=True)
    (upload_dir / "meta.json").write_bytes(b"{not valid json")

    response = await _send_chunk(
        user,
        conversation_id=conversation_id,
        upload_id=upload_id,
        chunk_index=0,
        total_chunks=1,
        total_size=4,
        chunk_size=4,
        chunk=b"data",
    )
    payload = orjson.loads(response.body)
    assert response.status_code == 400
    assert payload["message"] == "Upload metadata is corrupted. Please attach the file again."
    assert not upload_dir.exists()


@pytest.mark.asyncio
async def test_status_non_dict_meta_returns_exists_false(mock_db, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(attachment_routes, "get_db_connection", mock_db)
    monkeypatch.setattr(attachment_uploads, "get_db_connection", mock_db)
    monkeypatch.setattr(attachment_routes, "ATTACHMENT_UPLOAD_CHUNK_ROOT", tmp_path / "upload_chunks")
    monkeypatch.setattr(attachment_uploads, "ATTACHMENT_UPLOAD_CHUNK_ROOT", tmp_path / "upload_chunks")

    user = DummyUser()
    conversation_id = 41
    upload_id = "upload_nondict_001"

    async with mock_db() as conn:
        await _seed_user_conversation(conn, user_id=user.id, conversation_id=conversation_id)

    upload_dir = tmp_path / "upload_chunks" / str(user.id) / str(conversation_id) / upload_id
    upload_dir.mkdir(parents=True)
    (upload_dir / "meta.json").write_bytes(b"[]")  # valid JSON, but not an object

    status = await attachment_routes.attachment_upload_status(
        conversation_id=conversation_id,
        current_user=user,
        upload_id=upload_id,
    )
    assert status.status_code == 200
    payload = orjson.loads(status.body)
    assert payload["exists"] is False
    assert payload["received_chunks"] == []


@pytest.mark.asyncio
async def test_status_skips_non_numeric_part_stems(mock_db, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(attachment_routes, "get_db_connection", mock_db)
    monkeypatch.setattr(attachment_uploads, "get_db_connection", mock_db)
    monkeypatch.setattr(attachment_routes, "ATTACHMENT_UPLOAD_CHUNK_ROOT", tmp_path / "upload_chunks")
    monkeypatch.setattr(attachment_uploads, "ATTACHMENT_UPLOAD_CHUNK_ROOT", tmp_path / "upload_chunks")

    user = DummyUser()
    conversation_id = 41
    upload_id = "upload_stray_001"

    async with mock_db() as conn:
        await _seed_user_conversation(conn, user_id=user.id, conversation_id=conversation_id)

    upload_dir = tmp_path / "upload_chunks" / str(user.id) / str(conversation_id) / upload_id
    upload_dir.mkdir(parents=True)
    (upload_dir / "meta.json").write_bytes(orjson.dumps({
        "upload_id": upload_id,
        "filename": "notes.txt",
        "content_type": "text/plain",
        "total_size": 8,
        "total_chunks": 2,
        "chunk_size": 4,
        "user_id": user.id,
        "conversation_id": conversation_id,
    }))
    (upload_dir / "000000.part").write_bytes(b"abcd")
    (upload_dir / "stray.part").write_bytes(b"junk")  # non-numeric stem must be skipped
    (upload_dir / "1.part").write_bytes(b"junk")  # numeric, but not the 6-digit server format

    status = await attachment_routes.attachment_upload_status(
        conversation_id=conversation_id,
        current_user=user,
        upload_id=upload_id,
    )
    assert status.status_code == 200
    payload = orjson.loads(status.body)
    assert payload["exists"] is True
    assert payload["received_chunks"] == [0]


@pytest.mark.asyncio
async def test_db_file_upload_permission_revocation_blocks_chunk(mock_db, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(attachment_routes, "get_db_connection", mock_db)
    monkeypatch.setattr(attachment_uploads, "get_db_connection", mock_db)
    monkeypatch.setattr(attachment_routes, "ATTACHMENT_UPLOAD_CHUNK_ROOT", tmp_path / "upload_chunks")
    monkeypatch.setattr(attachment_uploads, "ATTACHMENT_UPLOAD_CHUNK_ROOT", tmp_path / "upload_chunks")
    monkeypatch.setattr(attachment_uploads, "ATTACHMENT_UPLOAD_MIN_CHUNK_SIZE_BYTES", 1)

    user = DummyUser()
    async with mock_db() as conn:
        await _seed_user_conversation(conn, user_id=user.id, conversation_id=41)
        await conn.execute("UPDATE USER_DETAILS SET allow_file_upload = 0 WHERE user_id = ?", (user.id,))
        await conn.commit()

    response = await _send_chunk(
        user,
        conversation_id=41,
        upload_id="upload_perm_001",
        chunk_index=0,
        total_chunks=1,
        total_size=4,
        chunk_size=4,
        chunk=b"data",
    )
    payload = orjson.loads(response.body)
    assert response.status_code == 403
    assert payload["message"] == "File uploads are not enabled for your account"
