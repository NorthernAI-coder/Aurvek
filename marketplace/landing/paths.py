from pathlib import Path

from fastapi import HTTPException

from common import DATA_DIR, generate_user_hash, sanitize_name
from database import get_db_connection


def build_prompt_filesystem_path(username: str, prompt_id: int, prompt_name: str) -> Path:
    """Build the filesystem path to a prompt landing page directory."""
    hash_prefix1, hash_prefix2, user_hash = generate_user_hash(username)
    padded_id = f"{prompt_id:07d}"
    safe_prompt_name = sanitize_name(prompt_name)

    return (
        DATA_DIR
        / "users"
        / hash_prefix1
        / hash_prefix2
        / user_hash
        / "prompts"
        / padded_id[:3]
        / f"{padded_id[3:]}_{safe_prompt_name}"
    )


async def resolve_prompt_by_public_id(public_id: str) -> dict:
    """
    Fetch prompt data by public_id.
    Returns prompt_id, prompt_name, is_unlisted, and username.
    """
    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.execute(
            """
            SELECT p.id, p.name, p.is_unlisted, u.username
            FROM PROMPTS p
            JOIN USERS u ON p.created_by_user_id = u.id
            WHERE p.public_id = ?
            """,
            (public_id,),
        )
        result = await cursor.fetchone()

    if not result:
        raise HTTPException(status_code=404, detail="Prompt not found")

    return {
        "prompt_id": result[0],
        "prompt_name": result[1],
        "is_unlisted": result[2] or 0,
        "username": result[3],
    }


async def get_active_custom_domain(prompt_id: int) -> str | None:
    """
    Return a prompt's active verified custom domain, if one exists.
    """
    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.execute(
            """
            SELECT custom_domain FROM PROMPT_CUSTOM_DOMAINS
            WHERE prompt_id = ? AND is_active = 1 AND verification_status = 1
            """,
            (prompt_id,),
        )
        result = await cursor.fetchone()
        return result[0] if result else None
