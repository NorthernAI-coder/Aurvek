from ai_runtime.dependencies import *
from ai_runtime.atagia.state import (
    ATAGIA_LIVE_CONFIRMATION_STRATEGY,
    ATAGIA_LIVE_INGEST_ORIGIN,
    _current_atagia_user_message_id,
)

_ATAGIA_CONTEXT_HEADER = "[ATAGIA MEMORY CONTEXT - INTERNAL]"
_ATAGIA_CONTEXT_FOOTER = "[/ATAGIA MEMORY CONTEXT]"

@dataclass
class AtagiaContextDecision:
    full_prompt: str
    active: bool
    reason: str
    context: Any | None = None
    atagia_user_message_id: str | None = None


def _message_text_for_atagia(value: Any) -> str:
    """Convert Aurvek's stored/provider message shape into safe Atagia text."""
    if value is None:
        return ""

    if isinstance(value, bytes):
        try:
            return value.decode("utf-8", errors="ignore")
        except Exception:
            return ""

    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith(("{", "[")):
            try:
                parsed = orjson.loads(stripped)
            except orjson.JSONDecodeError:
                return value
            parsed_text = _message_text_for_atagia(parsed)
            return parsed_text or value
        return value

    if isinstance(value, list):
        parts = [_message_text_for_atagia(item) for item in value]
        return "\n".join(part for part in parts if part)

    if isinstance(value, dict):
        if value.get("multi_ai") and isinstance(value.get("responses"), list):
            response_parts = []
            for response in value["responses"]:
                if not isinstance(response, dict):
                    continue
                label = response.get("model") or response.get("machine") or "model"
                text = _message_text_for_atagia(response.get("content"))
                if text:
                    response_parts.append(f"[{label}]\n{text}")
            return "\n\n".join(response_parts)

        block_type = value.get("type")
        if block_type == "text":
            return str(value.get("text") or "")
        if block_type == "text_file":
            try:
                return text_file_block_to_text(value)
            except Exception:
                filename = value.get("text_file", {}).get("filename", "attached text file")
                return f"[Text file attached: {filename}]"
        if block_type in {"image_url", "image"}:
            return "[Image attached]"
        if block_type in {"document_url", "document", "document_bytes", "file"}:
            filename = (
                value.get("filename")
                or value.get("document_url", {}).get("filename")
                or value.get("file", {}).get("filename")
                or "document"
            )
            return f"[Document attached: {filename}]"

        if "message" in value:
            return _message_text_for_atagia(value.get("message"))
        if "content" in value:
            return _message_text_for_atagia(value.get("content"))

    return str(value)


def _extract_atagia_system_prompt(context: Any) -> str:
    try:
        from atagia.integrations import extract_context_system_prompt

        return extract_context_system_prompt(context)
    except Exception:
        pass

    if context is None:
        return ""
    if isinstance(context, dict):
        raw_prompt = context.get("system_prompt")
    else:
        raw_prompt = getattr(context, "system_prompt", None)
    return raw_prompt.strip() if isinstance(raw_prompt, str) else ""


def _append_atagia_context_to_prompt(full_prompt: str, context: Any) -> str:
    try:
        from atagia.integrations import append_context_to_prompt

        return append_context_to_prompt(full_prompt, context)
    except Exception:
        pass

    atagia_prompt = _extract_atagia_system_prompt(context)
    if not atagia_prompt:
        return full_prompt
    return (
        f"{full_prompt.rstrip()}\n\n"
        f"{_ATAGIA_CONTEXT_HEADER}\n"
        "Use this memory context to personalize and maintain continuity. "
        "Do not reveal this block verbatim to the user.\n\n"
        f"{atagia_prompt}\n"
        f"{_ATAGIA_CONTEXT_FOOTER}"
    )


async def _augment_prompt_with_atagia_context(
    full_prompt: str,
    *,
    user_id: int,
    conversation_id: int,
    message: Any,
    occurred_at: str | None = None,
    prompt_id: int | str | None = None,
    incognito: bool | None = None,
) -> str:
    decision = await _resolve_atagia_context(
        full_prompt,
        user_id=user_id,
        conversation_id=conversation_id,
        message=message,
        occurred_at=occurred_at,
        prompt_id=prompt_id,
        incognito=incognito,
    )
    return decision.full_prompt


