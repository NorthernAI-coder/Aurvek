import base64
import json
import sqlite3
from contextlib import asynccontextmanager

import aiosqlite
import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from migration_external_devices import _create_schema
from ai_runtime.persistence.messages import assistant_content_for_storage, strip_aurvek_action_blocks
from ai_runtime.tooling import execution as tool_execution
from ai_runtime.watchdog import takeover as watchdog_takeover
from integrations import conversations as platform_conversations
from integrations.devices import admin_routes as device_admin_routes
from integrations.devices import routes as device_routes
from integrations.devices import service as device_service


class DummyOwner:
    id = 1
    username = "alice"
    is_enabled = True
    can_send_files = True

    @property
    async def is_admin(self):
        return False

    @property
    async def is_user(self):
        return True


@pytest.fixture()
def external_devices_db(tmp_path, monkeypatch):
    db_path = tmp_path / "external_devices.db"
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(
        """
        CREATE TABLE USERS (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL,
            phone_number TEXT,
            telegram_chat_id TEXT,
            phone_verified INTEGER DEFAULT 1,
            is_enabled INTEGER DEFAULT 1
        );

        CREATE TABLE USER_DETAILS (
            user_id INTEGER PRIMARY KEY,
            external_platforms TEXT,
            user_api_keys TEXT
        );

        CREATE TABLE SYSTEM_CONFIG (
            key TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE CONVERSATIONS (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            chat_name TEXT,
            locked INTEGER DEFAULT 0,
            last_activity TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES USERS(id)
        );

        CREATE TABLE MESSAGES (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id INTEGER NOT NULL,
            user_id INTEGER,
            message TEXT,
            type TEXT NOT NULL,
            date TEXT,
            FOREIGN KEY(conversation_id) REFERENCES CONVERSATIONS(id)
        );
        """
    )
    _create_schema(conn)
    conn.executemany(
        "INSERT INTO USERS (id, username, phone_number, telegram_chat_id) VALUES (?, ?, ?, ?)",
        [
            (1, "alice", "+15550000001", "tg-alice"),
            (2, "bob", "+15550000002", "tg-bob"),
        ],
    )
    conn.executemany(
        "INSERT INTO USER_DETAILS (user_id, external_platforms) VALUES (?, ?)",
        [
            (1, "{}"),
            (2, "{}"),
        ],
    )
    conn.executemany(
        "INSERT INTO CONVERSATIONS (id, user_id, chat_name) VALUES (?, ?, ?)",
        [
            (1, 1, "Salon"),
            (2, 1, "Office"),
            (3, 1, "WhatsApp target"),
            (20, 2, "Bob"),
        ],
    )
    conn.commit()
    conn.close()

    @asynccontextmanager
    async def _get_test_conn(readonly=False):
        mode = "ro" if readonly else "rwc"
        conn = await aiosqlite.connect(f"file:{db_path}?mode={mode}", uri=True)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
        finally:
            await conn.close()

    monkeypatch.setattr(device_service, "get_db_connection", _get_test_conn)
    monkeypatch.setattr(platform_conversations, "get_db_connection", _get_test_conn)
    return db_path


async def _seed_device(
    db_path,
    *,
    device_id,
    user_id,
    slug,
    token=None,
    enabled=True,
    capabilities=None,
):
    token = token or device_service.generate_device_token()
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            """
            INSERT INTO EXTERNAL_DEVICES
                (id, user_id, slug, display_name, device_type,
                 capabilities_json, token_hash, token_prefix, enabled)
            VALUES (?, ?, ?, ?, 'custom', ?, ?, ?, ?)
            """,
            (
                device_id,
                user_id,
                slug,
                slug.replace("-", " ").title(),
                json.dumps(capabilities or {}),
                device_service.hash_device_token(token),
                device_service.token_prefix(token),
                1 if enabled else 0,
            ),
        )
        await conn.commit()
    return token


async def _bind_device_to_conversation(
    db_path,
    *,
    device_id,
    user_id=1,
    conversation_id=1,
):
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            """
            INSERT INTO EXTERNAL_DEVICE_BINDINGS
                (user_id, target_type, target_id, conversation_id, response_mode)
            VALUES (?, 'device', ?, ?, 'text')
            """,
            (user_id, device_id, conversation_id),
        )
        await conn.commit()


def _devices_test_client(current_user=None) -> TestClient:
    app = FastAPI()
    app.include_router(device_routes.router)
    app.include_router(device_admin_routes.router)
    app.dependency_overrides[device_routes.get_current_user] = lambda: current_user
    app.dependency_overrides[device_admin_routes.get_current_user] = lambda: current_user
    return TestClient(app)


