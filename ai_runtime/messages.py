from ai_runtime.dependencies import *
from tools import tools
from ai_runtime.attachments.media import format_image_for_provider, hydrate_image_for_context
from ai_runtime.attachments.pdf import (
    _decode_pdf_retry_token,
    _drop_pdf_blocks_from_context,
    _estimate_pdf_input_tokens_for_preflight,
    _extract_pdf_metadata_from_context_messages,
    _extract_pdf_metadata_from_saved_message,
    _merge_pdf_error_metadata,
    _pdf_upload_too_large_payload,
    _ranged_pdf_warning_text,
    _validate_pdf_retry_upload,
    format_pdf_for_provider,
    hydrate_pdf_for_context,
)
from ai_runtime.attachments.text_files import text_file_block_to_text_for_context
from ai_runtime.memory.context import _context_messages_for_memory_provider, _resolve_memory_context
from ai_runtime.perf_trace import ChatPerfTrace
from memory.health import get_user_memory_health_snapshot, should_surface_memory_health
from ai_runtime.billing import assert_billable_claude_system_key
from ai_runtime.config import (
    NATIVE_SEARCH_PROVIDERS,
    _log_output_limit_decision,
    _model_output_cap,
)
from ai_runtime.context.assembly import (
    apply_rate_limit,
    build_full_prompt_context,
    check_own_only_gransabio,
    run_input_moderation,
    update_chat_name_if_empty,
)
from ai_runtime.context.formatting import (
    _format_messages_for_provider,
    filter_invalid_context_messages,
    flatten_multi_ai_context,
    parse_stored_message,
)
from ai_runtime.context.history import apply_no_memory_context_budget
from ai_runtime.context.system import assemble_system_prompt, get_effective_blocks
from ai_runtime.context.warmup import (
    _build_warmup_cache_key_from_state,
    _copy_warmup_context_messages,
)
from ai_runtime.providers.claude import call_claude_api
from ai_runtime.providers.gemini import call_gemini_api
from ai_runtime.providers.kimi import call_kimi_api
from ai_runtime.providers.minimax import call_minimax_api
from ai_runtime.providers.openai_chat import call_o1_api
from ai_runtime.providers.openai_responses import call_gpt_responses_api
from ai_runtime.providers.openrouter import call_openrouter_api
from ai_runtime.providers.xai import call_xai_responses_api
from ai_runtime.streaming import _stream_with_sse_keepalives
from ai_runtime.tooling.catalog import tools_in_app
from ai_runtime.tooling.execution import _build_tool_response_messages, handle_function_call
from ai_runtime.tooling.formatters import (
    tools_for_claude,
    tools_for_gemini,
    tools_for_openai,
    tools_for_openai_responses,
    tools_for_xai_responses,
)
from ai_runtime.watchdog.prompting import _build_escalated_hint_block, _sanitize_watchdog_directive
from ai_runtime.watchdog.takeover import watchdog_takeover_response

