from ai_runtime.dependencies import *

async def text_file_block_to_text_for_context(
    block: dict,
    current_user=None,
    conversation_id: int | None = None,
) -> str:
    text_info = block.get("text_file", {}) if isinstance(block, dict) else {}
    attachment_ref = text_info.get("attachment_ref")
    if attachment_ref and current_user is not None:
        try:
            result = await read_attachment_bytes(
                attachment_ref,
                user_id=current_user.id,
                conversation_id=conversation_id,
                require_kind="text",
            )
        except Exception as exc:
            logger.warning("[text_file_block_to_text_for_context] Could not read attachment %s: %s", attachment_ref, exc)
            result = None
        if result:
            data, _ = result
            return data.decode("utf-8", errors="replace")
    owner_username = getattr(current_user, "username", None)
    return text_file_block_to_text(
        block,
        owner_username=owner_username,
        conversation_id=conversation_id,
    )