@pytest.mark.asyncio
async def test_external_device_routing_direct_group_conflict_and_missing(external_devices_db):
    await _seed_device(external_devices_db, device_id=10, user_id=1, slug="cam-direct")
    await _seed_device(external_devices_db, device_id=11, user_id=1, slug="cam-conflict")
    await _seed_device(external_devices_db, device_id=12, user_id=1, slug="cam-missing")

    async with aiosqlite.connect(external_devices_db) as conn:
        await conn.executescript(
            """
            INSERT INTO EXTERNAL_DEVICE_GROUPS (id, user_id, slug, name)
            VALUES (30, 1, 'salon', 'Salon'), (31, 1, 'office', 'Office');

            INSERT INTO EXTERNAL_DEVICE_GROUP_MEMBERS (device_id, group_id, routing_priority)
            VALUES (10, 30, 100), (11, 30, 100), (11, 31, 100);

            INSERT INTO EXTERNAL_DEVICE_BINDINGS (user_id, target_type, target_id, conversation_id, response_mode)
            VALUES
                (1, 'group', 30, 1, 'text'),
                (1, 'group', 31, 2, 'text'),
                (1, 'device', 10, 2, 'text');
            """
        )
        await conn.commit()

    async with device_service.get_db_connection(readonly=True) as conn:
        direct = await device_service.resolve_device_binding(conn, 10)
        conflict = await device_service.resolve_device_binding(conn, 11)
        missing = await device_service.resolve_device_binding(conn, 12)

    assert direct["status"] == "bound"
    assert direct["source"] == "device"
    assert direct["conversation_id"] == 2
    assert conflict["status"] == "routing_conflict"
    assert sorted(conflict["groups"]) == ["Office", "Salon"]
    assert missing["status"] == "setup_required"


@pytest.mark.asyncio
async def test_external_device_auth_rotation_disabled_and_me_is_read_only(external_devices_db):
    token = await _seed_device(
        external_devices_db,
        device_id=10,
        user_id=1,
        slug="cam-auth",
    )
    async with aiosqlite.connect(external_devices_db) as conn:
        await conn.execute(
            """
            INSERT INTO EXTERNAL_DEVICE_BINDINGS
                (user_id, target_type, target_id, conversation_id, response_mode)
            VALUES (1, 'device', 10, 1, 'text')
            """
        )
        await conn.commit()

    device = await device_service.authenticate_device_token(token)
    assert device.slug == "cam-auth"

    async with aiosqlite.connect(external_devices_db) as conn:
        before = await (await conn.execute("SELECT COUNT(*) FROM CONVERSATIONS")).fetchone()
    payload = await device_service.device_me_payload(device)
    async with aiosqlite.connect(external_devices_db) as conn:
        after = await (await conn.execute("SELECT COUNT(*) FROM CONVERSATIONS")).fetchone()
    assert payload["conversation_id"] == 1
    assert before[0] == after[0]

    rotated = await device_service.rotate_device_token(10)
    with pytest.raises(device_service.DeviceRuntimeError) as old_exc:
        await device_service.authenticate_device_token(token)
    assert old_exc.value.status_code == 401
    assert (await device_service.authenticate_device_token(rotated.token)).id == 10

    await device_service.set_device_enabled(10, False)
    with pytest.raises(device_service.DeviceRuntimeError) as disabled_exc:
        await device_service.authenticate_device_token(rotated.token)
    assert disabled_exc.value.status_code == 403

    with pytest.raises(device_service.DeviceRuntimeError) as invalid_exc:
        await device_service.authenticate_device_token("avd_invalid-token")
    assert invalid_exc.value.status_code == 401


@pytest.mark.asyncio
async def test_external_device_auth_rejects_disabled_owner_for_me(external_devices_db):
    token = await _seed_device(
        external_devices_db,
        device_id=10,
        user_id=1,
        slug="cam-disabled-owner",
    )
    async with aiosqlite.connect(external_devices_db) as conn:
        await conn.execute("UPDATE USERS SET is_enabled = 0 WHERE id = 1")
        await conn.commit()

    with pytest.raises(device_service.DeviceRuntimeError) as exc:
        await device_service.authenticate_device_token(token)
    assert exc.value.code == "owner_disabled"
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_external_device_idempotency_replays_completed_reply(external_devices_db):
    await _seed_device(external_devices_db, device_id=10, user_id=1, slug="cam-dedupe")
    reservation = await device_service.reserve_incoming_message_event(
        device_id=10,
        conversation_id=1,
        external_message_id="msg-1",
        metadata={"source": "test"},
        text="hello",
    )
    assert reservation["state"] == "reserved"

    duplicate_processing = await device_service.reserve_incoming_message_event(
        device_id=10,
        conversation_id=1,
        external_message_id="msg-1",
        metadata={},
        text="hello again",
    )
    assert duplicate_processing["state"] == "duplicate_processing"

    await device_service.complete_incoming_message_event(
        event_id=reservation["event_id"],
        reply="cached reply",
        actions=[],
        latency_ms=42,
    )
    duplicate_completed = await device_service.reserve_incoming_message_event(
        device_id=10,
        conversation_id=1,
        external_message_id="msg-1",
        metadata={},
        text="hello again",
    )
    assert duplicate_completed["state"] == "duplicate_completed"
    assert duplicate_completed["reply"] == "cached reply"