async def process_save_message(
    request: Request,
    conversation_id: int,
    current_user: User,
    text_compressed: Optional[bytes] = None,  # bytes instead of UploadFile
    text_plain: Optional[str] = None,
    files: Optional[List[dict]] = None,  # dict with 'data', 'content_type', 'filename'
    full_response: bool = False,
    is_whatsapp: bool = False,
    thinking_budget_tokens: Optional[int] = None,
    user_api_keys: Optional[dict] = None,  # User's custom API keys
    prevalidated: bool = False,
    pdf_page_start: Optional[int] = None,
    pdf_page_end: Optional[int] = None,
    pdf_retry_token: Optional[str] = None,
    attachment_refs: Optional[list[str]] = None,
):
    """
    Pure business logic function for processing and saving messages.
    No FastAPI dependencies (Form, File, Depends).
    """
    logger.debug("enters into process_save_message")
    perf_trace = ChatPerfTrace.from_request(request)
    perf_trace.mark("process_start", conversation_id=conversation_id, user_id=current_user.id)

    files = list(files or [])
    try:
        attachment_refs = parse_attachment_refs_value(attachment_refs)
    except ValueError as exc:
        return JSONResponse(content={'success': False, 'message': str(exc)}, status_code=400)

    if (files or attachment_refs) and not current_user.can_send_files:
        return JSONResponse(
            content={'success': False, 'message': 'File uploads are not enabled for your account'},
            status_code=403
        )

    if not prevalidated:
        guard_response = await validate_message_request(
            request=request,
            current_user=current_user,
            is_whatsapp=is_whatsapp,
        )
        if guard_response is not None:
            return guard_response

    context_months = 2
    start_date = (
        datetime.now(timezone.utc) - timedelta(days=context_months * 30)
    ).strftime("%Y-%m-%d %H:%M:%S.%f")

    global stop_signals, MAX_TOKENS
    # NOTE: stop_signals reset is deferred until AFTER the DB query resolves
    # gransabio_enabled_early. GranSabio resets inside generate_via_gransabio()
    # after lock acquisition; non-GranSabio resets below after the query.

    # Process the received message
    # Maximum decompressed message size: 10MB (protection against zip bombs)
    MAX_DECOMPRESSED_SIZE = 10 * 1024 * 1024
    # Maximum compressed input size: 1MB
    MAX_COMPRESSED_SIZE = 1 * 1024 * 1024

    try:
        if text_plain is not None:
            logger.debug(f"text_plain: {text_plain}")

            # If plain text exists, use it
            user_message = text_plain
        elif text_compressed is not None:
            logger.debug(f"text_compressed (bytes): {len(text_compressed)} bytes")

            # Check compressed size before decompression
            if len(text_compressed) > MAX_COMPRESSED_SIZE:
                return JSONResponse(content={'success': False, 'message': 'Compressed message too large'}, status_code=400)

            # If no plain text, assume a compressed file was sent
            # Use decompressobj with max_length to prevent zip bombs
            decompressor = zlib.decompressobj()
            decompressed = decompressor.decompress(text_compressed, max_length=MAX_DECOMPRESSED_SIZE)

            # Check if there's more data (indicates zip bomb attempt)
            if decompressor.unconsumed_tail:
                return JSONResponse(content={'success': False, 'message': 'Decompressed message exceeds size limit'}, status_code=400)

            user_message = decompressed.decode('utf-8')
        else:
            raise ValueError("[process_save_message] - No message provided")

        # Reject empty messages when no files are attached
        if (not user_message or not user_message.strip()) and not files and not attachment_refs:
            raise ValueError("Message content cannot be empty")

        message_size = len(user_message.encode('utf-8'))
        perf_trace.mark(
            "message_parsed",
            message_chars=len(user_message),
            message_bytes=message_size,
            file_count=len(files or []),
            attachment_ref_count=len(attachment_refs or []),
        )
    except zlib.error as e:
        logger.error(f"[process_save_message] - Decompression error: {e}")
        return JSONResponse(content={'success': False, 'message': 'Invalid compressed data'}, status_code=400)
    except Exception as e:
        logger.error(f"Error processing the message: {e}")
        return JSONResponse(content={'success': False, 'message': f'Failed to process message: {str(e)}'}, status_code=400)

    message_list_to_save = []
    message_list_to_send = []
    pending_attachment_refs: list[str] = []
    discardable_attachment_refs: list[str] = []

    async def _attachment_error_response(message: str, status_code: int = 400):
        await discard_pending_attachments(discardable_attachment_refs, "message_upload_aborted")
        return JSONResponse(content={'success': False, 'message': message}, status_code=status_code)

    async def _attachment_json_error_response(content: dict, status_code: int = 400):
        await discard_pending_attachments(discardable_attachment_refs, "message_upload_aborted")
        return JSONResponse(content=content, status_code=status_code)

    logger.debug("Before entering into get_db_connection")

    await ensure_conversation_privacy_schema()

    # Use read-only connection for SELECT queries
    async with get_db_connection(readonly=True) as conn_ro:
        logger.info("right after get_db_connection")
        # Consolidate SQL queries into one
        async with conn_ro.execute('''
            SELECT c.locked, c.llm_id, c.user_id, c.chat_name,
                   CASE WHEN c.role_id IS NULL THEN ud.current_prompt_id ELSE c.role_id END AS effective_prompt_id,
                   c.active_extension_id,
                   (
                       SELECT COALESCE(MAX(m.id), 0)
                       FROM messages m
                       WHERE m.conversation_id = c.id
                   ) AS last_message_id,
                   L.machine, L.model, COALESCE(L.input_token_cost, 0), COALESCE(L.output_token_cost, 0),
                   COALESCE(L.max_output_tokens, 0),
                   COALESCE(ep.enable_moderation, 0) AS enable_moderation,
                   COALESCE(ep.is_paid, 0) AS is_paid,
                   COALESCE(ep.gransabio_enabled, 0) AS gransabio_enabled,
                   COALESCE(c.is_incognito, 0) AS is_incognito
            FROM conversations c
            JOIN LLM L ON c.llm_id = L.id
            LEFT JOIN USER_DETAILS ud ON ud.user_id = c.user_id
            LEFT JOIN PROMPTS ep ON ep.id = COALESCE(c.role_id, ud.current_prompt_id)
            WHERE c.id = ?
        ''', (conversation_id,)) as cursor:
            conversation_row = await cursor.fetchone()
            if not conversation_row:
                return JSONResponse(content={'success': False, 'message': 'Conversation not found.'}, status_code=404)

            (
                is_locked,
                conversation_llm_id,
                conversation_user_id,
                chat_name,
                effective_prompt_id,
                active_extension_id,
                last_message_id,
                machine,
                model,
                input_token_cost,
                output_token_cost,
                llm_max_output_tokens,
                enable_moderation,
                prompt_is_paid,
                gransabio_enabled_col,
                conversation_incognito,
            ) = conversation_row
            perf_trace.mark(
                "conversation_loaded",
                llm_id=conversation_llm_id,
                machine=machine,
                model=model,
                prompt_id=effective_prompt_id,
                incognito=bool(conversation_incognito),
            )

        conversation_incognito = bool(conversation_incognito)

        if is_locked:
            logger.info(f"Ignored message to conversation ID {conversation_id}, Locked state: {is_locked}")
            return JSONResponse(content={'success': False, 'message': 'Conversation is locked.'}, status_code=403)

        if not full_response and current_user.id != conversation_user_id:
            logger.info(f"You cannot save messages to another user's conversation. current_user.id: {current_user.id}, conversation_user_id: {conversation_user_id}")
            return JSONResponse(content={'success': False, 'message': 'You cannot save messages to another user\'s conversation.'}, status_code=403)

        MAX_FILES_PER_MESSAGE = 16
        if len(files) + len(attachment_refs) > MAX_FILES_PER_MESSAGE:
            return JSONResponse(
                content={'success': False, 'message': f'Maximum {MAX_FILES_PER_MESSAGE} files per message.'},
                status_code=400,
            )

        if attachment_refs:
            try:
                files.extend(
                    await load_pending_attachment_files(
                        attachment_refs=attachment_refs,
                        user_id=current_user.id,
                        conversation_id=conversation_id,
                    )
                )
                discardable_attachment_refs.extend(attachment_refs)
            except ValueError as exc:
                return JSONResponse(content={'success': False, 'message': str(exc)}, status_code=400)

        logger.debug(f"text en process_save_message: {user_message}")

        input_tokens = estimate_message_tokens(user_message)
        pdf_pages_in_request = 0
        pdf_range_requested_preflight = pdf_page_start is not None or pdf_page_end is not None
        pdf_file_count = 0
        pdf_filename_for_retry = None
        pdf_file_hash_for_retry = None
        pdf_retry_payload = None
        skip_context_pdfs_for_retry = False

        if files:
            pdf_file_count = sum(1 for f in files if f['content_type'] == 'application/pdf')
            pdf_filename_for_retry = next(
                (f.get('filename') for f in files if f['content_type'] == 'application/pdf'),
                None,
            )
            if pdf_file_count == 1:
                retry_pdf = next((f for f in files if f['content_type'] == 'application/pdf'), None)
                if retry_pdf:
                    pdf_file_hash_for_retry = hashlib.sha1(retry_pdf['data']).hexdigest()

        if pdf_range_requested_preflight:
            if pdf_file_count != 1:
                return await _attachment_error_response('Page range retry supports one PDF at a time.')
            if pdf_page_start is None or pdf_page_end is None:
                return await _attachment_error_response('Both PDF page start and end are required.')
            pdf_retry_payload = _decode_pdf_retry_token(pdf_retry_token, current_user, conversation_id)
            if not pdf_retry_payload:
                return await _attachment_error_response('PDF range retry expired. Please resend the original PDF and wait for the range prompt again.')

        if files:
            for f in files:
                if is_text_file(f['content_type'], f['filename']):
                    input_tokens += int(len(f['data']) / 4 * 1.1 + 0.5)
                elif f['content_type'] == 'application/pdf':
                    if len(f['data']) > MAX_PDF_SIZE_MB * 1024 * 1024:
                        return await _attachment_error_response(f'PDF exceeds {MAX_PDF_SIZE_MB}MB limit.')
                    try:
                        page_count_for_cost = validate_pdf(
                            f['data'],
                            enforce_page_limit=False,
                        )
                        if pdf_range_requested_preflight:
                            retry_validation_error = _validate_pdf_retry_upload(
                                pdf_retry_payload,
                                f['data'],
                                page_count_for_cost,
                            )
                            if retry_validation_error:
                                return await _attachment_error_response(retry_validation_error)
                            range_start = int(pdf_page_start)
                            range_end = int(pdf_page_end)
                            if range_start < 1 or range_end < range_start or range_end > page_count_for_cost:
                                return await _attachment_error_response(f'PDF page range exceeds document length ({page_count_for_cost} pages)')
                            page_count_for_cost = range_end - range_start + 1
                            if page_count_for_cost > MAX_PDF_PAGES:
                                return await _attachment_json_error_response(
                                    _pdf_upload_too_large_payload(
                                        f'PDF page range exceeds {MAX_PDF_PAGES} page limit ({page_count_for_cost} pages)',
                                        pdf_file_count,
                                        page_count_for_cost,
                                        filename=f.get('filename'),
                                        current_user=current_user,
                                        conversation_id=conversation_id,
                                        retry_file_hash=hashlib.sha1(f['data']).hexdigest(),
                                    ),
                                    status_code=400
                                )
                        elif page_count_for_cost > MAX_PDF_PAGES:
                            return await _attachment_json_error_response(
                                _pdf_upload_too_large_payload(
                                    f'PDF exceeds {MAX_PDF_PAGES} page limit ({page_count_for_cost} pages)',
                                    pdf_file_count,
                                    page_count_for_cost,
                                    filename=f.get('filename') if pdf_file_count == 1 else None,
                                    current_user=current_user,
                                    conversation_id=conversation_id,
                                    retry_file_hash=hashlib.sha1(f['data']).hexdigest() if pdf_file_count == 1 else None,
                                ),
                                status_code=400
                            )
                    except ValueError as e:
                        return await _attachment_json_error_response(
                            {'success': False, 'message': str(e), 'error_code': 'pdf_validation_error'},
                            status_code=400,
                        )
                    pdf_pages_in_request += page_count_for_cost
                    if pdf_pages_in_request > MAX_PDF_PAGES:
                        return await _attachment_json_error_response(
                            _pdf_upload_too_large_payload(
                                f'PDF page total exceeds {MAX_PDF_PAGES} page limit ({pdf_pages_in_request} pages)',
                                pdf_file_count,
                                pdf_pages_in_request,
                                filename=pdf_filename_for_retry if pdf_file_count == 1 else None,
                                current_user=current_user,
                                conversation_id=conversation_id,
                                retry_file_hash=hashlib.sha1(f['data']).hexdigest() if pdf_file_count == 1 else None,
                            ),
                            status_code=400
                        )
                    input_tokens += _estimate_pdf_input_tokens_for_preflight(page_count_for_cost, machine)
            skip_context_pdfs_for_retry = bool(
                pdf_range_requested_preflight
                and pdf_retry_payload
                and pdf_retry_payload.get("allow_skip_context_pdfs")
            )

        if not skip_context_pdfs_for_retry:
            async with conn_ro.execute(
                '''
                SELECT message
                FROM messages
                WHERE conversation_id = ?
                AND date >= ?
                AND message LIKE '%"document_url"%'
                ORDER BY id ASC, date ASC
                ''',
                (conversation_id, start_date)
            ) as cursor:
                context_pdf_rows = await cursor.fetchall()

            context_pdf_pages = 0
            context_pdf_count = 0
            for row in context_pdf_rows:
                try:
                    stored_message = parse_stored_message(custom_unescape(row[0]))
                except Exception as exc:
                    logger.warning(
                        "[process_save_message] Could not estimate stored PDF tokens for conversation_id=%s: %s",
                        conversation_id,
                        exc,
                    )
                    continue
                if not isinstance(stored_message, list):
                    continue
                for block in stored_message:
                    if not isinstance(block, dict) or block.get("type") != "document_url":
                        continue
                    pdf_info = block.get("document_url") or {}
                    try:
                        page_count_for_cost = int(pdf_info.get("pages") or 0)
                    except (TypeError, ValueError):
                        page_count_for_cost = 0
                    context_pdf_count += 1
                    context_pdf_pages += page_count_for_cost
                    if context_pdf_pages + pdf_pages_in_request > MAX_PDF_PAGES:
                        return await _attachment_json_error_response(
                            _pdf_upload_too_large_payload(
                                f'PDF page total exceeds {MAX_PDF_PAGES} page limit ({context_pdf_pages + pdf_pages_in_request} pages including conversation context)',
                                pdf_file_count,
                                pdf_pages_in_request,
                                context_pdf_count=context_pdf_count,
                                context_pages=context_pdf_pages,
                                filename=pdf_filename_for_retry if pdf_file_count == 1 else None,
                                current_user=current_user,
                                conversation_id=conversation_id,
                                retry_file_hash=pdf_file_hash_for_retry,
                            ),
                            status_code=400
                        )
                    input_tokens += _estimate_pdf_input_tokens_for_preflight(page_count_for_cost, machine)

        current_balance = await get_balance(current_user.id)
        model_output_cap, output_limit_fallback_used = _model_output_cap(llm_max_output_tokens)

        # GranSabio early detection
        gransabio_enabled_early = bool(gransabio_enabled_col)

        # Reset stop_signals for non-GranSabio (deferred until after DB query).
        # GranSabio resets inside generate_via_gransabio() after lock acquisition.
        if not gransabio_enabled_early:
            stop_signals[conversation_id] = False

        if gransabio_enabled_early:
            # own_only guard (must be here, not just in save_message wrapper,
            # because Telegram/WhatsApp webhooks call process_save_message directly)
            own_only_error = await check_own_only_gransabio(current_user.id, conversation_id)
            if own_only_error:
                return await _attachment_error_response(own_only_error, status_code=403)
            if files:
                return await _attachment_error_response('File attachments are not supported with GranSabio mode. Send text only.')
            is_byok = False
            from common import get_effective_billing_info
            billing_info = await get_effective_billing_info(current_user.id)
            if billing_info['effective_balance'] <= 0:
                return await _attachment_error_response('Insufficient balance.', status_code=402)
            output_tokens = model_output_cap
            _log_output_limit_decision(
                source="single_gransabio",
                conversation_id=conversation_id,
                llm_id=conversation_llm_id,
                machine=machine,
                model=model,
                max_output_tokens=llm_max_output_tokens,
                fallback_used=output_limit_fallback_used,
                final_limit=int(output_tokens),
                balance_limited=False,
                current_balance=current_balance,
            )
        else:
            # Detect if PDF redirect will happen (GPT/xAI + PDFs present)
            pdf_redirect_will_happen = False
            if machine in ("GPT", "xAI"):
                # Check new files for PDFs
                if files:
                    pdf_redirect_will_happen = any(f['content_type'] == 'application/pdf' for f in files)
                # Check conversation history for existing PDFs
                if not pdf_redirect_will_happen:
                    async with get_db_connection(readonly=True) as conn_pdf:
                        cursor_pdf = await conn_pdf.execute(
                            "SELECT 1 FROM messages WHERE conversation_id = ? AND date >= ? AND message LIKE '%\"document_url\"%' LIMIT 1",
                            (conversation_id, start_date)
                        )
                        pdf_redirect_will_happen = (await cursor_pdf.fetchone()) is not None

            # Determine if this call will use BYOK (user's own API key)
            from common import resolve_api_key_for_provider, get_user_api_key_mode, API_KEY_MODE_SYSTEM_ONLY, BYOK_MIN_BALANCE_PAID_PROMPT
            api_key_mode_preflight = await get_user_api_key_mode(current_user.id)
            preflight_provider = "OpenRouter" if pdf_redirect_will_happen else machine
            preflight_key, preflight_use_system = resolve_api_key_for_provider(
                user_api_keys or {},
                api_key_mode_preflight,
                preflight_provider,
            )
            if (
                pdf_redirect_will_happen
                and not preflight_key
                and preflight_use_system
                and not openrouter_key
            ):
                return await _attachment_error_response('PDF files with this model require OpenRouter integration. Use Claude, Gemini, or select an OpenRouter model directly.')
            if not preflight_key and not preflight_use_system:
                return await _attachment_error_response(f'API key required for {preflight_provider}.')
            is_byok = preflight_key is not None

            if is_byok:
                # BYOK: no API cost to platform. Only need balance for paid prompt markup.
                if prompt_is_paid and current_balance < BYOK_MIN_BALANCE_PAID_PROMPT:
                    return await _attachment_error_response('Insufficient balance for creator markup.', status_code=402)
                # For free prompts with BYOK, no balance needed at all
                output_tokens = model_output_cap
                logger.debug(f"BYOK mode: max_tokens={output_tokens}, Balance: {current_balance}")
                _log_output_limit_decision(
                    source="single_byok",
                    conversation_id=conversation_id,
                    llm_id=conversation_llm_id,
                    machine=machine,
                    model=model,
                    max_output_tokens=llm_max_output_tokens,
                    fallback_used=output_limit_fallback_used,
                    final_limit=int(output_tokens),
                    balance_limited=False,
                    current_balance=current_balance,
                )
            else:
                input_cost = (input_tokens / 1000000) * input_token_cost

                guard_error = assert_billable_claude_system_key(
                    machine=machine,
                    model=model,
                    llm_id=conversation_llm_id,
                    is_byok=is_byok,
                    input_token_cost=input_token_cost,
                    output_token_cost=output_token_cost,
                )
                if guard_error:
                    logger.error(guard_error)
                    return await _attachment_error_response(guard_error, status_code=500)

                if input_token_cost == 0 and output_token_cost == 0:
                    # Free model: no API cost. Only need balance for paid prompt markup.
                    if prompt_is_paid and current_balance < BYOK_MIN_BALANCE_PAID_PROMPT:
                        return await _attachment_error_response('Insufficient balance for creator markup.', status_code=402)
                    output_tokens = model_output_cap
                    total_cost = 0
                    logger.debug(f"Free model: max_tokens={output_tokens}, Balance: {current_balance}")
                    _log_output_limit_decision(
                        source="single_free",
                        conversation_id=conversation_id,
                        llm_id=conversation_llm_id,
                        machine=machine,
                        model=model,
                        max_output_tokens=llm_max_output_tokens,
                        fallback_used=output_limit_fallback_used,
                        final_limit=int(output_tokens),
                        balance_limited=False,
                        current_balance=current_balance,
                    )
                else:
                    # Validate output_token_cost to prevent division by zero
                    if output_token_cost is None or output_token_cost <= 0:
                        logger.error(f"Invalid output_token_cost ({output_token_cost}) for LLM {conversation_llm_id}")
                        return await _attachment_error_response('LLM configuration error: invalid token cost', status_code=500)

                    max_affordable_tokens = int(((current_balance - input_cost) / output_token_cost) * 1000000)
                    output_tokens = int(min(model_output_cap, max(0, max_affordable_tokens)))  # Ensure non-negative
                    if output_tokens < 1:
                        return await _attachment_error_response('Insufficient balance to send the message.', status_code=402)
                    output_cost = (output_tokens / 1000000) * output_token_cost
                    total_cost = input_cost + output_cost

                    if total_cost >= current_balance:
                        return await _attachment_error_response('Insufficient balance to send the message.', status_code=402)

                    logger.debug(f"Total cost: {total_cost}, Balance: {current_balance}")
                    _log_output_limit_decision(
                        source="single_paid",
                        conversation_id=conversation_id,
                        llm_id=conversation_llm_id,
                        machine=machine,
                        model=model,
                        max_output_tokens=llm_max_output_tokens,
                        fallback_used=output_limit_fallback_used,
                        final_limit=int(output_tokens),
                        balance_limited=max_affordable_tokens < model_output_cap,
                        current_balance=current_balance,
                    )

        perf_trace.mark(
            "preflight_done",
            llm_id=conversation_llm_id,
            machine=machine,
            model=model,
            output_tokens=int(output_tokens),
            byok=bool(is_byok),
        )

        warmup_state = {
            "llm_id": conversation_llm_id,
            "effective_prompt_id": effective_prompt_id,
            "active_extension_id": active_extension_id,
            "last_message_id": last_message_id or 0,
            "is_incognito": conversation_incognito,
        }
        warmup_key = _build_warmup_cache_key_from_state(
            warmup_state,
            current_user.id,
            conversation_id,
            mode="single",
        )
        warmup_snapshot = get_warmup_snapshot(warmup_key)
        context_messages_dicts = _copy_warmup_context_messages(warmup_snapshot)
        warmup_hit = context_messages_dicts is not None
        if context_messages_dicts is not None:
            mark_warmup_consumed()
            logger.debug(
                "[process_save_message] Reused warm-up context for conversation_id=%s",
                conversation_id,
            )
        else:
            async with conn_ro.execute(
                '''
                SELECT message, type
                FROM messages
                WHERE conversation_id = ?
                AND date >= ?
                ORDER BY id ASC, date ASC
                ''', (conversation_id, start_date)
            ) as cursor:
                context_messages = await cursor.fetchall()

            context_messages_dicts = [
                {"message": parse_stored_message(custom_unescape(msg[0])), "type": msg[1]}
                for msg in context_messages
            ]
            context_messages_dicts = flatten_multi_ai_context(context_messages_dicts)
        perf_trace.mark(
            "history_loaded",
            warmup_hit=warmup_hit,
            context_messages=len(context_messages_dicts or []),
        )

    if files:
        logger.debug("Has files")
        MAX_IMAGES_PER_MESSAGE = 10     # Reasonable per-message upload limit

        # Classify files by type
        images = []
        pdfs = []
        text_files = []
        for f in files:
            if f['content_type'] == 'application/pdf':
                pdfs.append(f)
            elif f['content_type'].startswith('image/'):
                images.append(f)
            elif is_text_file(f['content_type'], f['filename']):
                text_files.append(f)
            else:
                return await _attachment_error_response(f"Unsupported file type: {f['content_type']}")

        # Validate and process PDFs
        if len(pdfs) > MAX_PDFS_PER_MESSAGE:
            return await _attachment_error_response(f'Maximum {MAX_PDFS_PER_MESSAGE} PDFs per message.')

        pdf_range_requested = pdf_page_start is not None or pdf_page_end is not None
        if pdf_range_requested:
            if len(pdfs) != 1:
                return await _attachment_error_response('Page range retry supports one PDF at a time.')
            if pdf_page_start is None or pdf_page_end is None:
                return await _attachment_error_response('Both PDF page start and end are required.')

        pdf_pages_in_request = 0
        for pdf in pdfs:
            if len(pdf['data']) > MAX_PDF_SIZE_MB * 1024 * 1024:
                return await _attachment_error_response(f'PDF exceeds {MAX_PDF_SIZE_MB}MB limit.')
            try:
                pdf_data = pdf['data']
                filename = pdf['filename'] or 'document.pdf'
                existing_ref = pdf.get('attachment_ref')
                existing_attachment = pdf.get('attachment_record')
                original_pdf_data = pdf_data
                original_pdf_hash = hashlib.sha1(original_pdf_data).hexdigest()
                page_count = validate_pdf(
                    pdf_data,
                    enforce_page_limit=not pdf_range_requested,
                )
                original_page_count = page_count
                if pdf_range_requested:
                    pdf_data, page_count, original_page_count = extract_pdf_page_range(
                        pdf_data,
                        pdf_page_start,
                        pdf_page_end
                    )
                    name_root, name_ext = os.path.splitext(filename)
                    filename = f"{name_root or 'document'}_pages_{pdf_page_start}-{pdf_page_end}{name_ext or '.pdf'}"
                    logger.info(
                        "[process_save_message] PDF page range selected: %s pages %s-%s of %s",
                        filename,
                        pdf_page_start,
                        pdf_page_end,
                        original_page_count,
                    )
                pdf_pages_in_request += page_count
                if pdf_pages_in_request > MAX_PDF_PAGES:
                    return await _attachment_error_response(
                        f'PDF page total exceeds {MAX_PDF_PAGES} page limit ({pdf_pages_in_request} pages)'
                    )
            except ValueError as exc:
                return await _attachment_error_response(str(exc))
            pdf_b64 = base64.b64encode(pdf_data).decode("utf-8")

            # For O1 only: extract text locally (O1 is text-only, can't receive PDF data)
            extracted_text = None
            if machine == "O1":
                extracted_text = extract_pdf_text_local(pdf_data)

            if existing_ref and existing_attachment and not pdf_range_requested:
                pending_attachment_refs.append(existing_ref)
                discardable_attachment_refs.append(existing_ref)
                save_block = attachment_record_to_block(existing_attachment, data=pdf_data)
            else:
                try:
                    pending_pdf = await create_pending_pdf_attachment(
                        user_id=current_user.id,
                        conversation_id=conversation_id,
                        data=pdf_data,
                        filename=filename,
                        page_count=page_count,
                        declared_mime=pdf.get('content_type') or 'application/pdf',
                    )
                except Exception as exc:
                    logger.error("[process_save_message] Could not save PDF attachment: %s", exc)
                    return await _attachment_error_response('Failed to save PDF.', status_code=500)
                pending_attachment_refs.append(pending_pdf.public_id)
                discardable_attachment_refs.append(pending_pdf.public_id)
                if existing_ref:
                    discardable_attachment_refs.append(existing_ref)
                save_block = pending_pdf.block
            _, content_to_send = format_pdf_for_provider(
                machine, "", pdf_b64, filename, page_count, extracted_text
            )
            if pdf_range_requested:
                save_block["document_url"]["retry_source_hash"] = original_pdf_hash
                save_block["document_url"]["retry_source_pages"] = original_page_count
            message_list_to_save.append(save_block)
            if pdf_range_requested:
                message_list_to_send.append({
                    "type": "text",
                    "text": _ranged_pdf_warning_text(
                        filename,
                        page_start=pdf_page_start,
                        page_end=pdf_page_end,
                        source_page_count=original_page_count,
                    ),
                })
            message_list_to_send.append(content_to_send)

        # Validate and process text files
        if text_files:
            if len(text_files) > MAX_TEXT_FILES_PER_MESSAGE:
                return await _attachment_error_response(f'Maximum {MAX_TEXT_FILES_PER_MESSAGE} text files per message')

            for tf in text_files:
                size_mb = len(tf['data']) / (1024 * 1024)
                if size_mb > MAX_TEXT_FILE_SIZE_MB:
                    return await _attachment_error_response(f"Text file '{tf['filename']}' exceeds {MAX_TEXT_FILE_SIZE_MB}MB limit")

                try:
                    text_content = decode_text_file(tf['data'], tf['filename'])
                except ValueError as e:
                    return await _attachment_error_response(str(e))

                filename = tf['filename'] or 'unnamed.txt'
                line_count = text_content.count('\n') + 1
                existing_ref = tf.get('attachment_ref')
                existing_attachment = tf.get('attachment_record')

                if existing_ref and existing_attachment:
                    pending_attachment_refs.append(existing_ref)
                    discardable_attachment_refs.append(existing_ref)
                    message_list_to_save.append(attachment_record_to_block(existing_attachment, data=tf['data']))
                else:
                    try:
                        pending_text = await create_pending_text_attachment(
                            user_id=current_user.id,
                            conversation_id=conversation_id,
                            text_content=text_content,
                            filename=filename,
                            declared_mime=tf.get('content_type') or 'text/plain',
                        )
                    except Exception as exc:
                        logger.error("[process_save_message] Could not save text attachment: %s", exc)
                        return await _attachment_error_response('Failed to save text file.', status_code=500)
                    pending_attachment_refs.append(pending_text.public_id)
                    discardable_attachment_refs.append(pending_text.public_id)
                    message_list_to_save.append(pending_text.block)

                content_to_send = {
                    "type": "text",
                    "text": f"[Content of uploaded file: {filename} ({line_count} lines)]\n\n{text_content}"
                }
                message_list_to_send.append(content_to_send)

        # Validate and process images
        if len(images) > MAX_IMAGES_PER_MESSAGE:
            return await _attachment_error_response(f'Maximum {MAX_IMAGES_PER_MESSAGE} images per message.')

        for file_item in images:
            image_data = file_item['data']
            filename = file_item.get('filename', 'image.jpg')
            existing_ref = file_item.get('attachment_ref')
            existing_attachment = file_item.get('attachment_record')

            if existing_ref and existing_attachment:
                image_media_type = (
                    existing_attachment.get('mime_detected')
                    or existing_attachment.get('declared_mime')
                    or file_item.get('content_type')
                    or 'image/webp'
                )
                w = h = None
                actual_format = image_media_type
            else:
                # Validate + compress in thread (does NOT block event loop)
                try:
                    image_data, image_media_type, w, h, actual_format, was_compressed = await asyncio.to_thread(
                        validate_and_compress_image, image_data, filename
                    )
                except ValueError as e:
                    return await _attachment_error_response(str(e))

            # Post-compression size check
            if len(image_data) > MAX_API_IMAGE_SIZE_MB * 1024 * 1024:
                return await _attachment_error_response('Image is too large. Please use a smaller or lower-resolution image.')

            logger.debug(
                f"[process_save_message] Image processed: {filename}, "
                f"{actual_format}, {w}x{h}, {len(image_data)} bytes, provider={machine}"
            )

            # Base64 encode (fast, stays on event loop)
            image1_data = base64.b64encode(image_data).decode("utf-8")

            if existing_ref and existing_attachment:
                pending_attachment_refs.append(existing_ref)
                discardable_attachment_refs.append(existing_ref)
                save_block = attachment_record_to_block(existing_attachment, data=image_data)
            else:
                try:
                    pending_image = await create_pending_image_attachment(
                        user_id=current_user.id,
                        conversation_id=conversation_id,
                        data=image_data,
                        filename=filename,
                        mime_detected=image_media_type,
                        declared_mime=file_item.get('content_type'),
                        width=w,
                        height=h,
                    )
                except Exception as e:
                    logger.error(f"[process_save_message] Could not save image: {e}")
                    return await _attachment_error_response('Failed to save image.', status_code=500)
                pending_attachment_refs.append(pending_image.public_id)
                discardable_attachment_refs.append(pending_image.public_id)
                save_block = pending_image.block

            # Format for provider (in thread -- xAI may need Pillow JPEG conversion)
            # NOTE: image_media_type here is the Pillow-detected/compression-derived type,
            # NOT the client-reported MIME. This is correct and intentional.
            try:
                _, image_content_to_send = await asyncio.to_thread(
                    format_image_for_provider,
                    machine, "", image1_data, image_media_type
                )
            except ValueError:
                return await _attachment_error_response(f'Unsupported AI provider for images: {machine}')

            message_list_to_save.append(save_block)
            message_list_to_send.append(image_content_to_send)

        if user_message:
            message_content = {
                "type": "text",
                "text": user_message
            }
            message_list_to_save.append(message_content)
            message_list_to_send.append(message_content)

        message_to_save = orjson.dumps(message_list_to_save).decode()
    else:
        logger.debug("NO has file")
        message_to_save = user_message
        message_list_to_send = user_message

    current_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")

    # --- Start of Moderation API Integration ---
    # Per-prompt moderation setting (enable_moderation from PROMPTS table)
    message_flagged = False
    if enable_moderation:
        logger.debug("Enters in moderation api (prompt has moderation enabled)")
        # Prepare input for the moderation API
        if isinstance(message_list_to_send, list):
            moderation_input = []
            for item in message_list_to_send:
                if 'type' in item:
                    if item['type'] == 'text':
                        moderation_input.append({"type": "text", "text": item['text']})
                    elif item['type'] == 'image_url':
                        moderation_input.append({
                            "type": "image_url",
                            "image_url": {
                                "url": item['image_url']['url']
                            }
                        })
                    elif item['type'] == 'image':
                        # Claude format — convert to OpenAI format for moderation
                        source = item.get('source', {})
                        if source.get('type') == 'base64':
                            moderation_input.append({
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:{source['media_type']};base64,{source['data']}"
                                }
                            })
                    elif item['type'] in ('document_url', 'document', 'document_bytes', 'file'):
                        pass  # PDF content cannot be moderated via OpenAI moderation API
        else:
            # message_list_to_send is text
            moderation_input = [{"type": "text", "text": message_list_to_send}]

        try:
            response = openai.moderations.create(
                model="omni-moderation-latest",
                input=moderation_input,
            )
            # Handle the response
            results = response.results
            # Check if any of the inputs are flagged
            for result in results:
                if result.flagged:
                    logger.info("Flagged Message")
                    # Message is flagged
                    message_flagged = True
                    break
            # If none are flagged, proceed
        except Exception as e:
            logger.error(f"[process_save_message] - Error calling moderation API: {e}")
            await discard_pending_attachments(discardable_attachment_refs, "moderation_error")
            return JSONResponse(content={'success': False, 'message': f'Failed to process message: {str(e)}'}, status_code=400)
    # --- End of Moderation API Integration ---

    if enable_moderation:
        logger.info("Moderation check completed")


    # Don't save user message here; we'll do it after getting AI response

    updated_chat_name = None

    if chat_name is None:
        try:
            # Try to load message_to_save as JSON
            message_list = orjson.loads(message_to_save)
            # Find the first element that is type 'text'
            message_text = next((m['text'] for m in message_list if m.get('type') == 'text'), '')
        except (orjson.JSONDecodeError, TypeError, ValueError):
            # If not valid JSON, use message_to_save directly
            message_text = message_to_save

        # Clean text from HTML tags and limit to 25 characters
        message_text = re.sub(r'<[^>]+>', '', message_text)
        message_text = message_text[:25]

        updated_chat_name = message_text

        if not updated_chat_name and message_list_to_save:
            for block in message_list_to_save:
                btype = block.get('type', '')
                if btype == 'text_file':
                    updated_chat_name = block.get('text_file', {}).get('filename', '')[:25]
                    break
                elif btype == 'document_url':
                    updated_chat_name = block.get('document_url', {}).get('filename', '')[:25]
                    break
                elif btype == 'image_url':
                    updated_chat_name = 'Image'
                    break

        # Update conversation name in database
        async with conversation_write_lock(conversation_id):
            async with get_db_connection() as conn_rw:
                transaction_started = False
                try:
                    await conn_rw.execute('BEGIN IMMEDIATE')
                    transaction_started = True
                    await conn_rw.execute(
                        'UPDATE conversations SET chat_name = ? WHERE id = ?',
                        (updated_chat_name, conversation_id)
                    )
                    await conn_rw.commit()
                except sqlite3.OperationalError as exc:
                    if transaction_started:
                        try:
                            await conn_rw.rollback()
                        except Exception:
                            pass
                    if is_lock_error(exc):
                        logger.warning(
                            "[process_save_message] - Could not update chat_name due to lock (conversation_id=%s)",
                            conversation_id,
                        )
                    else:
                        logger.error(f"[process_save_message] - Error updating chat_name: {exc}")
                except Exception as exc:
                    if transaction_started:
                        try:
                            await conn_rw.rollback()
                        except Exception:
                            pass
                    logger.error(f"[process_save_message] - Unexpected error updating chat_name: {exc}")

    async def stream_response():
        yield ": stream-ready\n\n"
        for event in perf_trace.pop_sse():
            yield event

        if updated_chat_name:
            yield f"data: {orjson.dumps({'updated_chat_name': updated_chat_name}).decode()}\n\n"

        current_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")

        # Save the user's message and handle the flagged case
        if message_flagged:
            await discard_pending_attachments(pending_attachment_refs, "moderation_blocked")
            # Save the user's message and the AI's response to the database
            async with conversation_write_lock(conversation_id):
                async with get_db_connection() as conn:
                    transaction_started = False
                    try:
                        await conn.execute("BEGIN IMMEDIATE")
                        transaction_started = True
                        # Save user's message
                        blocked_message = "[Blocked Message]"
                        user_insert_query = '''
                            INSERT INTO messages (conversation_id, user_id, message, type, date)
                            VALUES (?, ?, ?, ?, ?)
                        '''
                        await conn.execute(
                            user_insert_query,
                            (conversation_id, current_user.id, blocked_message, 'user', current_time)
                        )

                        # Prepare the rejection message
                        rejection_message = "*Sorry, but your message has been blocked for violating our usage policies.*"

                        # Save AI's response
                        bot_insert_query = '''
                            INSERT INTO messages
                            (conversation_id, user_id, message, type, date)
                            VALUES (?, ?, ?, ?, ?)
                        '''
                        await conn.execute(
                            bot_insert_query,
                            (conversation_id, current_user.id, rejection_message, 'bot', current_time)
                        )

                        # Update conversation last_activity for sort ordering
                        await conn.execute("UPDATE CONVERSATIONS SET last_activity = CURRENT_TIMESTAMP WHERE id = ?", (conversation_id,))

                        await conn.commit()
                    except Exception as e:
                        if transaction_started:
                            try:
                                await conn.rollback()
                            except Exception:
                                pass
                        logger.error(f"[process_save_message] - Error saving messages to database: {e}")

            try:
                await record_chat_turn(
                    user_id=current_user.id,
                    conversation_id=conversation_id,
                    user_message=blocked_message,
                    assistant_message=rejection_message,
                )
            except Exception:
                logger.warning(
                    "[wellbeing] Failed to record moderated chat turn for conversation_id=%s",
                    conversation_id,
                    exc_info=True,
                )

            # Yield the rejection message
            yield f"data: {orjson.dumps({'content': rejection_message}).decode()}\n\n"
        else:
            # Proceed to get AI response
            try:
                response_stream = get_ai_response(
                    message_list_to_send,
                    context_messages_dicts,
                    conversation_id,
                    machine,
                    model,
                    current_user,
                    request,
                    output_tokens,
                    user_message=message_to_save,
                    input_token_fallback=input_tokens,
                    skip_context_pdfs=skip_context_pdfs_for_retry,
                    thinking_budget_tokens=thinking_budget_tokens,
                    user_api_keys=user_api_keys,
                    llm_id=conversation_llm_id,
                    byok=is_byok,
                    pending_attachment_refs=pending_attachment_refs,
                    perf_trace=perf_trace,
                )
                async for chunk in _stream_with_sse_keepalives(response_stream):
                    yield chunk
            except asyncio.CancelledError:
                logger.info("Client disconnected")
                raise
            finally:
                await discard_pending_attachments(discardable_attachment_refs, "stream_finished")

    return StreamingResponse(stream_response(), media_type='text/event-stream')