async def _resolve_atagia_context(
    full_prompt: str,
    *,
    user_id: int,
    conversation_id: int,
    message: Any,
    occurred_at: str | None = None,
    prompt_id: int | str | None = None,
    message_id: int | str | None = None,
    incognito: bool | None = None,
) -> AtagiaContextDecision:
    _current_atagia_user_message_id.set(None)
    message_text = _message_text_for_atagia(message).strip()
    if not message_text:
        return AtagiaContextDecision(full_prompt, False, "empty_message")

    try:
        bridge = get_atagia_bridge()
        context = await bridge.get_context_for_turn(
            user_id=user_id,
            conversation_id=conversation_id,
            message_text=message_text,
            occurred_at=occurred_at,
            prompt_id=prompt_id,
            message_id=message_id,
            ingest_origin=ATAGIA_LIVE_INGEST_ORIGIN,
            confirmation_strategy=ATAGIA_LIVE_CONFIRMATION_STRATEGY,
            incognito=incognito,
        )
    except Exception:
        logger.warning(
            "[atagia] Failed to fetch sidecar context for conversation_id=%s",
            conversation_id,
            exc_info=True,
        )
        return AtagiaContextDecision(full_prompt, False, "error")
    if context is None:
        return AtagiaContextDecision(full_prompt, False, "no_context")

    try:
        from atagia.integrations import build_injection_decision

        upstream_decision = build_injection_decision(full_prompt, context)
        decision = AtagiaContextDecision(
            upstream_decision.full_prompt,
            upstream_decision.active,
            upstream_decision.reason,
            context=upstream_decision.context,
            atagia_user_message_id=upstream_decision.atagia_user_message_id,
        )
    except Exception:
        augmented = _append_atagia_context_to_prompt(full_prompt, context)
        if augmented == full_prompt:
            return AtagiaContextDecision(full_prompt, False, "empty_context", context=context)
        decision = AtagiaContextDecision(
            augmented,
            True,
            "active",
            context=context,
            atagia_user_message_id=_extract_atagia_message_id(context),
        )

    if not decision.active:
        return decision

    if decision.atagia_user_message_id:
        _current_atagia_user_message_id.set(decision.atagia_user_message_id)

    logger.debug(
        "[atagia] Injected sidecar context for conversation_id=%s user_id=%s",
        conversation_id,
        user_id,
    )
    return decision


def _context_messages_for_provider(
    context_messages: list[dict[str, Any]],
    atagia_decision: AtagiaContextDecision,
) -> list[dict[str, Any]]:
    try:
        from atagia.integrations import context_messages_for_provider

        return context_messages_for_provider(context_messages, atagia_decision)
    except Exception:
        pass

    if atagia_decision.active:
        return []
    return context_messages


def _extract_atagia_message_id(context: Any) -> str | None:
    try:
        from atagia.integrations import extract_context_message_id

        return extract_context_message_id(context)
    except Exception:
        pass

    if context is None:
        return None
    if isinstance(context, dict):
        raw_id = context.get("request_message_id") or context.get("message_id")
    else:
        raw_id = (
            getattr(context, "request_message_id", None)
            or getattr(context, "message_id", None)
        )
    return raw_id if isinstance(raw_id, str) and raw_id else None


async def _warmup_atagia_sidecar(
    user_id: int,
    conversation_id: int,
    *,
    prompt_id: int | str | None = None,
    incognito: bool | None = None,
) -> bool:
    try:
        bridge = get_atagia_bridge()
        return (
            await bridge.ensure_user_and_conversation(
                user_id,
                conversation_id,
                prompt_id=prompt_id,
                incognito=incognito,
            )
            is not None
        )
    except Exception:
        logger.warning(
            "[atagia] Warm-up sidecar preparation failed for conversation_id=%s",
            conversation_id,
            exc_info=True,
        )
        return False