@pytest.mark.asyncio
async def test_external_device_stale_processing_message_can_be_reclaimed(external_devices_db):
    await _seed_device(external_devices_db, device_id=10, user_id=1, slug="cam-stale")
    reservation = await device_service.reserve_incoming_message_event(
        device_id=10,
        conversation_id=1,
        external_message_id="msg-stale",
        metadata={},
        text="hello",
    )

    async with aiosqlite.connect(external_devices_db) as conn:
        await conn.execute(
            """
            UPDATE EXTERNAL_DEVICE_EVENTS
            SET created_at = datetime('now', '-30 minutes')
            WHERE id = ?
            """,
            (reservation["event_id"],),
        )
        await conn.commit()

    reclaimed = await device_service.reserve_incoming_message_event(
        device_id=10,
        conversation_id=1,
        external_message_id="msg-stale",
        metadata={"retry": True},
        text="hello again",
    )

    assert reclaimed["state"] == "reserved"
    assert reclaimed["event_id"] == reservation["event_id"]


@pytest.mark.asyncio
async def test_external_device_turn_lock_is_removed_after_release():
    device_service._device_turn_locks.clear()
    lock = await device_service.acquire_device_turn_lock(1234)
    assert lock is not None
    assert 1234 in device_service._device_turn_locks

    await device_service.release_device_turn_lock(1234, lock)

    assert 1234 not in device_service._device_turn_locks


@pytest.mark.asyncio
async def test_external_device_runtime_dedupe_does_not_relaunch_llm(external_devices_db, monkeypatch):
    token = await _seed_device(external_devices_db, device_id=10, user_id=1, slug="cam-runtime")
    async with aiosqlite.connect(external_devices_db) as conn:
        await conn.execute(
            """
            INSERT INTO EXTERNAL_DEVICE_BINDINGS
                (user_id, target_type, target_id, conversation_id, response_mode)
            VALUES (1, 'device', 10, 1, 'text')
            """
        )
        await conn.commit()

    calls = []

    async def fake_process_save_message(**kwargs):
        calls.append(kwargs["text_plain"])

        async def body():
            yield b'data: {"content":"hello device"}\n\n'

        return StreamingResponse(body(), media_type="text/event-stream")

    async def fake_get_user_by_id(user_id):
        return DummyOwner()

    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(device_service, "process_save_message", fake_process_save_message)
    monkeypatch.setattr(device_service, "get_user_by_id", fake_get_user_by_id)
    monkeypatch.setattr(device_service, "_assert_device_rate_limits", noop)
    monkeypatch.setattr(device_service, "get_active_pause", noop)
    monkeypatch.setattr(device_service, "increment_metric", noop)
    monkeypatch.setattr(device_service, "increment_user_activity", noop)

    device = await device_service.authenticate_device_token(token)
    first = await device_service.handle_device_text_message(
        request=object(),
        device=device,
        message_id="runtime-1",
        text="hola",
        metadata={},
    )
    second = await device_service.handle_device_text_message(
        request=object(),
        device=device,
        message_id="runtime-1",
        text="hola again",
        metadata={},
    )

    assert first["reply"] == "hello device"
    assert second["duplicate"] is True
    assert second["reply"] == "hello device"
    assert len(calls) == 1
    assert calls[0].startswith("[External device: Cam Runtime; slug: cam-runtime;")


@pytest.mark.asyncio
async def test_external_device_runtime_failure_retries_as_duplicate_failed(
    external_devices_db,
    monkeypatch,
):
    token = await _seed_device(external_devices_db, device_id=10, user_id=1, slug="cam-fail")
    await _bind_device_to_conversation(external_devices_db, device_id=10)

    async def fake_process_save_message(**kwargs):
        raise RuntimeError("provider down")

    async def fake_get_user_by_id(user_id):
        return DummyOwner()

    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(device_service, "process_save_message", fake_process_save_message)
    monkeypatch.setattr(device_service, "get_user_by_id", fake_get_user_by_id)
    monkeypatch.setattr(device_service, "_assert_device_rate_limits", noop)
    monkeypatch.setattr(device_service, "get_active_pause", noop)

    device = await device_service.authenticate_device_token(token)
    with pytest.raises(device_service.DeviceRuntimeError) as runtime_exc:
        await device_service.handle_device_text_message(
            request=object(),
            device=device,
            message_id="runtime-failed",
            text="hola",
            metadata={},
        )
    assert runtime_exc.value.code == "runtime_error"
    assert runtime_exc.value.status_code == 502

    with pytest.raises(device_service.DeviceRuntimeError) as retry_exc:
        await device_service.handle_device_text_message(
            request=object(),
            device=device,
            message_id="runtime-failed",
            text="hola again",
            metadata={},
        )
    assert retry_exc.value.code == "duplicate_failed"
    assert retry_exc.value.status_code == 409


