from ai_runtime.dependencies import *
from ai_runtime.attachments.pdf import (
    _extract_pdf_metadata_from_saved_message,
    _looks_like_generic_context_limit_error,
    _looks_like_pdf_size_error,
    _message_mentions_pdf_context,
    _pdf_too_large_payload,
)
from ai_runtime.provider_health import provider_health_for_error_payload

def _extract_human_error_message(raw_body: str, status_code: int, provider_label: str) -> str:
    """Extract a clean, user-facing error message from a provider error body."""
    if not raw_body or not raw_body.strip():
        return f"{provider_label} service error ({status_code})."

    try:
        parsed = orjson.loads(raw_body)
        if isinstance(parsed, dict):
            error_obj = parsed.get("error")
            if isinstance(error_obj, dict):
                code = error_obj.get("code") or error_obj.get("type")
                message = error_obj.get("message")
                if isinstance(message, str) and message.strip():
                    message = message.strip()
                    if isinstance(code, str) and code.strip() and code.strip() not in message:
                        return f"{code.strip()}: {message}"
                    if status_code == 413 and "413" not in message:
                        return f"413: {message}"
                    return message
                if isinstance(code, str) and code.strip():
                    return code.strip()
            top_level_message = parsed.get("message")
            top_level_code = parsed.get("code") or parsed.get("type")
            if isinstance(top_level_message, str) and top_level_message.strip():
                message = top_level_message.strip()
                if isinstance(top_level_code, str) and top_level_code.strip() and top_level_code.strip() not in message:
                    return f"{top_level_code.strip()}: {message}"
                if status_code == 413 and "413" not in message:
                    return f"413: {message}"
                return message
            if isinstance(top_level_code, str) and top_level_code.strip():
                return top_level_code.strip()
    except Exception:
        pass

    return f"{provider_label} service error ({status_code}). Please try again."


def _human_exception_error(exc: Exception, provider_label: str) -> str:
    """Map caught transport/runtime exceptions to user-facing messages."""
    if isinstance(exc, asyncio.TimeoutError):
        return f"{provider_label} took too long to respond. Please try again or shorten your message."
    if isinstance(exc, aiohttp.ClientError):
        return f"{provider_label} connection error. Please check your network and retry."
    return f"{provider_label} unexpected error. Please try again."

def _provider_error_payload(
    provider_label: str,
    message: str,
    user_message=None,
    pdf_metadata: dict | None = None,
    current_user=None,
    conversation_id: int | None = None,
) -> dict:
    pdf_meta = pdf_metadata or _extract_pdf_metadata_from_saved_message(user_message)
    if pdf_meta and _looks_like_pdf_size_error(
        message,
        has_pdf=True,
        mixed_attachments=bool(pdf_meta.get("has_other_attachments")),
    ):
        current_pdf_count = int(pdf_meta.get("current_pdf_count") or 0)
        generic_context_limit = (
            _looks_like_generic_context_limit_error(message)
            and not _message_mentions_pdf_context(message)
        )
        if generic_context_limit and current_pdf_count <= 0:
            return {"error": message}
        return _pdf_too_large_payload(provider_label, message, user_message, pdf_meta, current_user, conversation_id)
    payload = {"error": message}
    payload.update(provider_health_for_error_payload(provider_label))
    return payload
