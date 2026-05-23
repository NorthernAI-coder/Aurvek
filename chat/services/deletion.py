import os
import shutil

import aiosqlite

from common import generate_user_hash, users_directory
from database import get_db_connection
from file_storage import prune_unreferenced_blobs
from log_config import logger
from models import User

from chat.services.locks import conversation_write_lock
from chat.services.privacy import (
    delete_conversation_rows,
    ensure_conversation_privacy_schema,
    purge_conversation_local_records,
)


async def purge_atagia_conversation_best_effort(
    *,
    user_id: int,
    conversation_id: int,
    prompt_id: int | None = None,
    incognito: bool = False,
) -> bool:
    try:
        from atagia_bridge import get_atagia_bridge

        return await get_atagia_bridge().purge_conversation(
            user_id=user_id,
            conversation_id=conversation_id,
            prompt_id=prompt_id,
            incognito=incognito,
        )
    except Exception:
        logger.warning(
            "Failed to purge Atagia conversation data for conversation_id=%s",
            conversation_id,
            exc_info=True,
        )
        return False


async def delete_conversation_files_for_user(
    current_user: User,
    conversation_id: int,
) -> bool:
    hash_prefix1, hash_prefix2, user_hash = generate_user_hash(current_user.username)
    conversation_id_str = f"{conversation_id:07d}"
    conversation_folder = os.path.join(
        users_directory,
        hash_prefix1,
        hash_prefix2,
        user_hash,
        "files",
        conversation_id_str[:3],
        conversation_id_str[3:],
    )
    if not os.path.exists(conversation_folder):
        return True
    try:
        shutil.rmtree(conversation_folder)
        return True
    except OSError as exc:
        logger.error("Error deleting conversation folder %s: %s", conversation_id, str(exc))
        return False


async def purge_stale_incognito_conversations_for_user(current_user: User) -> None:
    try:
        await ensure_conversation_privacy_schema()
        async with get_db_connection(readonly=True) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                """
                SELECT id, role_id
                FROM CONVERSATIONS
                WHERE user_id = ?
                  AND COALESCE(is_incognito, 0) = 1
                """,
                (current_user.id,),
            )
            rows = await cursor.fetchall()
    except Exception:
        logger.warning(
            "Failed to load stale incognito conversations for user_id=%s",
            current_user.id,
            exc_info=True,
        )
        return

    for row in rows:
        conversation_id = int(row["id"])
        await purge_atagia_conversation_best_effort(
            user_id=current_user.id,
            conversation_id=conversation_id,
            prompt_id=row["role_id"],
            incognito=True,
        )
        try:
            purged = await purge_conversation_local_records(
                conversation_id=conversation_id,
                user_id=current_user.id,
            )
            if purged:
                await delete_conversation_files_for_user(current_user, conversation_id)
                await prune_unreferenced_blobs()
        except Exception:
            logger.warning(
                "Failed to purge stale incognito conversation_id=%s",
                conversation_id,
                exc_info=True,
            )


async def delete_owned_conversation(current_user: User, conversation_id: int) -> dict:
    async with get_db_connection() as conn:
        cursor = await conn.cursor()
        await ensure_conversation_privacy_schema(conn)

        await cursor.execute(
            """
            SELECT user_id, role_id, COALESCE(is_incognito, 0) AS is_incognito
            FROM conversations
            WHERE id = ?
            """,
            (conversation_id,),
        )
        result = await cursor.fetchone()
        if not result:
            return {"success": False, "error": "Conversation not found", "status_code": 404}

        user_id = result[0]
        if user_id != current_user.id:
            return {"success": False, "error": "Access denied", "status_code": 403}

        prompt_id = result[1]
        is_incognito = bool(result[2])
        atagia_purged = False
        if is_incognito:
            atagia_purged = await purge_atagia_conversation_best_effort(
                user_id=current_user.id,
                conversation_id=conversation_id,
                prompt_id=prompt_id,
                incognito=True,
            )

        await delete_conversation_rows(
            conn,
            conversation_id=conversation_id,
            user_id=current_user.id,
        )
        await conn.commit()

    await prune_unreferenced_blobs()
    await delete_conversation_files_for_user(current_user, conversation_id)
    return {"success": True, "atagia_purged": atagia_purged}


async def close_incognito_conversation_for_user(current_user: User, privacy: dict) -> dict:
    conversation_id = int(privacy["id"])
    async with conversation_write_lock(conversation_id):
        atagia_purged = await purge_atagia_conversation_best_effort(
            user_id=current_user.id,
            conversation_id=conversation_id,
            prompt_id=privacy.get("role_id"),
            incognito=True,
        )
        purged = await purge_conversation_local_records(
            conversation_id=conversation_id,
            user_id=current_user.id,
        )

    if purged:
        await delete_conversation_files_for_user(current_user, conversation_id)
        await prune_unreferenced_blobs()

    return {
        "success": True,
        "purged": bool(purged),
        "atagia_purged": bool(atagia_purged),
    }


async def delete_conversation_recursively(conversation_id):
    async with get_db_connection() as conn:
        cursor = await conn.cursor()
        await ensure_conversation_privacy_schema(conn)
        await cursor.execute("SELECT user_id FROM conversations WHERE id = ?", (conversation_id,))
        result = await cursor.fetchone()
        if result:
            user_id = result[0]
            await delete_conversation_rows(conn, conversation_id=conversation_id)
            await conn.commit()
            await prune_unreferenced_blobs()
            return user_id
    return None


async def delete_conversation_folder(static_directory, user_id, conversation_id):
    conversation_folder = os.path.join(str(static_directory), "files", str(user_id), str(conversation_id))
    try:
        if os.path.exists(conversation_folder):
            shutil.rmtree(conversation_folder)
    except OSError as exc:
        logger.error("Error deleting conversation folder %s: %s", conversation_id, str(exc))
        return False
    return True