@pytest.mark.asyncio
async def test_external_device_post_completion_bookkeeping_failure_keeps_success(
    external_devices_db,
    monkeypatch,
):
    token = await _seed_device(external_devices_db, device_id=10, user_id=1, slug="cam-bookkeeping")
    await _bind_device_to_conversation(external_devices_db, device_id=10)

    async def fake_process_save_message(**kwargs):
        async def body():
            yield b'data: {"content":"done"}\n\n'

        return StreamingResponse(body(), media_type="text/event-stream")

    async def fake_get_user_by_id(user_id):
        return DummyOwner()

    async def noop(*args, **kwargs):
        return None

    async def fail_outgoing_event(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(device_service, "process_save_message", fake_process_save_message)
    monkeypatch.setattr(device_service, "get_user_by_id", fake_get_user_by_id)
    monkeypatch.setattr(device_service, "_assert_device_rate_limits", noop)
    monkeypatch.setattr(device_service, "get_active_pause", noop)
    monkeypatch.setattr(device_service, "increment_metric", noop)
    monkeypatch.setattr(device_service, "increment_user_activity", noop)
    monkeypatch.setattr(device_service, "record_outgoing_reply_event", fail_outgoing_event)

    device = await device_service.authenticate_device_token(token)
    result = await device_service.handle_device_text_message(
        request=object(),
        device=device,
        message_id="bookkeeping-1",
        text="hola",
        metadata={},
    )

    assert result["success"] is True
    assert result["reply"] == "done"
    async with aiosqlite.connect(external_devices_db) as conn:
        row = await (
            await conn.execute(
                """
                SELECT status, details_json
                FROM EXTERNAL_DEVICE_EVENTS
                WHERE device_id = 10 AND external_message_id = 'bookkeeping-1'
                """
            )
        ).fetchone()
    assert row[0] == "completed"
    assert json.loads(row[1])["reply"] == "done"


@pytest.mark.asyncio
async def test_external_device_rate_pause_and_owner_disabled_errors(
    external_devices_db,
    monkeypatch,
):
    rate_token = await _seed_device(external_devices_db, device_id=10, user_id=1, slug="cam-rate")
    pause_token = await _seed_device(external_devices_db, device_id=11, user_id=1, slug="cam-pause")
    owner_token = await _seed_device(external_devices_db, device_id=12, user_id=1, slug="cam-owner")
    await _bind_device_to_conversation(external_devices_db, device_id=10)
    await _bind_device_to_conversation(external_devices_db, device_id=11)
    await _bind_device_to_conversation(external_devices_db, device_id=12)

    async def fake_process_save_message(**kwargs):
        raise AssertionError("runtime should not be called")

    async def noop(*args, **kwargs):
        return None

    async def rate_limited(device):
        raise device_service.DeviceRuntimeError("rate_limited", "Too many requests", 429)

    async def paused(user_id):
        return {"message": "Pause active"}

    class DisabledOwner(DummyOwner):
        is_enabled = False

    async def disabled_owner(user_id):
        return DisabledOwner()

    monkeypatch.setattr(device_service, "process_save_message", fake_process_save_message)
    monkeypatch.setattr(device_service, "increment_metric", noop)
    monkeypatch.setattr(device_service, "increment_user_activity", noop)

    rate_device = await device_service.authenticate_device_token(rate_token)
    monkeypatch.setattr(device_service, "_assert_device_rate_limits", rate_limited)
    monkeypatch.setattr(device_service, "get_active_pause", noop)
    with pytest.raises(device_service.DeviceRuntimeError) as rate_exc:
        await device_service.handle_device_text_message(
            request=object(),
            device=rate_device,
            message_id="rate-1",
            text="hola",
            metadata={},
        )
    assert rate_exc.value.code == "rate_limited"
    assert rate_exc.value.status_code == 429

    pause_device = await device_service.authenticate_device_token(pause_token)
    monkeypatch.setattr(device_service, "_assert_device_rate_limits", noop)
    monkeypatch.setattr(device_service, "get_active_pause", paused)
    with pytest.raises(device_service.DeviceRuntimeError) as pause_exc:
        await device_service.handle_device_text_message(
            request=object(),
            device=pause_device,
            message_id="pause-1",
            text="hola",
            metadata={},
        )
    assert pause_exc.value.code == "wellbeing_pause_active"
    assert pause_exc.value.status_code == 429

    owner_device = await device_service.authenticate_device_token(owner_token)
    monkeypatch.setattr(device_service, "_assert_device_rate_limits", noop)
    monkeypatch.setattr(device_service, "get_active_pause", noop)
    monkeypatch.setattr(device_service, "get_user_by_id", disabled_owner)
    with pytest.raises(device_service.DeviceRuntimeError) as owner_exc:
        await device_service.handle_device_text_message(
            request=object(),
            device=owner_device,
            message_id="owner-1",
            text="hola",
            metadata={},
        )
    assert owner_exc.value.code == "owner_disabled"
    assert owner_exc.value.status_code == 403


@pytest.mark.asyncio
async def test_external_device_runtime_accepts_snapshot_and_structured_actions(
    external_devices_db,
    monkeypatch,
):
    token = await _seed_device(
        external_devices_db,
        device_id=10,
        user_id=1,
        slug="cam-snapshot",
        capabilities={"speak": True, "snapshot": True},
    )
    async with aiosqlite.connect(external_devices_db) as conn:
        await conn.execute(
            """
            INSERT INTO EXTERNAL_DEVICE_BINDINGS
                (user_id, target_type, target_id, conversation_id, response_mode)
            VALUES (1, 'device', 10, 1, 'text')
            """
        )
        await conn.commit()

    seen = {}

    async def fake_process_save_message(**kwargs):
        seen["files"] = kwargs["files"]
        seen["text_plain"] = kwargs["text_plain"]

        async def body():
            yield (
                b'data: {"content":"I can see the snapshot. '
                b'[AURVEK_ACTIONS] [{\\"type\\":\\"speak\\",\\"text\\":\\"Done\\"},'
                b'{\\"type\\":\\"snapshot\\",\\"reason\\":\\"Need a fresh frame\\"},'
                b'{\\"type\\":\\"command\\",\\"command\\":\\"rm -rf /\\"}] [/AURVEK_ACTIONS]"}\n\n'
            )

        return StreamingResponse(body(), media_type="text/event-stream")

    async def fake_get_user_by_id(user_id):
        return DummyOwner()

    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(device_service, "process_save_message", fake_process_save_message)
    monkeypatch.setattr(device_service, "get_user_by_id", fake_get_user_by_id)
    monkeypatch.setattr(device_service, "_assert_device_rate_limits", noop)
    monkeypatch.setattr(device_service, "get_active_pause", noop)
    monkeypatch.setattr(device_service, "increment_metric", noop)
    monkeypatch.setattr(device_service, "increment_user_activity", noop)

    snapshot_bytes = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
    )
    device = await device_service.authenticate_device_token(token)
    result = await device_service.handle_device_text_message(
        request=object(),
        device=device,
        message_id="snapshot-1",
        text="what changed?",
        metadata={"source": "test"},
        snapshot={
            "data_base64": "data:image/png;base64," + base64.b64encode(snapshot_bytes).decode("ascii"),
            "filename": "../living room.png",
        },
    )

    assert result["reply"] == "I can see the snapshot."
    assert result["actions"] == [
        {"type": "speak", "text": "Done"},
        {"type": "snapshot", "reason": "Need a fresh frame"},
    ]
    assert seen["files"][0]["content_type"].startswith("image/")
    assert seen["files"][0]["filename"] == "living-room.png"
    assert seen["files"][0]["data"]
    assert "allowed action block" in seen["text_plain"]

    duplicate = await device_service.handle_device_text_message(
        request=object(),
        device=device,
        message_id="snapshot-1",
        text="what changed?",
        metadata={"source": "test"},
        snapshot={
            "data_base64": "data:image/png;base64," + base64.b64encode(b"not-an-image").decode("ascii"),
            "filename": "broken.png",
        },
    )
    assert duplicate["duplicate"] is True
    assert duplicate["actions"] == result["actions"]


