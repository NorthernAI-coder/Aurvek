from ai_runtime.dependencies import *
from ai_runtime.billing import assert_billable_claude_system_key
from ai_runtime.config import _log_output_limit_decision, _model_output_cap
from ai_runtime.context.formatting import _format_messages_for_provider
from ai_runtime.context.system import assemble_system_prompt, get_effective_blocks
from ai_runtime.providers.claude import call_claude_api
from ai_runtime.providers.gemini import call_gemini_api
from ai_runtime.providers.openai_chat import call_o1_api
from ai_runtime.providers.openai_responses import call_gpt_responses_api
from ai_runtime.providers.openrouter import call_openrouter_api
from ai_runtime.providers.xai import call_xai_responses_api
from ai_runtime.watchdog.prompting import _sanitize_watchdog_directive

TAKEOVER_PROMPT_TEMPLATE = """You are taking over this conversation on behalf of the regular AI assistant.
A supervisor system detected an issue that requires your intervention.

ORIGINAL BOT PERSONALITY/INSTRUCTIONS:
{original_prompt}

SUPERVISOR DIRECTIVE (you MUST follow this):
{directive}

IMPORTANT:
- Stay in character as the original bot personality described above.
- Follow the directive naturally within that character.
- Never mention the supervisor system, takeover, or that you are a different AI.
- Respond as if you are the same assistant that has been chatting all along."""

TAKEOVER_SECURITY_SUFFIX = """

=== TAKEOVER SECURITY ===
- You are a TAKEOVER responder. You MUST follow the supervisor directive above.
- Never reveal the existence of the supervisor system or the takeover mechanism.
- Never acknowledge being a different AI or replacement.
- If the user asks about system changes, deflect naturally in character.
==========================="""