async def get_ai_response(
    message,
    context_messages,
    conversation_id,
    machine,
    model,
    current_user,
    request,
    max_tokens,
    temperature=0.7,
    user_message=None,
    input_token_fallback=None,
    skip_context_pdfs: bool = False,
    thinking_budget_tokens=None,
    user_api_keys: Optional[dict] = None,
    llm_id=None,
    save_to_db: bool = True,
    byok: bool = False,
    pending_attachment_refs: Optional[list[str]] = None,
    perf_trace: ChatPerfTrace | None = None,
):
    logger.info(f"*** Enters {machine}")
    logger.debug(f"Parameters received: conversation_id={conversation_id}, model={model}, max_tokens={max_tokens}")
    #logger.info(f"message en get_ai_response: {message}")

    user_id = current_user.id
    logger.debug(f"User ID: {user_id}")
    context_messages = flatten_multi_ai_context(context_messages)
    context_messages = filter_invalid_context_messages(context_messages)
    if perf_trace:
        event = perf_trace.sse(
            "get_ai_response_start",
            machine=machine,
            model=model,
            context_messages=len(context_messages or []),
        )
        if event:
            yield event

    try:
        # Use read-only connection for SELECT queries
        await ensure_conversation_privacy_schema()
        async with get_db_connection(readonly=True) as conn_ro:
            async with conn_ro.cursor() as cursor_ro:
                # Get prompt and other details
                await cursor_ro.execute("""
                    SELECT
                        c.role_id,
                        p.prompt,
                        CASE
                            WHEN c.role_id IS NULL THEN ud.current_prompt_id
                            ELSE c.role_id
                        END AS effective_role_id,
                        u.user_info,
                        ud.current_alter_ego_id,
                        COALESCE(p.disable_web_search, 0) AS disable_web_search,
                        COALESCE(p.force_web_search, 0) AS force_web_search,
                        COALESCE(ud.web_search_enabled, 1) AS user_web_search_enabled,
                        COALESCE(ud.web_search_mode, 'native') AS web_search_mode,
                        COALESCE(p.extensions_enabled, 0) AS extensions_enabled,
                        COALESCE(p.extensions_auto_advance, 0) AS extensions_auto_advance,
                        COALESCE(p.extensions_free_selection, 1) AS extensions_free_selection,
                        c.active_extension_id,
                        pe.name AS extension_name,
                        pe.prompt_text AS extension_prompt_text,
                        COALESCE(p.gransabio_enabled, 0) AS gransabio_enabled,
                        p.gransabio_config AS gransabio_config,
                        COALESCE(c.is_incognito, 0) AS is_incognito
                    FROM CONVERSATIONS c
                    LEFT JOIN PROMPTS p ON c.role_id = p.id
                    LEFT JOIN USER_DETAILS ud ON ud.user_id = ?
                    LEFT JOIN USERS u ON u.id = ?
                    LEFT JOIN PROMPT_EXTENSIONS pe ON c.active_extension_id = pe.id
                    WHERE c.id = ? AND c.user_id = ?
                """, (user_id, user_id, conversation_id, user_id))

                result = await cursor_ro.fetchone()

                if result:
                    (conversation_role_id, prompt, effective_role_id, user_info,
                     current_alter_ego_id, disable_web_search, force_web_search,
                     user_web_search_enabled, web_search_mode, extensions_enabled,
                     extensions_auto_advance, extensions_free_selection,
                     active_extension_id, extension_name,
                     extension_prompt_text,
                     gransabio_enabled, gransabio_config_raw,
                     conversation_incognito) = result
                    conversation_incognito = bool(conversation_incognito)

                    if conversation_role_id is None and effective_role_id:
                        # Update conversation role_id if needed
                        async with get_db_connection() as conn_rw:
                            async with conn_rw.cursor() as cursor_rw:
                                await cursor_rw.execute("UPDATE CONVERSATIONS SET role_id = ? WHERE id = ?", (effective_role_id, conversation_id))
                                await conn_rw.commit()
                        logger.info(f"Conversation updated with role_id: {effective_role_id}")

                        # Get prompt AND reload all prompt-dependent flags for the effective prompt
                        # (fixes pre-existing bug: COALESCE defaults were used instead of actual values)
                        await cursor_ro.execute(
                            """SELECT prompt, gransabio_enabled, gransabio_config,
                                      force_web_search, disable_web_search,
                                      extensions_enabled, extensions_auto_advance,
                                      extensions_free_selection, enable_moderation
                               FROM PROMPTS WHERE id = ?""",
                            (effective_role_id,)
                        )
                        eff_row = await cursor_ro.fetchone()
                        if eff_row:
                            prompt = eff_row[0] or prompt
                            gransabio_enabled = bool(eff_row[1]) if eff_row[1] else False
                            gransabio_config_raw = eff_row[2]
                            force_web_search = bool(eff_row[3]) if eff_row[3] else False
                            disable_web_search = bool(eff_row[4]) if eff_row[4] else False
                            extensions_enabled = bool(eff_row[5]) if eff_row[5] else False
                            extensions_auto_advance = bool(eff_row[6]) if eff_row[6] else False
                            extensions_free_selection = bool(eff_row[7]) if eff_row[7] else True
                        logger.info(f"Effective prompt flags reloaded for role_id={effective_role_id}")

                    # Determine user privilege level for system prompt blocks
                    if await current_user.is_admin:
                        user_level = "admin"
                    elif await current_user.is_user:
                        user_level = "user"
                    else:
                        user_level = "customer"

                    # Check if user has selected an alter-ego
                    if current_alter_ego_id:
                        # Get alter-ego information
                        await cursor_ro.execute("""
                            SELECT name, description
                            FROM USER_ALTER_EGOS
                            WHERE id = ? AND user_id = ?
                        """, (current_alter_ego_id, user_id))
                        alter_ego_row = await cursor_ro.fetchone()
                        if alter_ego_row:
                            alter_ego_name, alter_ego_description = alter_ego_row
                            # Use alter-ego info instead of user info
                            if alter_ego_description:
                                prompt_base = f"User info:\nName: {alter_ego_name}\n{alter_ego_description}\n\n-----\nSystem info:\n{prompt}"
                            else:
                                prompt_base = f"User info:\nName: {alter_ego_name}\n\n-----\nSystem info:\n{prompt}"
                        else:
                            # If alter-ego not found, use user info
                            if user_info:
                                prompt_base = f"User info:\n{user_info}\n\n-----\nSystem info:\n{prompt}"
                            else:
                                prompt_base = prompt
                    else:
                        # No alter-ego selected, use user info
                        if user_info:
                            prompt_base = f"User info:\n{user_info}\n\n-----\nSystem info:\n{prompt}"
                        else:
                            prompt_base = prompt

                    # --- Extensions: inject active extension prompt and level context ---
                    has_extensions = False
                    if extensions_enabled and extension_prompt_text:
                        prompt_base = (
                            f"{prompt_base}\n\n"
                            f"--- ACTIVE EXTENSION: {extension_name} ---\n"
                            f"{extension_prompt_text}\n"
                            f"--- END EXTENSION ---"
                        )

                    if extensions_enabled and extensions_auto_advance:
                        async with get_db_connection(readonly=True) as conn_ext:
                            async with conn_ext.cursor() as cursor_ext:
                                await cursor_ext.execute(
                                    "SELECT id, name, display_order, description FROM PROMPT_EXTENSIONS WHERE prompt_id = ? ORDER BY display_order",
                                    (effective_role_id,)
                                )
                                all_extensions = await cursor_ext.fetchall()
                                if all_extensions:
                                    has_extensions = True
                                    ext_list = "\n".join([
                                        f"  - [{e[0]}] {e[1]}{' (CURRENT)' if e[0] == active_extension_id else ''}: {e[3] or 'No description'}"
                                        for e in all_extensions
                                    ])
                                    extensions_context = (
                                        f"\n\n--- EXTENSION LEVELS ---\n"
                                        f"This conversation has the following levels/phases. You are currently on the one marked (CURRENT).\n"
                                        f"When you determine the current level's objectives are sufficiently covered, "
                                        f"use the advanceExtension tool to transition to the next level.\n"
                                        f"{ext_list}\n"
                                        f"--- END EXTENSION LEVELS ---"
                                    )
                                    prompt_base += extensions_context

                    # --- Watchdog: read config and pending hint ---
                    watchdog_config = None
                    prompt_id = effective_role_id
                    watchdog_hint_block = ""
                    watchdog_hint_active = False
                    watchdog_hint_eval_id = None
                    watchdog_enabled = False
                    raw_watchdog_config = None
                    pre_watchdog_config = None
                    post_watchdog_config = None

                    if effective_role_id:
                        await cursor_ro.execute("SELECT watchdog_config FROM PROMPTS WHERE id = ?", (effective_role_id,))
                        wd_row = await cursor_ro.fetchone()
                        if wd_row and wd_row[0]:
                            try:
                                raw_watchdog_config = orjson.loads(wd_row[0])
                                post_watchdog_config = extract_post_watchdog_config(raw_watchdog_config)
                                pre_watchdog_config = extract_pre_watchdog_config(raw_watchdog_config)
                                watchdog_config = post_watchdog_config  # For passing to streaming functions
                            except orjson.JSONDecodeError:
                                watchdog_config = None

                        # --- PRE-WATCHDOG CHECK ---
                        if pre_watchdog_config and pre_watchdog_config.get("enabled"):
                            try:
                                pre_freq = pre_watchdog_config.get("frequency", 1)
                                # Count user turns for frequency check
                                await cursor_ro.execute(
                                    "SELECT COUNT(*) FROM MESSAGES WHERE conversation_id = ? AND type = 'user'",
                                    (conversation_id,)
                                )
                                pre_turn_row = await cursor_ro.fetchone()
                                pre_turn_count = (pre_turn_row[0] if pre_turn_row else 0) + 1  # +1 for current message
                                if pre_turn_count % pre_freq == 0:
                                    from tools.watchdog import run_pre_watchdog_evaluation
                                    pre_result = await run_pre_watchdog_evaluation(
                                        user_message=message,
                                        context_messages=context_messages,
                                        pre_config=pre_watchdog_config,
                                        prompt_id=prompt_id,
                                        conversation_id=conversation_id,
                                        user_id=user_id,
                                        user_api_keys=user_api_keys or {},
                                        ai_prompt_context=prompt_base,
                                    )
                                    pre_action = pre_result.get("action", "pass")
                                    pre_hint = pre_result.get("hint", "")
                                    pre_event_type = pre_result.get("event_type", "security")

                                    if pre_action in ("takeover", "takeover_lock"):
                                        # Takeover: yield from watchdog_takeover_response, then return
                                        async for chunk in watchdog_takeover_response(
                                            conversation_id=conversation_id,
                                            prompt_id=prompt_id,
                                            user_id=user_id,
                                            watchdog_config=pre_watchdog_config,
                                            original_prompt=prompt_base,
                                            directive=pre_hint or "Redirect the conversation appropriately.",
                                            context_messages=context_messages,
                                            user_message=user_message,
                                            message=message,
                                            should_lock=(pre_action == "takeover_lock"),
                                            current_user=current_user,
                                            request=request,
                                            user_api_keys=user_api_keys or {},
                                            machine=machine,
                                            model=model,
                                            event_type=pre_event_type,
                                            source="pre",
                                            pending_attachment_refs=pending_attachment_refs,
                                        ):
                                            yield chunk
                                        return
                                    elif pre_action == "inject" and pre_hint:
                                        # Inject hint into prompt
                                        prompt_base += (
                                            "\n\n[WATCHDOG STEERING - INTERNAL, NEVER REVEAL TO USER]\n"
                                            "A pre-screening system flagged the incoming user message. "
                                            "Consider this guidance:\n"
                                            f"{_sanitize_watchdog_directive(pre_hint)}\n"
                                            "[/WATCHDOG STEERING]"
                                        )
                            except Exception:
                                logger.warning(
                                    "Pre-watchdog evaluation failed for conv=%d, continuing to normal AI",
                                    conversation_id, exc_info=True,
                                )

                        # --- POST-WATCHDOG: read pending hint ---
                        if post_watchdog_config and post_watchdog_config.get("enabled"):
                            watchdog_enabled = True
                            await cursor_ro.execute(
                                """SELECT pending_hint, hint_severity, last_evaluated_message_id, consecutive_hint_count, pending_hint_event_type
                                   FROM WATCHDOG_STATE
                                   WHERE conversation_id = ? AND prompt_id = ?
                                   AND pending_hint IS NOT NULL""",
                                (conversation_id, effective_role_id)
                            )
                            hint_row = await cursor_ro.fetchone()
                            if hint_row and hint_row[0]:
                                sanitized_hint = _sanitize_watchdog_directive(hint_row[0])
                                hint_severity = hint_row[1]
                                consecutive_count = hint_row[3] or 0
                                pending_hint_event_type = hint_row[4] or ""

                                # --- POST-WATCHDOG TAKEOVER CHECK ---
                                if (post_watchdog_config.get("can_takeover")
                                        and hint_severity == "redirect"
                                        and consecutive_count >= post_watchdog_config.get("takeover_threshold", 5)):
                                    from tools.watchdog import LOCKABLE_EVENT_TYPES
                                    can_lock_post = (
                                        post_watchdog_config.get("can_lock", False)
                                        and pending_hint_event_type in LOCKABLE_EVENT_TYPES
                                    )
                                    if can_lock_post:
                                        # Fetch real analysis for judge
                                        analysis_cursor = await cursor_ro.execute(
                                            """SELECT analysis FROM WATCHDOG_EVENTS
                                               WHERE conversation_id = ? AND bot_message_id = ? AND source = 'post'
                                               LIMIT 1""",
                                            (conversation_id, hint_row[2])
                                        )
                                        analysis_row = await analysis_cursor.fetchone()
                                        real_analysis = analysis_row[0] if analysis_row else f"Takeover escalation after {consecutive_count} ignored hints"

                                        from tools.watchdog import _judge_lock_decision
                                        approve, judge_reason, _ = await _judge_lock_decision(
                                            conversation_id, effective_role_id, pending_hint_event_type, real_analysis
                                        )
                                        if not approve:
                                            can_lock_post = False
                                            logger.info("Lock Judge rejected takeover lock for conv=%d: %s", conversation_id, judge_reason)
                                    async for chunk in watchdog_takeover_response(
                                        conversation_id=conversation_id,
                                        prompt_id=prompt_id,
                                        user_id=user_id,
                                        watchdog_config=post_watchdog_config,
                                        original_prompt=prompt_base,
                                        directive=sanitized_hint,
                                        context_messages=context_messages,
                                        user_message=user_message,
                                        message=message,
                                        should_lock=can_lock_post,
                                        current_user=current_user,
                                        request=request,
                                        user_api_keys=user_api_keys or {},
                                        machine=machine,
                                        model=model,
                                        event_type=pending_hint_event_type,
                                        source="post",
                                        pending_attachment_refs=pending_attachment_refs,
                                    ):
                                        yield chunk
                                    return

                                # Normal hint injection (existing behavior)
                                watchdog_hint_block = _build_escalated_hint_block(
                                    sanitized_hint, hint_severity, consecutive_count
                                )
                                watchdog_hint_active = True
                                watchdog_hint_eval_id = hint_row[2]

                    # Assemble full_prompt via global system prompt blocks
                    blocks = await get_effective_blocks()
                    variables = {
                        "user_level": user_level,
                        "current_datetime_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                    }
                    full_prompt = assemble_system_prompt(blocks, variables, prompt_base,
                                                        watchdog_enabled, watchdog_hint_block)
                    if perf_trace:
                        event = perf_trace.sse("memory_context_start")
                        if event:
                            yield event
                    memory_decision = await _resolve_memory_context(
                        full_prompt,
                        user_id=user_id,
                        conversation_id=conversation_id,
                        message=message,
                        prompt_id=prompt_id,
                        incognito=conversation_incognito,
                    )
                    if perf_trace:
                        event = perf_trace.sse(
                            "memory_context_done",
                            memory_provider=memory_decision.provider,
                            memory_active=memory_decision.active,
                            memory_reason=memory_decision.reason,
                            prompt_chars=len(memory_decision.full_prompt or ""),
                        )
                        if event:
                            yield event
                    full_prompt = memory_decision.full_prompt
                    context_messages = _context_messages_for_memory_provider(
                        context_messages,
                        memory_decision,
                    )
                    memory_health = get_user_memory_health_snapshot(
                        memory_decision.provider,
                        enabled=(
                            memory_decision.provider != "none"
                            and memory_decision.reason != "disabled_by_user"
                        ),
                    )
                    if should_surface_memory_health(memory_health):
                        yield f"data: {orjson.dumps({'type': 'memory_health', 'memory_health': memory_health}).decode()}\n\n"
                    if memory_decision.provider == "none":
                        context_messages = await apply_no_memory_context_budget(
                            context_messages,
                            llm_id=llm_id,
                            prompt_id=prompt_id,
                            full_prompt=full_prompt,
                            current_message=message,
                        )
                    if skip_context_pdfs:
                        context_messages = _drop_pdf_blocks_from_context(context_messages)
                    if perf_trace:
                        event = perf_trace.sse(
                            "prompt_context_ready",
                            prompt_chars=len(full_prompt or ""),
                            context_messages=len(context_messages or []),
                            memory_provider=memory_decision.provider,
                            web_search_mode=web_search_mode,
                        )
                        if event:
                            yield event

                else:
                    logger.error(f"[get_ai_response] - No conversation found with id {conversation_id} for user {user_id}")
                    return

                # ========================================
                # PDF redirect: GPT/xAI -> OpenRouter
                # ========================================
                # When PDFs are present, redirect GPT/xAI calls through OpenRouter
                # BEFORE message formatting so the entire pipeline uses OpenRouter format
                has_pdfs_in_message = any(
                    isinstance(block, dict) and block.get("type") in ("file", "document_bytes")
                    for block in (message if isinstance(message, list) else [])
                )
                has_pdfs_in_context = any(
                    isinstance(block, dict) and block.get("type") == "document_url"
                    for msg in context_messages
                    for block in (msg.get("message", []) if isinstance(msg.get("message"), list) else [])
                )
                current_pdf_error_metadata = _extract_pdf_metadata_from_saved_message(user_message)
                context_pdf_error_metadata = _extract_pdf_metadata_from_context_messages(context_messages)
                pdf_error_metadata = _merge_pdf_error_metadata(
                    current_pdf_error_metadata,
                    context_pdf_error_metadata,
                )
                if pdf_error_metadata:
                    current_pdf_count = int((current_pdf_error_metadata or {}).get("pdf_count") or 0)
                    context_pdf_count = int((context_pdf_error_metadata or {}).get("pdf_count") or 0)
                    pdf_error_metadata["current_pdf_count"] = current_pdf_count
                    pdf_error_metadata["context_pdf_count"] = context_pdf_count
                    pdf_error_metadata["range_retry_available"] = current_pdf_count == 1
                    if current_pdf_count == 1:
                        pdf_error_metadata["retry_filename"] = current_pdf_error_metadata.get("filename")
                        pdf_error_metadata["retry_pages"] = current_pdf_error_metadata.get("pages")
                        pdf_error_metadata["retry_file_hash"] = (
                            current_pdf_error_metadata.get("retry_source_hash")
                            or current_pdf_error_metadata.get("file_hash")
                        )
                        pdf_error_metadata["retry_source_pages"] = (
                            current_pdf_error_metadata.get("retry_source_pages")
                            or current_pdf_error_metadata.get("pages")
                        )

                pdf_redirect_active = False

                if (has_pdfs_in_message or has_pdfs_in_context) and machine in ("GPT", "xAI"):
                    pdf_redirect_active = True
                    original_machine = machine
                    original_model = model

                    machine = "OpenRouter"
                    openrouter_model_id = OPENROUTER_MODEL_MAP.get(
                        original_model,
                        f"openai/{original_model}" if original_machine == "GPT" else f"x-ai/{original_model}"
                    )
                    # Keep original model for billing, pass remapped model via api_model
                    # (api_model is set after kwargs construction below)

                    # Web search: Responses API features not available via OpenRouter
                    if web_search_mode == 'native':
                        web_search_mode = None

                    logger.info(f"PDF redirect: {original_machine}/{original_model} -> OpenRouter/{openrouter_model_id}")

                # Prepare messages in correct format for LLM
                api_messages = []

                if machine == "Gemini":
                    # Build structured Gemini contents (system prompt sent via config)
                    gemini_contents = []
                    for msg in context_messages:
                        role = "user" if msg['type'] == 'user' else "model"
                        msg_content = msg['message']
                        if isinstance(msg_content, list):
                            parts = []
                            for block in msg_content:
                                if block.get("type") == "text":
                                    parts.append(genai_types.Part.from_text(text=block["text"]))
                                elif block.get("type") == "image_url":
                                    hydrated_block = await hydrate_image_for_context(
                                        block,
                                        "Gemini",
                                        current_user,
                                        conversation_id=conversation_id,
                                    )
                                    if hydrated_block is None:
                                        continue
                                    token_url = hydrated_block["image_url"]["url"]
                                    if token_url.startswith("data:"):
                                        header, b64_data = token_url.split(",", 1)
                                        mime = header.split(":")[1].split(";")[0]
                                        parts.append(genai_types.Part.from_bytes(data=base64.b64decode(b64_data), mime_type=mime))
                                    else:
                                        base_url = block["image_url"]["url"]
                                        mime = "image/webp"
                                        if base_url.lower().endswith(".png"):
                                            mime = "image/png"
                                        elif base_url.lower().endswith(".jpg") or base_url.lower().endswith(".jpeg"):
                                            mime = "image/jpeg"
                                        parts.append(genai_types.Part.from_uri(file_uri=token_url, mime_type=mime))
                                elif block.get("type") == "document_url":
                                    hydrated = await hydrate_pdf_for_context(block, "Gemini", current_user, conversation_id=conversation_id)
                                    if hydrated is not None:
                                        parts.append(genai_types.Part.from_bytes(
                                            data=base64.b64decode(hydrated["data"]),
                                            mime_type="application/pdf"
                                        ))
                                elif block.get("type") == "text_file":
                                    parts.append(genai_types.Part.from_text(text=await text_file_block_to_text_for_context(block, current_user, conversation_id=conversation_id)))
                            if parts:
                                gemini_contents.append(genai_types.Content(role=role, parts=parts))
                        else:
                            gemini_contents.append(genai_types.Content(role=role, parts=[genai_types.Part.from_text(text=str(msg_content))]))

                    # Add new user message
                    if isinstance(message, list):
                        parts = []
                        for block in message:
                            if block.get("type") == "text":
                                parts.append(genai_types.Part.from_text(text=block["text"]))
                            elif block.get("type") == "image_url":
                                url = block["image_url"]["url"]
                                if url.startswith("data:"):
                                    # New message: base64 data URL -> use from_bytes
                                    header, b64_data = url.split(",", 1)
                                    mime = header.split(":")[1].split(";")[0]
                                    parts.append(genai_types.Part.from_bytes(data=base64.b64decode(b64_data), mime_type=mime))
                                else:
                                    # Token URL -> use from_uri
                                    mime = "image/webp"
                                    if url.lower().endswith(".png"):
                                        mime = "image/png"
                                    elif url.lower().endswith(".jpg") or url.lower().endswith(".jpeg"):
                                        mime = "image/jpeg"
                                    parts.append(genai_types.Part.from_uri(file_uri=url, mime_type=mime))
                            elif block.get("type") == "document_bytes":
                                parts.append(genai_types.Part.from_bytes(
                                    data=base64.b64decode(block["data"]),
                                    mime_type=block["mime_type"]
                                ))
                        gemini_contents.append(genai_types.Content(role="user", parts=parts))
                    else:
                        gemini_contents.append(genai_types.Content(role="user", parts=[genai_types.Part.from_text(text=str(message))]))

                    api_messages = gemini_contents

                elif machine == "O1":
                    combined_message_content = f"{full_prompt}\n\n{message}"
                    for msg in context_messages:
                        msg_content = msg['message']
                        if isinstance(msg_content, list):
                            text_parts = []
                            for block in msg_content:
                                if isinstance(block, dict):
                                    if block.get("type") == "text":
                                        text_parts.append(block["text"])
                                    elif block.get("type") == "document_url":
                                        hydrated = await hydrate_pdf_for_context(block, "O1", current_user, conversation_id=conversation_id)
                                        if hydrated is not None:
                                            text_parts.append(hydrated["text"])
                                    elif block.get("type") == "text_file":
                                        text_parts.append(await text_file_block_to_text_for_context(block, current_user, conversation_id=conversation_id))
                                    elif block.get("type") == "image_url":
                                        text_parts.append("[An image was shared]")
                            msg_content = "\n".join(text_parts) if text_parts else str(msg_content)
                        api_messages.append({"role": "user" if msg['type'] == 'user' else 'assistant', "content": msg_content})
                    api_messages.append({"role": "user", "content": combined_message_content})

                else:
                    # Existing logic for GPT and Claude
                    for i, msg in enumerate(context_messages):
                        content = msg['message']
                        if isinstance(content, list):
                            # Hydrate image and PDF blocks with fresh data
                            hydrated = []
                            for block in content:
                                if block.get("type") == "image_url":
                                    result = await hydrate_image_for_context(
                                        block,
                                        machine,
                                        current_user,
                                        conversation_id=conversation_id,
                                    )
                                    if result is not None:
                                        hydrated.append(result)
                                elif block.get("type") == "document_url":
                                    result = await hydrate_pdf_for_context(block, machine, current_user, conversation_id=conversation_id)
                                    if result is not None:
                                        hydrated.append(result)
                                elif block.get("type") == "text_file":
                                    hydrated.append({"type": "text", "text": await text_file_block_to_text_for_context(block, current_user, conversation_id=conversation_id)})
                                else:
                                    hydrated.append(block)
                            api_messages.append({"role": "user" if msg['type'] == 'user' else "assistant", "content": hydrated})
                        else:
                            if i == len(context_messages) - 2 and msg['type'] == 'user' and machine == "Claude":
                                content = [{"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}]
                            else:
                                content = [{"type": "text", "text": content}]
                            api_messages.append({"role": "user" if msg['type'] == 'user' else "assistant", "content": content})
                    # Add new user message
                    if machine == "Claude":
                        if isinstance(message, list):
                            api_messages.append({
                                "role": "user",
                                "content": message
                            })
                        else:
                            api_messages.append({
                                "role": "user",
                                "content": [{"type": "text", "text": message, "cache_control": {"type": "ephemeral"}}]
                            })
                    else:
                        if isinstance(message, list):
                            api_messages.append({
                                "role": "user",
                                "content": message
                            })
                        else:
                            api_messages.append({
                                "role": "user",
                                "content": [{"type": "text", "text": message}]
                            })

                #logger.debug(f"get_ai_response -> Prepared messages for API: {api_messages}")
                if perf_trace:
                    event = perf_trace.sse(
                        "provider_messages_ready",
                        machine=machine,
                        api_messages=len(api_messages or []),
                    )
                    if event:
                        yield event

                # =============================================================
                # GranSabio routing - intercept before normal provider routing
                # =============================================================
                if gransabio_enabled:
                    from gransabio_service import generate_via_gransabio
                    from gransabio_config import get_gransabio_config

                    # Runtime fail-fast: catch incompatible flags
                    if force_web_search:
                        yield f"data: {orjson.dumps({'error': 'Configuration conflict: force_web_search is incompatible with GranSabio. Disable one of them in the prompt settings.'}).decode()}\n\n"
                        return

                    admin_config = await get_gransabio_config()

                    if admin_config.get("gransabio_enabled") != "true":
                        yield f"data: {orjson.dumps({'error': 'GranSabio is disabled globally by admin.'}).decode()}\n\n"
                        return

                    # Parse gransabio_config with error handling
                    try:
                        prompt_config = orjson.loads(gransabio_config_raw) if gransabio_config_raw else {}
                        if not isinstance(prompt_config, dict):
                            prompt_config = {}
                    except orjson.JSONDecodeError:
                        logger.error(f"Invalid GranSabio config JSON for prompt {prompt_id}")
                        yield f"data: {orjson.dumps({'error': 'Invalid GranSabio configuration for this prompt (corrupted JSON). Contact admin.'}).decode()}\n\n"
                        return

                    async for chunk in generate_via_gransabio(
                        message=message, context_messages=context_messages,
                        conversation_id=conversation_id, current_user=current_user,
                        full_prompt=full_prompt, prompt_config=prompt_config,
                        admin_config=admin_config, user_message=user_message,
                        save_to_db=save_to_db, llm_id=llm_id, prompt_id=prompt_id,
                        byok=False, watchdog_config=watchdog_config,
                        watchdog_hint_active=watchdog_hint_active,
                        watchdog_hint_eval_id=watchdog_hint_eval_id,
                        max_tokens=max_tokens,
                    ):
                        yield chunk
                    return  # Don't fall through -- generate_via_gransabio handles its own DB saving

                # =============================================================
                # Native Tool Calling - Tools are passed directly to each AI
                # No more semantic router intermediate step
                # =============================================================

                # Select appropriate API function based on machine
                # Use global 'tools' list which contains all registered tools
                # (generateImage, generateVideo, QR codes, perplexity, time, etc.)

                # Filter tools based on web search settings
                # Priority: disable_web_search > force_web_search > user preference > mode selection
                filtered_tools = tools
                if disable_web_search:
                    # Prompt forces web search OFF - remove all search tools
                    filtered_tools = [t for t in tools if t['function']['name'] != 'query_perplexity']
                    web_search_mode = None
                elif force_web_search:
                    # Prompt forces web search ON - ensure search is active regardless of user pref
                    if not web_search_mode or web_search_mode == 'none':
                        web_search_mode = 'native'
                    if web_search_mode == 'native':
                        if machine in NATIVE_SEARCH_PROVIDERS:
                            filtered_tools = [t for t in tools if t['function']['name'] != 'query_perplexity']
                        else:
                            web_search_mode = 'perplexity'
                elif not user_web_search_enabled:
                    # User disabled web search - remove all search tools
                    filtered_tools = [t for t in tools if t['function']['name'] != 'query_perplexity']
                    web_search_mode = None
                elif web_search_mode == 'native':
                    if machine in NATIVE_SEARCH_PROVIDERS:
                        filtered_tools = [t for t in tools if t['function']['name'] != 'query_perplexity']
                    else:
                        web_search_mode = 'perplexity'
                # else: 'perplexity' mode - keep query_perplexity (current behavior)

                # Filter advanceExtension tool: only include when extensions + auto_advance are active
                if not (extensions_enabled and extensions_auto_advance and has_extensions):
                    filtered_tools = [t for t in filtered_tools if t.get("function", {}).get("name") != "advanceExtension"]

                if machine == "Gemini":
                    api_func = call_gemini_api
                    provider_tools = tools_for_gemini(filtered_tools)
                elif machine == "O1":
                    api_func = call_o1_api
                    provider_tools = None  # O1 models don't support tools yet
                elif machine == "GPT":
                    api_func = call_gpt_responses_api
                    provider_tools = tools_for_openai_responses(filtered_tools, web_search_mode)
                elif machine == "Claude":
                    api_func = call_claude_api
                    provider_tools = tools_for_claude(filtered_tools)
                elif machine == "xAI":
                    api_func = call_xai_responses_api
                    provider_tools = tools_for_xai_responses(filtered_tools, web_search_mode)
                elif machine == "OpenRouter":
                    api_func = call_openrouter_api
                    provider_tools = tools_for_openai(filtered_tools)
                elif machine == "MiniMax":
                    api_func = call_minimax_api
                    provider_tools = tools_for_openai(filtered_tools)
                elif machine == "Kimi":
                    api_func = call_kimi_api
                    provider_tools = tools_for_openai(filtered_tools)
                else:
                    raise ValueError(f"Unknown machine type: {machine}")

                # Build kwargs for API call
                kwargs = {
                    "messages": api_messages,
                    "model": model,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    "prompt": full_prompt,
                    "conversation_id": conversation_id,
                    "current_user": current_user,
                    "request": request,
                    "user_message": user_message,
                    "input_token_fallback": input_token_fallback,
                    "pdf_error_metadata": pdf_error_metadata,
                    "prompt_id": prompt_id,
                    "watchdog_config": watchdog_config,
                    "watchdog_hint_active": watchdog_hint_active,
                    "watchdog_hint_eval_id": watchdog_hint_eval_id,
                    "llm_id": llm_id,
                    "save_to_db": save_to_db,
                    "web_search_mode": web_search_mode,
                    "byok": byok,
                    "pending_attachment_refs": pending_attachment_refs,
                }

                # Add tools if available for this provider
                if provider_tools:
                    kwargs["tools"] = provider_tools
                if perf_trace:
                    event = perf_trace.sse(
                        "tools_ready",
                        machine=machine,
                        provider_tool_count=len(provider_tools or []),
                        web_search_mode=web_search_mode,
                    )
                    if event:
                        yield event

                if machine == "Claude" and thinking_budget_tokens:
                    kwargs["thinking_budget_tokens"] = thinking_budget_tokens

                # PDF redirect: pass remapped model while preserving BYOK when
                # the user provided an OpenRouter key.
                if pdf_redirect_active:
                    kwargs["api_model"] = openrouter_model_id

                # ===========================================
                # Resolve which API key to use based on mode
                # ===========================================
                from common import resolve_api_key_for_provider, get_user_api_key_mode

                api_key_mode = await get_user_api_key_mode(current_user.id)
                resolved_key, use_system = resolve_api_key_for_provider(
                    user_api_keys or {},
                    api_key_mode,
                    machine
                )

                if resolved_key:
                    kwargs["user_api_key"] = resolved_key
                    kwargs["byok"] = True
                    logger.info(f"Using user's custom {machine} API key")
                elif use_system:
                    kwargs["byok"] = False
                    logger.info(f"Using system {machine} API key")
                else:
                    # own_only mode without configured key - should have been caught earlier
                    # but double-check here for security
                    logger.error(f"User {current_user.id} in own_only mode without API key for {machine}")
                    yield f"data: {orjson.dumps({'error': 'API key required', 'action': 'configure_api_keys'}).decode()}\n\n"
                    return
                if perf_trace:
                    event = perf_trace.sse(
                        "provider_call_start",
                        machine=machine,
                        model=model,
                        byok=bool(resolved_key),
                        use_system_key=bool(use_system and not resolved_key),
                    )
                    if event:
                        yield event
                    if machine == "GPT":
                        kwargs["perf_trace"] = perf_trace

                # Call the API and collect response
                # Watch for tool_call in the response stream
                collected_tool_call = None
                pre_tool_content = ""  # Text Claude generated before calling the tool

                _IMAGE_DL_ERROR_PATTERNS = ("unable to download", "could not download", "error downloading", "failed to fetch image")
                _retried_base64 = False

                def _is_perf_trace_chunk(c):
                    if not isinstance(c, str) or not c.startswith("data: "):
                        return False
                    try:
                        chunk_data = orjson.loads(c[6:].strip())
                    except orjson.JSONDecodeError:
                        return False
                    return chunk_data.get("type") == "perf_trace"

                # Peek at first chunk to detect image download errors
                first_chunk = None
                prefetched_chunks = []
                api_stream = api_func(**kwargs)
                async for chunk in api_stream:
                    if _is_perf_trace_chunk(chunk):
                        prefetched_chunks.append(chunk)
                        continue
                    first_chunk = chunk
                    break

                # Check if first chunk indicates an image download error
                if first_chunk and isinstance(first_chunk, str) and first_chunk.startswith("data: "):
                    try:
                        data = orjson.loads(first_chunk[6:].strip())
                        error_msg = str(data.get("error", "")).lower()
                        if any(p in error_msg for p in _IMAGE_DL_ERROR_PATTERNS):
                            _retried_base64 = True
                            logger.warning("[get_ai_response] Image download error detected, retrying with base64")
                            api_messages_b64 = await _format_messages_for_provider(
                                context_messages, message, full_prompt, machine,
                                current_user=current_user, force_base64=True,
                                conversation_id=conversation_id,
                            )
                            kwargs["messages"] = api_messages_b64
                            first_chunk = None
                            prefetched_chunks = []
                            api_stream = api_func(**kwargs)
                            async for chunk in api_stream:
                                if _is_perf_trace_chunk(chunk):
                                    prefetched_chunks.append(chunk)
                                    continue
                                first_chunk = chunk
                                break
                    except (orjson.JSONDecodeError, KeyError):
                        pass

                # Process first_chunk through the same logic as remaining chunks
                def _is_tool_call_chunk(c):
                    return isinstance(c, str) and 'tool_call' in c and 'tool_call_pending' not in c

                def _is_tool_pending_chunk(c):
                    return isinstance(c, str) and 'tool_call_pending' in c

                for chunk in (prefetched_chunks + ([first_chunk] if first_chunk is not None else [])):
                    if _is_tool_call_chunk(chunk):
                        try:
                            if chunk.startswith("data: "):
                                chunk_data = orjson.loads(chunk[6:].strip())
                                if 'tool_call' in chunk_data:
                                    collected_tool_call = chunk_data['tool_call']
                                    pre_tool_content = chunk_data.get('pre_tool_content', '')
                                    logger.info(f"[get_ai_response] - Collected tool_call: {collected_tool_call['name']}, pre_tool_content length: {len(pre_tool_content)}")
                                    continue
                        except (orjson.JSONDecodeError, KeyError) as e:
                            logger.debug(f"[get_ai_response] - Could not parse chunk as tool_call: {e}")
                    if _is_tool_pending_chunk(chunk):
                        continue
                    yield chunk

                async for chunk in api_stream:
                    # Check if this chunk contains a tool_call
                    if _is_tool_call_chunk(chunk):
                        try:
                            # Parse the SSE data format
                            if chunk.startswith("data: "):
                                chunk_data = orjson.loads(chunk[6:].strip())
                                if 'tool_call' in chunk_data:
                                    collected_tool_call = chunk_data['tool_call']
                                    pre_tool_content = chunk_data.get('pre_tool_content', '')
                                    logger.info(f"[get_ai_response] - Collected tool_call: {collected_tool_call['name']}, pre_tool_content length: {len(pre_tool_content)}")
                                    continue  # Don't yield the tool_call to frontend
                        except (orjson.JSONDecodeError, KeyError) as e:
                            logger.debug(f"[get_ai_response] - Could not parse chunk as tool_call: {e}")

                    # Skip the tool_call_pending marker
                    if _is_tool_pending_chunk(chunk):
                        continue

                    # Yield normal content to frontend
                    yield chunk

                # If a tool call was collected, handle it
                if collected_tool_call:
                    function_name = collected_tool_call['name']
                    function_arguments = collected_tool_call['arguments']

                    logger.info(f"[get_ai_response] - Processing tool call: {function_name}")

                    if function_name == "query_perplexity":
                        # === SECOND PASS FLOW ===
                        # The AI decided to search the web. We call Perplexity silently,
                        # feed the results back to the AI, and let it formulate its own answer.
                        from tools.perplexity import get_perplexity_result

                        query = function_arguments.get('query', '') if isinstance(function_arguments, dict) else str(function_arguments)

                        if not query.strip():
                            logger.warning("[get_ai_response] - Perplexity second pass: empty query")
                            yield f"data: {orjson.dumps({'error': 'Web search query was empty'}).decode()}\n\n"
                            return

                        logger.debug(f"[get_ai_response] - Perplexity second pass for query: {query[:100]}")

                        # 1. Tell the frontend we're searching
                        yield f"data: {orjson.dumps({'searching': True}).decode()}\n\n"

                        try:
                            # 2. Get Perplexity results (non-streaming)
                            perplexity_result = await get_perplexity_result(query)
                            logger.info(f"[get_ai_response] - Perplexity result length: {len(perplexity_result)}")
                        except Exception as e:
                            logger.error(f"[get_ai_response] - Perplexity second pass failed: {e}")
                            yield f"data: {orjson.dumps({'searching': False}).decode()}\n\n"
                            yield f"data: {orjson.dumps({'error': f'Web search failed: {e}'}).decode()}\n\n"
                            return

                        # 3. Build tool response messages (appends to api_messages in-place)
                        _build_tool_response_messages(api_messages, collected_tool_call, perplexity_result, machine)

                        # 4. Build second_kwargs: same as kwargs but without tools (prevent loops)
                        #    Also clear web_search_mode so provider functions don't add native search tools
                        second_kwargs = dict(kwargs)
                        second_kwargs.pop("tools", None)
                        second_kwargs["web_search_mode"] = None
                        second_kwargs["messages"] = api_messages

                        # System prompt dedup for Chat Completions providers:
                        # call_llm_api does messages.insert(0, {"role": "system", ...}) mutating the list.
                        # The first call already inserted it, so pop it before the second call.
                        # GPT and xAI excluded: their Responses API functions don't mutate the caller's message list.
                        if machine in ("OpenRouter", "MiniMax", "Kimi"):
                            if api_messages and isinstance(api_messages[0], dict) and api_messages[0].get("role") == "system":
                                api_messages.pop(0)

                        # 5. Tell frontend search is done, AI response about to stream
                        yield f"data: {orjson.dumps({'searching': False}).decode()}\n\n"

                        # 6. Stream the second pass response from the original AI
                        async for chunk in api_func(**second_kwargs):
                            yield chunk
                        # api_func handles save_to_db internally
                        return

                    elif function_name == "lookup_platform_help":
                        # === PLATFORM HELP SECOND PASS ===
                        from tools.platform_help import lookup_platform_help, log_help_query

                        query = function_arguments.get('query', '') if isinstance(function_arguments, dict) else str(function_arguments)
                        category = function_arguments.get('category') if isinstance(function_arguments, dict) else None

                        if not query.strip():
                            yield f"data: {orjson.dumps({'error': 'Platform help query was empty'}).decode()}\n\n"
                            return

                        logger.info(f"[get_ai_response] - Platform help lookup (category={category})")
                        logger.debug(f"[get_ai_response] - Platform help query: {query[:100]}")

                        # 1. Determine user role for article filtering (live from DB, not JWT cache)
                        role_cursor = await conn_ro.execute(
                            "SELECT role_id FROM USERS WHERE id = ?", (current_user.id,)
                        )
                        user_row = await role_cursor.fetchone()
                        live_role_id = user_row['role_id'] if user_row else None

                        roles_cursor = await conn_ro.execute("SELECT id, role_name FROM USER_ROLES")
                        role_rows = await roles_cursor.fetchall()
                        role_map = {r['id']: r['role_name'].lower() for r in role_rows}
                        user_role = role_map.get(live_role_id, 'customer')

                        # 2. Query the KB
                        help_result, results_count, top_article = await lookup_platform_help(conn_ro, query, category, user_role)

                        # 3. Log the query for gap analysis (fire-and-forget)
                        asyncio.create_task(log_help_query(
                            query, user_message, category, results_count, top_article,
                            prompt_id
                        ))

                        # 4. Build tool response messages
                        # For Claude: use plain text instead of tool_use/tool_result blocks.
                        # Claude's API requires tools defined when tool_use blocks are in messages,
                        # but keeping tools causes Claude to re-call the tool instead of answering.
                        # Plain text avoids both problems.
                        if machine == "Claude":
                            api_messages.append({"role": "assistant", "content": [{"type": "text", "text": f"I looked up platform help information."}]})
                            api_messages.append({"role": "user", "content": [{"type": "text", "text": f"Here is the platform help result. Use it to answer my question:\n\n{help_result}"}]})
                        else:
                            _build_tool_response_messages(api_messages, collected_tool_call, help_result, machine)

                        # 5. Second pass without tools
                        second_kwargs = dict(kwargs)
                        second_kwargs.pop("tools", None)
                        second_kwargs["web_search_mode"] = None
                        second_kwargs["messages"] = api_messages

                        if machine in ("OpenRouter", "MiniMax", "Kimi"):
                            if api_messages and isinstance(api_messages[0], dict) and api_messages[0].get("role") == "system":
                                api_messages.pop(0)

                        # 6. Stream the AI's answer incorporating KB results
                        async for chunk in api_func(**second_kwargs):
                            yield chunk
                        return

                    else:
                        # === EXISTING FLOW for all other tools ===
                        input_tokens = estimate_message_tokens(message)
                        total_tokens = input_tokens + max_tokens

                        async for chunk in handle_function_call(
                            function_name,
                            function_arguments,
                            api_messages,
                            model,
                            temperature,
                            max_tokens,
                            pre_tool_content,  # Text Claude generated before tool call
                            conversation_id,
                            current_user,
                            request,
                            input_tokens,
                            max_tokens,
                            total_tokens,
                            None,
                            user_id,
                            machine,
                            full_prompt,
                            user_message,
                            input_token_fallback=input_token_fallback,
                            user_api_key=resolved_key,
                            api_model=openrouter_model_id if pdf_redirect_active else None,
                            pdf_error_metadata=pdf_error_metadata,
                            prompt_id=prompt_id,
                            watchdog_config=watchdog_config,
                            watchdog_hint_active=watchdog_hint_active,
                            watchdog_hint_eval_id=watchdog_hint_eval_id,
                            llm_id=llm_id,
                            byok=byok,
                            thinking_budget_tokens=thinking_budget_tokens,
                            pending_attachment_refs=pending_attachment_refs,
                        ):
                            yield chunk

    except ValueError as ve:
        logger.error(f"[get_ai_response] - Database connection error: {ve}")
    except Exception as e:
        logger.error(f"[get_ai_response] - Error getting response from {machine}: {e}")
        logger.error(f"[get_ai_response] - Traceback: {traceback.format_exc()}")
        yield None