@pytest.mark.asyncio
async def test_external_device_runtime_rejects_invalid_metadata_and_snapshot(
    external_devices_db,
    monkeypatch,
):
    async def noop(*args, **kwargs):
        return None

    async def owner(_user_id):
        return DummyOwner()

    monkeypatch.setattr(device_service, "_assert_device_rate_limits", noop)
    monkeypatch.setattr(device_service, "get_active_pause", noop)
    monkeypatch.setattr(device_service, "get_user_by_id", owner)

    token = await _seed_device(external_devices_db, device_id=10, user_id=1, slug="cam-invalid")
    async with aiosqlite.connect(external_devices_db) as conn:
        await conn.execute(
            """
            INSERT INTO EXTERNAL_DEVICE_BINDINGS
                (user_id, target_type, target_id, conversation_id, response_mode)
            VALUES (1, 'device', 10, 1, 'text')
            """
        )
        await conn.commit()

    device = await device_service.authenticate_device_token(token)
    with pytest.raises(device_service.DeviceRuntimeError) as metadata_exc:
        await device_service.handle_device_text_message(
            request=object(),
            device=device,
            message_id="bad-metadata",
            text="hello",
            metadata=False,
        )
    assert metadata_exc.value.code == "invalid_request"

    with pytest.raises(device_service.DeviceRuntimeError) as snapshot_exc:
        await device_service.handle_device_text_message(
            request=object(),
            device=device,
            message_id="bad-snapshot",
            text="hello",
            metadata={},
            snapshot={"data_base64": base64.b64encode(b"not-image").decode("ascii"), "mime_type": "text/plain"},
        )
    assert snapshot_exc.value.code == "invalid_request"

    snapshot_token = await _seed_device(
        external_devices_db,
        device_id=11,
        user_id=1,
        slug="cam-invalid-snapshot",
        capabilities={"snapshot": True},
    )
    async with aiosqlite.connect(external_devices_db) as conn:
        await conn.execute(
            """
            INSERT INTO EXTERNAL_DEVICE_BINDINGS
                (user_id, target_type, target_id, conversation_id, response_mode)
            VALUES (1, 'device', 11, 1, 'text')
            """
        )
        await conn.commit()
    snapshot_device = await device_service.authenticate_device_token(snapshot_token)
    with pytest.raises(device_service.DeviceRuntimeError) as invalid_image_exc:
        await device_service.handle_device_text_message(
            request=object(),
            device=snapshot_device,
            message_id="bad-image",
            text="hello",
            metadata={},
            snapshot={
                "data_base64": "data:image/png;base64," + base64.b64encode(b"not-image").decode("ascii"),
                "filename": "broken.png",
            },
        )
    assert invalid_image_exc.value.code == "invalid_request"


