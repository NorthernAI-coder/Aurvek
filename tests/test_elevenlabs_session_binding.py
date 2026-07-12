import asyncio
import sqlite3
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import aiosqlite
import pytest
import pytest_asyncio

from integrations.elevenlabs import service as service_module
from integrations.elevenlabs.service import (
    ElevenLabsProviderSessionError,
    ElevenLabsService,
    ElevenLabsSessionBindingError,
)
from migration_elevenlabs_call_sessions import migrate


BASE_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE USERS (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL
);

CREATE TABLE VOICES (
    id INTEGER PRIMARY KEY,
    voice_code TEXT
);

CREATE TABLE PROMPTS (
    id INTEGER PRIMARY KEY,
    name TEXT,
    prompt TEXT,
    description TEXT,
    voice_id INTEGER,
    watchdog_config TEXT
);

CREATE TABLE CONVERSATIONS (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    role_id INTEGER,
    chat_name TEXT,
    locked INTEGER DEFAULT 0,
    last_activity TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES USERS(id)
);

CREATE TABLE MESSAGES (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    message TEXT NOT NULL,
    type TEXT NOT NULL,
    date TIMESTAMP NOT NULL,
    FOREIGN KEY (conversation_id) REFERENCES CONVERSATIONS(id),
    FOREIGN KEY (user_id) REFERENCES USERS(id)
);

CREATE TABLE ELEVENLABS_AGENTS (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL UNIQUE,
    agent_name TEXT,
    is_default INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE PROMPT_AGENT_MAPPING (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    prompt_id INTEGER NOT NULL UNIQUE,
    agent_id TEXT NOT NULL,
    voice_id TEXT
);
"""


def _provider_details(
    session_id: str,
    conversation_id: int,
    user_id: int,
    agent_id: str = "agent-main",
):
    return {
        "conversation_id": session_id,
        "agent_id": agent_id,
        "status": "in-progress",
        "conversation_initiation_client_data": {
            "dynamic_variables": {
                "aurvek_conversation_id": str(conversation_id),
                "aurvek_user_id": str(user_id),
            }
        },
    }


@pytest_asyncio.fixture()
async def elevenlabs_db(tmp_path, monkeypatch):
    db_path = tmp_path / "elevenlabs.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(BASE_SCHEMA)
        conn.executemany(
            "INSERT INTO USERS (id, username) VALUES (?, ?)",
            [(1, "owner"), (2, "other")],
        )
        conn.execute(
            "INSERT INTO PROMPTS (id, name, prompt) VALUES (10, 'Coach', 'Help')"
        )
        conn.execute(
            """
            INSERT INTO ELEVENLABS_AGENTS (agent_id, agent_name, is_default)
            VALUES ('agent-main', 'Main', 1)
            """
        )
        conn.execute(
            """
            INSERT INTO PROMPT_AGENT_MAPPING (prompt_id, agent_id)
            VALUES (10, 'agent-main')
            """
        )
        conn.executemany(
            """
            INSERT INTO CONVERSATIONS (id, user_id, role_id, chat_name)
            VALUES (?, ?, 10, ?)
            """,
            [(100, 1, "First"), (101, 1, "Second"), (200, 2, "Other")],
        )
        conn.execute("PRAGMA journal_mode = WAL")
        conn.commit()

    migrate(str(db_path))

    @asynccontextmanager
    async def get_connection(readonly=False):
        conn = await aiosqlite.connect(str(db_path), timeout=5.0)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys = ON")
        await conn.execute("PRAGMA busy_timeout = 5000")
        try:
            yield conn
        finally:
            await conn.close()

    monkeypatch.setattr(service_module, "get_db_connection", get_connection)
    return db_path


def test_migration_backfills_legacy_session_and_is_idempotent(tmp_path):
    db_path = tmp_path / "legacy-elevenlabs.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(BASE_SCHEMA)
        conn.execute("ALTER TABLE CONVERSATIONS ADD COLUMN elevenlabs_session_id TEXT")
        conn.execute("ALTER TABLE CONVERSATIONS ADD COLUMN elevenlabs_status TEXT")
        conn.execute("INSERT INTO USERS (id, username) VALUES (1, 'owner')")
        conn.execute(
            "INSERT INTO PROMPTS (id, name, prompt) VALUES (10, 'Coach', 'Help')"
        )
        conn.execute(
            """
            INSERT INTO ELEVENLABS_AGENTS (agent_id, agent_name, is_default)
            VALUES ('agent-main', 'Main', 1)
            """
        )
        conn.execute(
            """
            INSERT INTO PROMPT_AGENT_MAPPING (prompt_id, agent_id)
            VALUES (10, 'agent-main')
            """
        )
        conn.execute(
            """
            INSERT INTO CONVERSATIONS (
                id, user_id, role_id, chat_name,
                elevenlabs_session_id, elevenlabs_status
            ) VALUES (100, 1, 10, 'Legacy', 'legacy-session', 'completed')
            """
        )

    first = migrate(str(db_path))
    second = migrate(str(db_path))

    assert first["call_sessions_table_created"] is True
    assert first["legacy_sessions_backfilled"] == 1
    assert second["call_sessions_table_created"] is False
    assert second["legacy_sessions_backfilled"] == 0
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT conversation_id, user_id, agent_id, status,
                   transcript_saved_at IS NOT NULL
            FROM ELEVENLABS_CALL_SESSIONS
            WHERE session_id = 'legacy-session'
            """
        ).fetchone()
    assert row == (100, 1, "agent-main", "completed", 1)


