"""Conversation privacy helpers for local Aurvek chat state."""

from __future__ import annotations

from typing import Any

import aiosqlite

import database
from log_config import logger


_PRIVACY_COLUMNS: dict[str, str] = {
    "is_incognito": (
        "is_incognito INTEGER NOT NULL DEFAULT 0 "
        "CHECK(is_incognito IN (0, 1))"
    ),
    "hidden_from_history": (
        "hidden_from_history INTEGER NOT NULL DEFAULT 0 "
        "CHECK(hidden_from_history IN (0, 1))"
    ),
    "purge_on_close": (
        "purge_on_close INTEGER NOT NULL DEFAULT 0 "
        "CHECK(purge_on_close IN (0, 1))"
    ),
    "incognito_closed_at": "incognito_closed_at TEXT",
}


async def ensure_conversation_privacy_schema(
    conn: aiosqlite.Connection | None = None,
) -> None:
    """Add conversation privacy columns idempotently."""
    if conn is None:
        async with database.get_db_connection() as owned_conn:
            await _ensure_schema_on_connection(owned_conn)
            await owned_conn.commit()
        return

    await _ensure_schema_on_connection(conn)


async def mark_conversation_incognito(
    conn: aiosqlite.Connection,
    *,
    conversation_id: int,
    user_id: int,
    incognito: bool,
) -> bool:
    """Set local incognito/history flags for one conversation."""
    await ensure_conversation_privacy_schema(conn)
    value = 1 if incognito else 0
    cursor = await conn.execute(
        """
        UPDATE CONVERSATIONS
        SET is_incognito = ?,
            hidden_from_history = ?,
            purge_on_close = ?
        WHERE id = ?
          AND user_id = ?
        """,
        (value, value, value, conversation_id, user_id),
    )
    return bool(cursor.rowcount)


async def get_conversation_privacy(
    conversation_id: int,
    *,
    user_id: int | None = None,
) -> dict[str, Any] | None:
    """Return local privacy flags and prompt ownership for a conversation."""
    await ensure_conversation_privacy_schema()
    async with database.get_db_connection(readonly=True) as conn:
        conn.row_factory = aiosqlite.Row
        params: list[Any] = [conversation_id]
        user_filter = ""
        if user_id is not None:
            user_filter = " AND user_id = ?"
            params.append(user_id)
        cursor = await conn.execute(
            f"""
            SELECT id, user_id, role_id,
                   COALESCE(is_incognito, 0) AS is_incognito,
                   COALESCE(hidden_from_history, 0) AS hidden_from_history,
                   COALESCE(purge_on_close, 0) AS purge_on_close,
                   incognito_closed_at
            FROM CONVERSATIONS
            WHERE id = ?{user_filter}
            """,
            params,
        )
        row = await cursor.fetchone()
    return dict(row) if row else None


async def is_incognito_conversation(
    conversation_id: int,
    *,
    user_id: int | None = None,
) -> bool:
    row = await get_conversation_privacy(conversation_id, user_id=user_id)
    return bool(row and row.get("is_incognito"))


async def purge_conversation_local_records(
    *,
    conversation_id: int,
    user_id: int,
) -> bool:
    """Delete local records for an incognito conversation after close."""
    await ensure_conversation_privacy_schema()
    async with database.get_db_connection() as conn:
        await ensure_conversation_privacy_schema(conn)
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            """
            SELECT id, user_id, COALESCE(is_incognito, 0) AS is_incognito
            FROM CONVERSATIONS
            WHERE id = ?
              AND user_id = ?
            """,
            (conversation_id, user_id),
        )
        row = await cursor.fetchone()
        if row is None:
            await conn.commit()
            return False
        if not bool(row["is_incognito"]):
            await conn.commit()
            raise ValueError("Conversation is not incognito")

        await delete_conversation_rows(conn, conversation_id=conversation_id, user_id=user_id)
        await conn.commit()
        return True


async def delete_conversation_rows(
    conn: aiosqlite.Connection,
    *,
    conversation_id: int,
    user_id: int | None = None,
) -> None:
    """Delete local rows owned by one conversation, preserving caller transaction."""
    message_ids: list[int] = []
    cursor = await conn.execute(
        "SELECT id FROM MESSAGES WHERE conversation_id = ?",
        (conversation_id,),
    )
    rows = await cursor.fetchall()
    message_ids = [int(row[0]) for row in rows]

    if message_ids and await _table_exists(conn, "ATAGIA_MESSAGE_LINKS"):
        placeholders = ",".join("?" for _ in message_ids)
        await conn.execute(
            f"DELETE FROM ATAGIA_MESSAGE_LINKS WHERE message_id IN ({placeholders})",
            message_ids,
        )

    try:
        from file_storage import delete_attachments_for_conversation

        await delete_attachments_for_conversation(
            conn,
            conversation_id=conversation_id,
        )
    except Exception:
        logger.exception(
            "Failed to delete file attachments for conversation_id=%s",
            conversation_id,
        )
        raise

    await conn.execute(
        "DELETE FROM WATCHDOG_STATE WHERE conversation_id = ?",
        (conversation_id,),
    )
    await conn.execute(
        "DELETE FROM WATCHDOG_EVENTS WHERE conversation_id = ?",
        (conversation_id,),
    )
    await conn.execute(
        "DELETE FROM messages WHERE conversation_id = ?",
        (conversation_id,),
    )

    if user_id is None:
        await conn.execute(
            "DELETE FROM conversations WHERE id = ?",
            (conversation_id,),
        )
    else:
        await conn.execute(
            "DELETE FROM conversations WHERE id = ? AND user_id = ?",
            (conversation_id, user_id),
        )


async def _ensure_schema_on_connection(conn: aiosqlite.Connection) -> None:
    try:
        cursor = await conn.execute("PRAGMA table_info(CONVERSATIONS)")
        columns = {str(row[1]) for row in await cursor.fetchall()}
        for name, definition in _PRIVACY_COLUMNS.items():
            if name not in columns:
                await conn.execute(
                    f"ALTER TABLE CONVERSATIONS ADD COLUMN {definition}"
                )
        if "folder_id" in columns and "last_activity" in columns:
            await conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_conversations_history_visible
                ON CONVERSATIONS(user_id, folder_id, hidden_from_history, last_activity, id)
                """
            )
        else:
            await conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_conversations_history_visible
                ON CONVERSATIONS(user_id, hidden_from_history, id)
                """
            )
    except Exception:
        logger.exception("Failed to ensure conversation privacy schema")
        raise


async def _table_exists(conn: aiosqlite.Connection, table_name: str) -> bool:
    cursor = await conn.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table'
          AND name = ?
        """,
        (table_name,),
    )
    return await cursor.fetchone() is not None