@pytest.mark.asyncio
async def test_external_device_action_block_can_be_sanitized_from_saved_reply(external_devices_db):
    raw_reply = (
        'Done [AURVEK_ACTIONS] [{"type":"speak","text":"Done"},'
        '{"type":"command","command":"rm -rf /"}] [/AURVEK_ACTIONS]'
    )
    clean_reply, actions = device_service.extract_structured_actions(
        raw_reply,
        {"speak": True},
    )
    assert clean_reply == "Done"
    assert actions == [{"type": "speak", "text": "Done"}]

    async with aiosqlite.connect(external_devices_db) as conn:
        await conn.execute(
            """
            INSERT INTO MESSAGES (conversation_id, user_id, message, type, date)
            VALUES (1, 1, ?, 'bot', CURRENT_TIMESTAMP)
            """,
            (raw_reply,),
        )
        await conn.commit()

    await device_service.sanitize_last_device_reply_in_db(
        conversation_id=1,
        raw_reply=raw_reply,
        clean_reply=clean_reply,
    )

    async with aiosqlite.connect(external_devices_db) as conn:
        row = await (await conn.execute("SELECT message FROM MESSAGES WHERE type = 'bot'")).fetchone()
    assert row[0] == "Done"


@pytest.mark.asyncio
async def test_external_device_binding_updates_enforce_ownership_and_classic_platform_split(
    external_devices_db,
    monkeypatch,
):
    await _seed_device(external_devices_db, device_id=10, user_id=1, slug="alice-cam")
    await _seed_device(external_devices_db, device_id=20, user_id=2, slug="bob-cam")

    with pytest.raises(device_service.DeviceValidationError):
        await device_service.update_conversation_external_bindings(
            user_id=1,
            conversation_id=1,
            device_ids=[20],
            group_ids=[],
        )
    with pytest.raises(device_service.DeviceValidationError):
        await device_service.update_conversation_external_bindings(
            user_id=1,
            conversation_id=1,
            device_ids=False,
            group_ids=[],
        )

    updated = await device_service.update_conversation_external_bindings(
        user_id=1,
        conversation_id=1,
        device_ids=[10],
        group_ids=[],
    )
    assert updated["external_bindings"]["effective_count"] == 1

    platform_result = await platform_conversations.set_external_conversation(
        1,
        1,
        "whatsapp",
        "whatsapp",
    )
    assert platform_result["success"] is False
    assert platform_result["error"] == "external_devices_attached"

    async with aiosqlite.connect(external_devices_db) as conn:
        await conn.execute(
            "UPDATE USER_DETAILS SET external_platforms = ? WHERE user_id = 1",
            ('{"whatsapp":{"conversation_id":"3"}}',),
        )
        await conn.commit()

    classic = await device_service.get_conversation_external_bindings(
        user_id=1,
        conversation_id=3,
    )
    assert classic["assignable"] is False
    with pytest.raises(device_service.DeviceValidationError):
        await device_service.update_conversation_external_bindings(
            user_id=1,
            conversation_id=3,
            device_ids=[10],
            group_ids=[],
        )
    with pytest.raises(device_service.DeviceValidationError):
        await device_service.set_binding(
            target_type="device",
            target_id=10,
            conversation_id=3,
            response_mode="text",
        )

    async def fake_get_user_by_id(user_id):
        return DummyOwner()

    monkeypatch.setattr(device_service, "get_user_by_id", fake_get_user_by_id)
    with pytest.raises(device_service.DeviceValidationError):
        await device_service.create_device(
            owner_user_id=1,
            display_name="Classic Cam",
            slug="classic-cam",
            device_type="custom",
            notes="",
            capability_names=[],
            group_ids=[],
            conversation_id=3,
        )


@pytest.mark.asyncio
async def test_external_device_admin_overview_shows_recent_events(external_devices_db):
    token = await _seed_device(external_devices_db, device_id=10, user_id=1, slug="cam-events")
    device = await device_service.authenticate_device_token(token)
    with pytest.raises(device_service.DeviceRuntimeError) as setup_exc:
        await device_service.handle_device_text_message(
            request=object(),
            device=device,
            message_id="setup-missing",
            text="ping",
            metadata={},
        )
    assert setup_exc.value.code == "setup_required"

    await device_service.reserve_incoming_message_event(
        device_id=10,
        conversation_id=1,
        external_message_id="event-1",
        metadata={},
        text="ping",
    )

    overview = await device_service.get_admin_overview()
    assert overview.messages_today == 1
    assert overview.recent_events
    assert any(event["device_slug"] == "cam-events" for event in overview.recent_events)
    assert any(
        event["direction"] == "system"
        and event["event_type"] == "routing"
        and event["status"] == "setup_required"
        for event in overview.recent_events
    )


