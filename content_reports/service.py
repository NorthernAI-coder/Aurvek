"""Persistence and validation for user content reports."""

from __future__ import annotations

import json
from typing import Any


VALID_REPORT_TARGET_TYPES = {"prompt", "pack", "conversation", "message"}
VALID_REPORT_REASONS = {
    "offensive",
    "harassment",
    "sexual_content",
    "violence",
    "self_harm",
    "illegal",
    "spam",
    "privacy",
    "copyright",
    "other",
}


async def ensure_content_reports_schema(conn) -> None:
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS CONTENT_REPORTS (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reporter_user_id INTEGER NOT NULL,
            target_type TEXT NOT NULL,
            target_id INTEGER NOT NULL,
            target_owner_user_id INTEGER,
            reason TEXT NOT NULL,
            details TEXT,
            status TEXT NOT NULL DEFAULT 'open',
            metadata_json TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_content_reports_target ON CONTENT_REPORTS(target_type, target_id)"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_content_reports_reporter ON CONTENT_REPORTS(reporter_user_id, created_at)"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_content_reports_status ON CONTENT_REPORTS(status, created_at)"
    )


async def resolve_report_target(conn, *, target_type: str, target_id: int, reporter_user_id: int) -> dict[str, Any] | None:
    if target_type == "prompt":
        cursor = await conn.execute(
            """
            SELECT id, created_by_user_id AS owner_user_id
            FROM PROMPTS
            WHERE id = ? AND public = 1 AND COALESCE(is_unlisted, 0) = 0
            """,
            (target_id,),
        )
        row = await cursor.fetchone()
        if row:
            return {"target_owner_user_id": row["owner_user_id"], "metadata": {"visibility": "public"}}
        return None

    if target_type == "pack":
        cursor = await conn.execute(
            """
            SELECT id, created_by_user_id AS owner_user_id
            FROM PACKS
            WHERE id = ? AND is_public = 1 AND status = 'published'
            """,
            (target_id,),
        )
        row = await cursor.fetchone()
        if row:
            return {"target_owner_user_id": row["owner_user_id"], "metadata": {"visibility": "public"}}
        return None

    if target_type == "conversation":
        cursor = await conn.execute(
            """
            SELECT id, user_id AS owner_user_id
            FROM CONVERSATIONS
            WHERE id = ? AND user_id = ?
            """,
            (target_id, reporter_user_id),
        )
        row = await cursor.fetchone()
        if row:
            return {"target_owner_user_id": row["owner_user_id"], "metadata": {"scope": "own_conversation"}}
        return None

    if target_type == "message":
        cursor = await conn.execute(
            """
            SELECT m.id, m.conversation_id, c.user_id AS owner_user_id
            FROM MESSAGES m
            JOIN CONVERSATIONS c ON c.id = m.conversation_id
            WHERE m.id = ? AND c.user_id = ?
            """,
            (target_id, reporter_user_id),
        )
        row = await cursor.fetchone()
        if row:
            return {
                "target_owner_user_id": row["owner_user_id"],
                "metadata": {
                    "conversation_id": row["conversation_id"],
                    "scope": "own_conversation",
                },
            }
        return None

    return None


async def create_content_report(
    conn,
    *,
    reporter_user_id: int,
    target_type: str,
    target_id: int,
    target_owner_user_id: int | None,
    reason: str,
    details: str | None,
    metadata: dict[str, Any] | None = None,
) -> int:
    cursor = await conn.execute(
        """
        INSERT INTO CONTENT_REPORTS (
            reporter_user_id, target_type, target_id, target_owner_user_id,
            reason, details, metadata_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            reporter_user_id,
            target_type,
            target_id,
            target_owner_user_id,
            reason,
            details,
            json.dumps(metadata or {}, ensure_ascii=True, sort_keys=True),
        ),
    )
    return cursor.lastrowid