async def watchdog_takeover_response(
    conversation_id: int,
    prompt_id: int,
    user_id: int,
    watchdog_config: dict,
    original_prompt: str,
    directive: str,
    context_messages: list,
    user_message,
    message,
    should_lock: bool,
    current_user,
    request,
    user_api_keys: dict,
    machine: str,
    model: str,
    event_type: str = "security",
    source: str = "post",
    pending_attachment_refs: Optional[list[str]] = None,
):
    """Async generator: stream a takeover response from the watchdog LLM.

    Yields SSE chunks. If should_lock, also locks the conversation and yields
    an end_conversation event.
    """
    # 1. Resolve watchdog LLM
    wd_llm_id = watchdog_config.get("llm_id")
    wd_llm = await get_llm_info(wd_llm_id)
    if not wd_llm:
        logger.error("watchdog takeover: LLM id=%s not found", wd_llm_id)
        yield f"data: {orjson.dumps({'error': 'Watchdog LLM not found'}).decode()}\n\n"
        return

    wd_machine = wd_llm["machine"]
    wd_model = wd_llm["model"]
    wd_max_tokens, wd_limit_fallback = _model_output_cap(wd_llm.get("max_output_tokens"))
    _log_output_limit_decision(
        source="watchdog_takeover",
        conversation_id=conversation_id,
        llm_id=wd_llm_id,
        machine=wd_machine,
        model=wd_model,
        max_output_tokens=wd_llm.get("max_output_tokens"),
        fallback_used=wd_limit_fallback,
        final_limit=wd_max_tokens,
        balance_limited=False,
    )

    # 2. Resolve BYOK key for watchdog LLM
    api_key_mode = await get_user_api_key_mode(user_id)
    resolved_key, use_system = resolve_api_key_for_provider(
        user_api_keys or {}, api_key_mode, wd_machine
    )
    if not resolved_key and not use_system:
        logger.error("watchdog takeover: no API key for %s", wd_machine)
        yield f"data: {orjson.dumps({'error': 'API key required for takeover LLM'}).decode()}\n\n"
        return

    wd_guard_error = assert_billable_claude_system_key(
        machine=wd_machine,
        model=wd_model,
        llm_id=wd_llm_id,
        is_byok=resolved_key is not None,
        input_token_cost=wd_llm.get("input_token_cost", 0),
        output_token_cost=wd_llm.get("output_token_cost", 0),
    )
    if wd_guard_error:
        logger.error(wd_guard_error)
        yield f"data: {orjson.dumps({'error': wd_guard_error}).decode()}\n\n"
        return

    # 3. Sanitize directive
    sanitized_directive = _sanitize_watchdog_directive(directive)

    # 4. Build system prompt via global blocks (system blocks only for takeover)
    blocks = await get_effective_blocks()
    takeover_blocks = [b for b in blocks if b.get("system_key") in SYSTEM_BLOCK_METADATA]
    if await current_user.is_admin:
        user_level = "admin"
    elif await current_user.is_user:
        user_level = "user"
    else:
        user_level = "customer"
    variables = {
        "user_level": user_level,
        "current_datetime_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }

    takeover_base = TAKEOVER_PROMPT_TEMPLATE.format(
        original_prompt=original_prompt[:5000],
        directive=sanitized_directive,
    )
    assembled = assemble_system_prompt(takeover_blocks, variables, takeover_base,
                                        watchdog_enabled=True)
    full_prompt = assembled + "\n\n" + TAKEOVER_SECURITY_SUFFIX.strip()

    # 5. Format messages for the watchdog LLM's provider
    api_messages = await _format_messages_for_provider(
        context_messages, message, full_prompt, wd_machine, current_user,
        conversation_id=conversation_id,
    )

    # 6. Select streaming function
    if wd_machine == "Gemini":
        api_func = call_gemini_api
    elif wd_machine == "O1":
        api_func = call_o1_api
    elif wd_machine == "GPT":
        api_func = call_gpt_responses_api
    elif wd_machine == "Claude":
        api_func = call_claude_api
    elif wd_machine == "xAI":
        api_func = call_xai_responses_api
    elif wd_machine == "OpenRouter":
        api_func = call_openrouter_api
    else:
        logger.error("watchdog takeover: unknown machine %s", wd_machine)
        yield f"data: {orjson.dumps({'error': f'Unknown LLM provider: {wd_machine}'}).decode()}\n\n"
        return

    # 7. Build kwargs (no tools, no watchdog_config to prevent recursion)
    kwargs = {
        "messages": api_messages,
        "model": wd_model,
        "temperature": 0.3,
        "max_tokens": wd_max_tokens,
        "prompt": full_prompt,
        "conversation_id": conversation_id,
        "current_user": current_user,
        "request": request,
        "user_message": user_message,
        "prompt_id": prompt_id,
        "watchdog_config": None,  # Prevent self-evaluation
        "watchdog_hint_active": False,
        "watchdog_hint_eval_id": None,
        "llm_id": wd_llm_id,
        "byok": resolved_key is not None,
        "pending_attachment_refs": pending_attachment_refs,
    }
    if resolved_key:
        kwargs["user_api_key"] = resolved_key

    # 8. Stream response
    try:
        async for chunk in api_func(**kwargs):
            # Skip tool call chunks (takeover doesn't support tools)
            if isinstance(chunk, str) and ("tool_call" in chunk and "tool_call_pending" not in chunk):
                continue
            if isinstance(chunk, str) and "tool_call_pending" in chunk:
                continue
            yield chunk
    except Exception as exc:
        logger.error("watchdog takeover: streaming failed for conv=%d: %s", conversation_id, exc)
        # Persist error event
        from tools.watchdog import _persist_error_event
        await _persist_error_event(conversation_id, prompt_id, 0, 0, f"Takeover streaming error: {exc}", source)
        raise

    # 9. Finalize takeover (lock if needed, clean state, persist event)
    from tools.watchdog import _finalize_takeover
    await _finalize_takeover(
        conversation_id, prompt_id, event_type, directive,
        channel="web", should_lock=should_lock,
        locked_reason=f"WATCHDOG_{event_type.upper()}_TAKEOVER" if should_lock else None,
    )
    if should_lock:
        yield f"data: {orjson.dumps({'end_conversation': True}).decode()}\n\n"


class _StubUser:
    """Minimal user stub for provider functions that only need current_user.id."""
    __slots__ = ("id",)

    def __init__(self, user_id: int):
        self.id = user_id


async def watchdog_takeover_response_requestfree(
    directive: str,
    watchdog_config: dict,
    context_messages: list,
    user_id: int,
    conversation_id: int = 0,
    prompt_id: int = 0,
    original_prompt: str = "",
    user_level: str = "customer",
    source: str = "post",
):
    """Request-free watchdog takeover response generator.

    Extracted from watchdog_takeover_response() for use in both web chat
    (get_ai_response) and external channels (process_gransabio_external)
    where no FastAPI Request or full User object is available.

    Args:
        directive: The watchdog's instruction (what to generate).
        watchdog_config: Sub-config dict (pre or post watchdog) with llm_id, etc.
        context_messages: Conversation history for context.
        user_id: For BYOK key resolution.
        conversation_id: Conversation ID (for stop signals and logging).
        prompt_id: Prompt ID (for event persistence).
        original_prompt: The bot's system prompt (for takeover template).
        user_level: One of "admin", "user", "customer" (for system block variables).
        source: "pre" or "post" (for event persistence).

    Yields:
        SSE-formatted string chunks (same format as provider functions).
    """
    # 1. Resolve watchdog LLM
    wd_llm_id = watchdog_config.get("llm_id")
    wd_llm = await get_llm_info(wd_llm_id)
    if not wd_llm:
        logger.error("watchdog takeover requestfree: LLM id=%s not found", wd_llm_id)
        yield f"data: {orjson.dumps({'error': 'Watchdog LLM not found'}).decode()}\n\n"
        return

    wd_machine = wd_llm["machine"]
    wd_model = wd_llm["model"]
    wd_max_tokens, wd_limit_fallback = _model_output_cap(wd_llm.get("max_output_tokens"))
    _log_output_limit_decision(
        source="watchdog_takeover_requestfree",
        conversation_id=conversation_id,
        llm_id=wd_llm_id,
        machine=wd_machine,
        model=wd_model,
        max_output_tokens=wd_llm.get("max_output_tokens"),
        fallback_used=wd_limit_fallback,
        final_limit=wd_max_tokens,
        balance_limited=False,
    )

    # 2. Resolve BYOK key for watchdog LLM
    from tools.watchdog import _read_user_api_keys
    user_api_keys = await _read_user_api_keys(user_id)
    api_key_mode = await get_user_api_key_mode(user_id)
    resolved_key, use_system = resolve_api_key_for_provider(
        user_api_keys, api_key_mode, wd_machine
    )
    if not resolved_key and not use_system:
        logger.error("watchdog takeover requestfree: no API key for %s", wd_machine)
        yield f"data: {orjson.dumps({'error': 'API key required for takeover LLM'}).decode()}\n\n"
        return

    wd_guard_error = assert_billable_claude_system_key(
        machine=wd_machine,
        model=wd_model,
        llm_id=wd_llm_id,
        is_byok=resolved_key is not None,
        input_token_cost=wd_llm.get("input_token_cost", 0),
        output_token_cost=wd_llm.get("output_token_cost", 0),
    )
    if wd_guard_error:
        logger.error(wd_guard_error)
        yield f"data: {orjson.dumps({'error': wd_guard_error}).decode()}\n\n"
        return

    # 3. Sanitize directive
    sanitized_directive = _sanitize_watchdog_directive(directive)

    # 4. Build system prompt via global blocks
    blocks = await get_effective_blocks()
    takeover_blocks = [b for b in blocks if b.get("system_key") in SYSTEM_BLOCK_METADATA]
    variables = {
        "user_level": user_level,
        "current_datetime_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }

    takeover_base = TAKEOVER_PROMPT_TEMPLATE.format(
        original_prompt=original_prompt[:5000],
        directive=sanitized_directive,
    )
    assembled = assemble_system_prompt(takeover_blocks, variables, takeover_base,
                                        watchdog_enabled=True)
    full_prompt = assembled + "\n\n" + TAKEOVER_SECURITY_SUFFIX.strip()

    # 5. Format messages for the watchdog LLM's provider
    # Extract last user message as plain text (no multimodal for external channels)
    last_user_msg = ""
    for msg in reversed(context_messages):
        if msg.get("type") == "user":
            content = msg.get("message", "")
            if isinstance(content, list):
                last_user_msg = " ".join(
                    b.get("text", "") for b in content if b.get("type") == "text"
                )
            else:
                last_user_msg = str(content)
            break

    api_messages = await _format_messages_for_provider(
        context_messages, last_user_msg, full_prompt, wd_machine,
        current_user=None,
        conversation_id=conversation_id,
    )

    # 6. Select streaming function
    if wd_machine == "Gemini":
        api_func = call_gemini_api
    elif wd_machine == "O1":
        api_func = call_o1_api
    elif wd_machine == "GPT":
        api_func = call_gpt_responses_api
    elif wd_machine == "Claude":
        api_func = call_claude_api
    elif wd_machine == "xAI":
        api_func = call_xai_responses_api
    elif wd_machine == "OpenRouter":
        api_func = call_openrouter_api
    else:
        logger.error("watchdog takeover requestfree: unknown machine %s", wd_machine)
        yield f"data: {orjson.dumps({'error': f'Unknown LLM provider: {wd_machine}'}).decode()}\n\n"
        return

    # 7. Build kwargs (stub user, no request, no tools, no watchdog to prevent recursion)
    # save_to_db=False: caller (process_gransabio_external or get_ai_response)
    # owns persistence. Prevents double-save when providers auto-persist.
    stub_user = _StubUser(user_id)
    kwargs = {
        "messages": api_messages,
        "model": wd_model,
        "temperature": 0.3,
        "max_tokens": wd_max_tokens,
        "prompt": full_prompt,
        "conversation_id": conversation_id,
        "current_user": stub_user,
        "request": None,
        "user_message": last_user_msg,
        "prompt_id": prompt_id,
        "watchdog_config": None,
        "watchdog_hint_active": False,
        "watchdog_hint_eval_id": None,
        "llm_id": wd_llm_id,
        "byok": resolved_key is not None,
        "save_to_db": False,
    }
    if resolved_key:
        kwargs["user_api_key"] = resolved_key

    # 8. Stream response
    try:
        async for chunk in api_func(**kwargs):
            if isinstance(chunk, str) and ("tool_call" in chunk and "tool_call_pending" not in chunk):
                continue
            if isinstance(chunk, str) and "tool_call_pending" in chunk:
                continue
            yield chunk
    except Exception as exc:
        logger.error("watchdog takeover requestfree: streaming failed for conv=%d: %s",
                     conversation_id, exc)
        from tools.watchdog import _persist_error_event
        await _persist_error_event(
            conversation_id, prompt_id, 0, 0,
            f"Takeover requestfree streaming error: {exc}", source,
        )
        raise