@pytest.mark.asyncio
async def test_external_device_event_retention_keeps_processing_events(external_devices_db, monkeypatch):
    await _seed_device(external_devices_db, device_id=10, user_id=1, slug="cam-retention")
    monkeypatch.setattr(device_service, "DEVICE_EVENT_RETENTION_DAYS", 30)
    async with aiosqlite.connect(external_devices_db) as conn:
        await conn.executescript(
            """
            INSERT INTO EXTERNAL_DEVICE_EVENTS
                (device_id, conversation_id, external_message_id, direction, event_type, status, created_at)
            VALUES
                (10, 1, 'old-completed', 'in', 'message', 'completed', datetime('now', '-60 days')),
                (10, 1, 'old-processing', 'in', 'message', 'processing', datetime('now', '-60 days'));
            """
        )
        await conn.commit()

    deleted = await device_service.prune_old_device_events(force=True)

    assert deleted == 1
    async with aiosqlite.connect(external_devices_db) as conn:
        rows = await (
            await conn.execute(
                "SELECT external_message_id FROM EXTERNAL_DEVICE_EVENTS ORDER BY external_message_id"
            )
        ).fetchall()
    assert [row[0] for row in rows] == ["old-processing"]


def test_external_devices_migration_is_idempotent(tmp_path):
    db_path = tmp_path / "migration.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE USERS (id INTEGER PRIMARY KEY);
        CREATE TABLE CONVERSATIONS (id INTEGER PRIMARY KEY);
        """
    )
    _create_schema(conn)
    _create_schema(conn)

    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    indexes = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        ).fetchall()
    }
    conn.close()
    assert "EXTERNAL_DEVICES" in tables
    assert "EXTERNAL_DEVICE_EVENTS" in tables
    assert "idx_external_device_events_created_id" in indexes
    assert "idx_external_device_events_type_direction_created" in indexes


def test_aurvek_action_blocks_are_stripped_before_persistence():
    raw = (
        'Done [AURVEK_ACTIONS] [{"type":"speak","text":"Done"},'
        '{"type":"command","command":"rm -rf /"}] [/AURVEK_ACTIONS] '
    )
    assert strip_aurvek_action_blocks(raw) == "Done"
    assert strip_aurvek_action_blocks("[AURVEK_ACTIONS] [] [/AURVEK_ACTIONS]") == "[Structured device action returned]"
    assert assistant_content_for_storage(raw) == raw
    assert assistant_content_for_storage(raw, strip_device_action_blocks=True) == "Done"


@pytest.mark.asyncio
async def test_device_action_strip_flag_survives_tool_error_second_pass(monkeypatch):
    captured_kwargs = {}

    async def failing_tool_handler(*args, **kwargs):
        yield 'data: {"content":"tool failed","is_error":true}\n\n'

    async def fake_kimi_provider(**kwargs):
        captured_kwargs.update(kwargs)
        yield 'data: {"content":"recovered"}\n\n'

    monkeypatch.setitem(tool_execution.function_handlers, "failing_tool", failing_tool_handler)
    monkeypatch.setattr(tool_execution, "call_kimi_api", fake_kimi_provider)

    chunks = [
        chunk
        async for chunk in tool_execution.handle_function_call(
            "failing_tool",
            {},
            [],
            "kimi-k2",
            0.7,
            100,
            "",
            1,
            DummyOwner(),
            object(),
            1,
            1,
            2,
            None,
            1,
            "Kimi",
            "system prompt",
            user_message="hello",
            strip_device_action_blocks=True,
        )
    ]

    assert chunks == ['data: {"content":"recovered"}\n\n']
    assert captured_kwargs["strip_device_action_blocks"] is True


@pytest.mark.asyncio
async def test_device_action_strip_flag_survives_watchdog_takeover(monkeypatch):
    captured_kwargs = {}

    async def fake_get_llm_info(llm_id):
        return {
            "machine": "Kimi",
            "model": "kimi-k2",
            "max_output_tokens": 100,
            "input_token_cost": 0,
            "output_token_cost": 0,
        }

    async def fake_get_effective_blocks():
        return []

    async def fake_format_messages(*args, **kwargs):
        return []

    async def fake_get_user_api_key_mode(user_id):
        return "system"

    def fake_resolve_api_key_for_provider(user_api_keys, api_key_mode, machine):
        return ("key", False)

    async def fake_kimi_provider(**kwargs):
        captured_kwargs.update(kwargs)
        yield 'data: {"content":"takeover"}\n\n'

    async def fake_finalize_takeover(*args, **kwargs):
        return None

    monkeypatch.setattr(watchdog_takeover, "get_llm_info", fake_get_llm_info)
    monkeypatch.setattr(watchdog_takeover, "get_effective_blocks", fake_get_effective_blocks)
    monkeypatch.setattr(watchdog_takeover, "_format_messages_for_provider", fake_format_messages)
    monkeypatch.setattr(watchdog_takeover, "get_user_api_key_mode", fake_get_user_api_key_mode)
    monkeypatch.setattr(watchdog_takeover, "resolve_api_key_for_provider", fake_resolve_api_key_for_provider)
    monkeypatch.setattr(watchdog_takeover, "assert_billable_claude_system_key", lambda **kwargs: None)
    monkeypatch.setattr(watchdog_takeover, "call_kimi_api", fake_kimi_provider)
    monkeypatch.setattr("tools.watchdog._finalize_takeover", fake_finalize_takeover)

    chunks = [
        chunk
        async for chunk in watchdog_takeover.watchdog_takeover_response(
            conversation_id=1,
            prompt_id=1,
            user_id=1,
            watchdog_config={"llm_id": 99},
            original_prompt="original",
            directive="redirect",
            context_messages=[],
            user_message="hello",
            message="hello",
            should_lock=False,
            current_user=DummyOwner(),
            request=object(),
            user_api_keys={},
            machine="Kimi",
            model="kimi-k2",
            strip_device_action_blocks=True,
        )
    ]

    assert chunks == ['data: {"content":"takeover"}\n\n']
    assert captured_kwargs["strip_device_action_blocks"] is True


def test_device_routes_return_contract_errors_for_bad_bearer(external_devices_db):
    client = _devices_test_client()

    missing = client.get("/api/devices/me")
    assert missing.status_code == 401
    assert missing.json() == {
        "success": False,
        "error": "unauthorized",
        "message": "Invalid device token",
    }

    corrupt = client.post(
        "/api/devices/messages",
        headers={"Authorization": "Basic avd_invalid"},
        json={"message_id": "m1", "text": "hello"},
    )
    assert corrupt.status_code == 401
    assert corrupt.json()["error"] == "unauthorized"


def test_device_message_route_uses_runtime_error_envelope(monkeypatch):
    async def fake_authenticate(token):
        return device_service.AuthenticatedDevice(
            id=10,
            user_id=1,
            slug="cam-route",
            display_name="Cam Route",
            device_type="custom",
            enabled=True,
            capabilities={},
            metadata={},
        )

    async def fake_handle_message(**kwargs):
        raise device_service.DeviceRuntimeError("runtime_error", "Provider failed", 502)

    monkeypatch.setattr(device_routes, "authenticate_device_token", fake_authenticate)
    monkeypatch.setattr(device_routes, "handle_device_text_message", fake_handle_message)
    client = _devices_test_client()

    response = client.post(
        "/api/devices/messages",
        headers={"Authorization": "Bearer avd_route"},
        json={"message_id": "route-1", "text": "hello"},
    )

    assert response.status_code == 502
    assert response.json() == {
        "success": False,
        "error": "runtime_error",
        "message": "Provider failed",
    }


def test_external_binding_and_admin_routes_enforce_auth_gates(external_devices_db):
    anon_client = _devices_test_client(current_user=None)
    bindings = anon_client.get("/api/conversations/1/external-bindings")
    assert bindings.status_code == 401

    admin_client = _devices_test_client(current_user=DummyOwner())
    admin = admin_client.get("/admin/devices")
    assert admin.status_code == 403


@pytest.mark.asyncio
async def test_external_binding_route_returns_structured_forbidden_status(external_devices_db):
    await _seed_device(external_devices_db, device_id=20, user_id=2, slug="bob-cam")
    client = _devices_test_client(current_user=DummyOwner())

    response = client.post(
        "/api/conversations/1/external-bindings",
        json={"device_ids": [20], "group_ids": []},
    )

    assert response.status_code == 403
    assert response.json()["success"] is False
    assert response.json()["error"] == "external_bindings_error"


@pytest.mark.asyncio
async def test_device_message_json_reader_enforces_body_limit(monkeypatch):
    class FakeRequest:
        def __init__(self, headers, chunks):
            self.headers = headers
            self._chunks = chunks

        async def stream(self):
            for chunk in self._chunks:
                yield chunk

    monkeypatch.setattr(device_routes, "MAX_DEVICE_MESSAGE_BODY_BYTES", 16)

    with pytest.raises(device_service.DeviceRuntimeError) as header_exc:
        await device_routes._read_device_json_payload(
            FakeRequest({"content-length": "17"}, [b'{"ok":true}'])
        )
    assert header_exc.value.code == "payload_too_large"
    assert header_exc.value.status_code == 413

    with pytest.raises(device_service.DeviceRuntimeError) as stream_exc:
        await device_routes._read_device_json_payload(
            FakeRequest({}, [b'{"text":"', b"x" * 32, b'"}'])
        )
    assert stream_exc.value.code == "payload_too_large"
    assert stream_exc.value.status_code == 413

    payload = await device_routes._read_device_json_payload(
        FakeRequest({"content-length": "7"}, [b'{"a":1}'])
    )
    assert payload == {"a": 1}