@pytest.mark.asyncio
async def test_session_registration_validates_provider_metadata_and_is_idempotent(
    elevenlabs_db,
    monkeypatch,
):
    service = ElevenLabsService()
    provider = AsyncMock(return_value=_provider_details("session-one", 100, 1))
    monkeypatch.setattr(service, "fetch_session_details", provider)

    assert await service.register_session(100, "session-one", 1) is True
    assert await service.register_session(100, "session-one", 1) is False

    with sqlite3.connect(elevenlabs_db) as conn:
        row = conn.execute(
            """
            SELECT conversation_id, user_id, agent_id, status
            FROM ELEVENLABS_CALL_SESSIONS
            WHERE session_id = 'session-one'
            """
        ).fetchone()
    assert row == (100, 1, "agent-main", "active")


@pytest.mark.asyncio
async def test_session_registration_rejects_provider_conversation_mismatch(
    elevenlabs_db,
    monkeypatch,
):
    service = ElevenLabsService()
    monkeypatch.setattr(
        service,
        "fetch_session_details",
        AsyncMock(return_value=_provider_details("session-one", 101, 1)),
    )

    with pytest.raises(ElevenLabsProviderSessionError):
        await service.register_session(100, "session-one", 1)

    with sqlite3.connect(elevenlabs_db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM ELEVENLABS_CALL_SESSIONS"
        ).fetchone()[0] == 0


@pytest.mark.asyncio
async def test_session_id_cannot_be_rebound_to_another_conversation(
    elevenlabs_db,
    monkeypatch,
):
    service = ElevenLabsService()
    provider = AsyncMock(return_value=_provider_details("shared-session", 100, 1))
    monkeypatch.setattr(service, "fetch_session_details", provider)
    await service.register_session(100, "shared-session", 1)

    provider.return_value = _provider_details("shared-session", 101, 1)
    with pytest.raises(ElevenLabsSessionBindingError):
        await service.register_session(101, "shared-session", 1)

    with sqlite3.connect(elevenlabs_db) as conn:
        rows = conn.execute(
            """
            SELECT session_id, conversation_id
            FROM ELEVENLABS_CALL_SESSIONS
            """
        ).fetchall()
    assert rows == [("shared-session", 100)]


@pytest.mark.asyncio
async def test_stop_and_lookup_reject_unrelated_or_premature_completion(
    elevenlabs_db,
    monkeypatch,
):
    service = ElevenLabsService()
    monkeypatch.setattr(
        service,
        "fetch_session_details",
        AsyncMock(return_value=_provider_details("session-one", 100, 1)),
    )
    await service.register_session(100, "session-one", 1)

    with pytest.raises(ElevenLabsSessionBindingError):
        await service.get_bound_session(100, "different-session", 1)
    with pytest.raises(ElevenLabsSessionBindingError):
        await service.mark_session_status(
            100,
            "session-one",
            "completed",
            1,
        )

    assert await service.mark_session_status(100, "session-one", "failed", 1)


@pytest.mark.asyncio
async def test_concurrent_transcript_retries_insert_each_turn_once(
    elevenlabs_db,
    monkeypatch,
):
    service = ElevenLabsService()
    monkeypatch.setattr(
        service,
        "fetch_session_details",
        AsyncMock(return_value=_provider_details("session-one", 100, 1)),
    )
    wellbeing_activity = AsyncMock()
    monkeypatch.setattr(
        service_module,
        "record_voice_transcript_activity",
        wellbeing_activity,
    )
    await service.register_session(100, "session-one", 1)

    transcript = [
        {"role": "user", "message": "Hello"},
        {"role": "agent", "message": "Hi there"},
    ]
    first, second = await asyncio.gather(
        service.save_transcript_to_db(100, "session-one", 1, transcript),
        service.save_transcript_to_db(100, "session-one", 1, transcript),
    )

    assert sorted([first[0], second[0]]) == [0, 2]
    assert sorted([first[3], second[3]]) == [False, True]
    assert wellbeing_activity.await_count == 1
    with sqlite3.connect(elevenlabs_db) as conn:
        messages = conn.execute(
            """
            SELECT message, type
            FROM MESSAGES
            WHERE conversation_id = 100
            ORDER BY id
            """
        ).fetchall()
        binding = conn.execute(
            """
            SELECT status, transcript_saved_at IS NOT NULL
            FROM ELEVENLABS_CALL_SESSIONS
            WHERE session_id = 'session-one'
            """
        ).fetchone()
    assert messages == [("Hello", "user"), ("Hi there", "bot")]
    assert binding == ("completed", 1)
