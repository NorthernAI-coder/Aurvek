from ai_runtime.dependencies import *
from ai_runtime.atagia.context import _message_text_for_atagia
from ai_runtime.atagia.state import (
    ATAGIA_LIVE_CONFIRMATION_STRATEGY,
    ATAGIA_LIVE_INGEST_ORIGIN,
    _current_atagia_user_message_id,
)

async def _record_atagia_assistant_response(
    *,
    user_id: int,
    conversation_id: int,
    content: Any,
    occurred_at: str | None = None,
    prompt_id: int | str | None = None,
    message_id: int | str | None = None,
    source_seq: int | str | None = None,
    incognito: bool | None = None,
) -> bool:
    response_text = _message_text_for_atagia(content).strip()
    if not response_text:
        return False

    try:
        bridge = get_atagia_bridge()
        return await bridge.record_assistant_response(
            user_id=user_id,
            conversation_id=conversation_id,
            response_text=response_text,
            occurred_at=occurred_at,
            prompt_id=prompt_id,
            message_id=message_id,
            source_seq=source_seq,
            ingest_origin=ATAGIA_LIVE_INGEST_ORIGIN,
            confirmation_strategy=ATAGIA_LIVE_CONFIRMATION_STRATEGY,
            incognito=incognito,
        )
    except Exception:
        logger.warning(
            "[atagia] Failed to record assistant response for conversation_id=%s",
            conversation_id,
            exc_info=True,
        )
        return False


async def _link_atagia_message_best_effort(
    *,
    message_id: int | None,
    atagia_message_id: str | None,
    conversation_id: int,
    user_id: int,
    role: str,
    source: str = "live",
) -> bool:
    if message_id is None or not atagia_message_id:
        return False
    try:
        from atagia_sync import record_atagia_message_link

        return await record_atagia_message_link(
            message_id=int(message_id),
            atagia_message_id=atagia_message_id,
            conversation_id=int(conversation_id),
            user_id=int(user_id),
            role="user" if role == "user" else "assistant",
            source=source,
        )
    except Exception:
        logger.warning(
            "[atagia] Failed to link Aurvek message_id=%s to Atagia",
            message_id,
            exc_info=True,
        )
        return False


def _aurvek_atagia_message_id(message_id: int | str | None) -> str | None:
    if message_id is None:
        return None
    text = str(message_id).strip()
    if not text:
        return None
    if text.startswith("aurvek:msg:"):
        return text
    try:
        from atagia.integrations import aurvek_message_id

        return aurvek_message_id(text)
    except Exception:
        return f"aurvek:msg:{text}"
