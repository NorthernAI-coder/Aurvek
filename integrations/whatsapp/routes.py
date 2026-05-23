import os
import time

import aiohttp
import orjson
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from auth import get_user_from_phone_number
from clients import async_twilio, twilio_validator
from common import (
    PRIMARY_APP_DOMAIN,
    cache_directory,
    validate_twilio_media_url,
)
from database import get_db_connection
from integrations.conversations import (
    can_use_platform,
    change_response_mode,
    create_new_platform_conversation,
    ensure_platform_conversation,
    get_chats_list,
    set_external_conversation,
)
from integrations.media import transcribe_external_request
from integrations.whatsapp.service import get_phone_user_not_found
from log_config import logger
from prompt_access import get_user_accessible_prompts
from prompts import can_user_access_prompt
from save_images import get_or_generate_img_token
from tools.tts import handle_tts_request, insert_tts_break

from ai_runtime.messages import process_save_message


router = APIRouter()


# Whatsapp

# WhatsApp rate limiting (in-memory)
_whatsapp_rate_limits = {}  # {phone_number: [timestamp, ...]}
_whatsapp_rate_limit_notices = {}  # {phone_number: last_notice_timestamp}
WHATSAPP_RATE_LIMIT_PER_USER = int(os.getenv("WHATSAPP_RATE_LIMIT_PER_USER", "20"))  # messages per minute
WHATSAPP_RATE_LIMIT_GLOBAL = int(os.getenv("WHATSAPP_RATE_LIMIT_GLOBAL", "200"))  # messages per minute
_whatsapp_global_timestamps = []

@router.post("/whatsapp")
async def whatsapp_webhook(request: Request):
    if async_twilio is None:
        logger.warning("WhatsApp webhook called but Twilio is not configured")
        return Response(content="<Response></Response>", media_type="application/xml", status_code=200)

    # Security: Validate Twilio signature to prevent spoofed requests
    if twilio_validator:
        signature = request.headers.get("X-Twilio-Signature", "")
        # Reconstruct the full URL that Twilio signed.
        # Use PRIMARY_APP_DOMAIN to build the canonical URL, avoiding proxy header
        # fragility (works with nginx, Cloudflare Tunnel, or any other reverse proxy).
        url = f"https://{PRIMARY_APP_DOMAIN}{request.url.path}"
        if request.url.query:
            url += f"?{request.url.query}"

        form_data = await request.form()
        # Convert form data to dict for validation
        params = {key: form_data[key] for key in form_data}

        is_valid = twilio_validator.validate(url, params, signature)
        if not is_valid:
            logger.warning(f"Invalid Twilio signature from {request.client.host}")
            raise HTTPException(status_code=403, detail="Invalid Twilio signature")

        data = form_data
    else:
        logger.warning("Twilio validator not configured - signature validation skipped")
        data = await request.form()

    message_body = (data.get("Body") or "").strip()
    from_number = data.get("From")
    to_number = data.get("To")
    # Collect all media from Twilio (supports up to 10 items)
    media_items = []
    for i in range(10):
        m_url = data.get(f"MediaUrl{i}")
        m_type = data.get(f"MediaContentType{i}")
        if m_url:
            media_items.append({"url": m_url, "type": m_type or ""})
        else:
            break
    media_url = media_items[0]["url"] if media_items else None
    media_type = media_items[0]["type"] if media_items else None

    # Idempotency: deduplicate Twilio retries by MessageSid
    message_sid = data.get("MessageSid")
    if message_sid:
        async with get_db_connection() as conn:
            # Cleanup old entries (older than 24h)
            await conn.execute("DELETE FROM WHATSAPP_PROCESSED_MESSAGES WHERE created_at < datetime('now', '-1 day')")
            # Attempt insert - if already exists, skip processing
            cursor = await conn.execute("INSERT OR IGNORE INTO WHATSAPP_PROCESSED_MESSAGES (message_sid) VALUES (?)", (message_sid,))
            await conn.commit()
            if cursor.rowcount == 0:
                logger.info(f"Duplicate WhatsApp message detected, skipping: {message_sid}")
                return Response(content="<Response></Response>", media_type="application/xml", status_code=200)

    # Rate limiting per phone number and global
    now = time.time()

    # Global rate limit
    _whatsapp_global_timestamps[:] = [t for t in _whatsapp_global_timestamps if now - t < 60]
    if len(_whatsapp_global_timestamps) >= WHATSAPP_RATE_LIMIT_GLOBAL:
        logger.warning("WhatsApp global rate limit exceeded")
        return Response(content="<Response></Response>", media_type="application/xml", status_code=200)
    _whatsapp_global_timestamps.append(now)

    # Per-user rate limit
    from_number_rl = data.get("From", "")
    if from_number_rl:
        user_timestamps = _whatsapp_rate_limits.get(from_number_rl, [])
        user_timestamps = [t for t in user_timestamps if now - t < 60]
        _whatsapp_rate_limits[from_number_rl] = user_timestamps

        if len(user_timestamps) >= WHATSAPP_RATE_LIMIT_PER_USER:
            # Send cooldown notice max once per 5 minutes
            last_notice = _whatsapp_rate_limit_notices.get(from_number_rl, 0)
            if now - last_notice > 300:
                _whatsapp_rate_limit_notices[from_number_rl] = now
                to_number_rl = data.get("To", "")
                try:
                    await async_twilio.send_message(
                        body="You're sending too many messages. Please wait a moment before sending more.",
                        from_=to_number_rl,
                        to=from_number_rl
                    )
                except Exception:
                    pass
            return Response(content="<Response></Response>", media_type="application/xml", status_code=200)

        user_timestamps.append(now)
        _whatsapp_rate_limits[from_number_rl] = user_timestamps

    # Security: Validate all media URLs to prevent SSRF attacks
    valid_media_items = []
    for item in media_items:
        if validate_twilio_media_url(item["url"]):
            valid_media_items.append(item)
        else:
            logger.warning(f"Rejected invalid media URL in WhatsApp webhook: {item['url'][:100]}")
    media_items = valid_media_items
    media_url = media_items[0]["url"] if media_items else None
    media_type = media_items[0]["type"] if media_items else None

    try:
        current_user = await get_user_from_phone_number(from_number)
        if current_user is None:
            response_text = get_phone_user_not_found()
            message = await async_twilio.send_message(
                body=response_text,
                from_=to_number,
                to=from_number
            )
            return {"status": "success", "message": "User not found"}
        logger.debug(f"WhatsApp message from user: {current_user.username}")

        if not current_user.is_enabled:
            return {"status": "success"}

        message_lower = message_body.lower()

        if message_lower == "!help":
            help_text = (
                "*Available commands:*\n\n"
                "!help - Show this help message\n"
                "!text - Switch to text response mode\n"
                "!voice - Switch to voice response mode\n"
                "!chats - List your recent conversations\n"
                "!set <id> [platform] - Switch to a conversation\n"
                "!prompt list - List available prompts\n"
                "!prompt <name or id> - Switch to a different prompt\n"
                "!new - Start a new conversation\n"
            )
            await async_twilio.send_message(body=help_text, from_=to_number, to=from_number)
            return {"status": "success", "message": "Help sent"}

        async with get_db_connection(readonly=True) as conn:
            cursor = await conn.cursor()
            await cursor.execute(
                "SELECT external_platforms FROM USER_DETAILS WHERE user_id = ?",
                (current_user.id,),
            )
            result = await cursor.fetchone()
            platforms = orjson.loads(result[0]) if result and result[0] else {}
            whatsapp_data = platforms.get("whatsapp") or {}
            if not isinstance(whatsapp_data, dict):
                whatsapp_data = {}
            is_first_whatsapp = not whatsapp_data

        async with get_db_connection(readonly=True) as conn:
            cursor = await conn.cursor()
            ok, _, err_msg = await can_use_platform(current_user.id, "whatsapp", cursor)
            if not ok:
                await async_twilio.send_message(
                    body=err_msg,
                    from_=to_number,
                    to=from_number,
                )
                return {"status": "success"}

        if message_lower == "!chats":
            async with get_db_connection(readonly=True) as conn:
                chats_message = await get_chats_list(
                    current_user.id,
                    "whatsapp",
                    conn,
                    markdown=True,
                )
            await async_twilio.send_message(body=chats_message, from_=to_number, to=from_number)
            return {"status": "success"}

        if message_lower == "!set" or message_lower.startswith("!set "):
            parts = message_body[4:].strip().split() if len(message_body) > 4 else []
            if not parts or len(parts) > 2:
                await async_twilio.send_message(
                    body="Usage: !set <conversation_id> [whatsapp|telegram]",
                    from_=to_number,
                    to=from_number,
                )
                return {"status": "success"}

            raw_id = parts[0]
            clean_id = raw_id[1:] if raw_id.startswith("#") else raw_id
            if not clean_id.isdigit() or int(clean_id) <= 0:
                await async_twilio.send_message(
                    body="Usage: !set <conversation_id> [whatsapp|telegram]",
                    from_=to_number,
                    to=from_number,
                )
                return {"status": "success"}

            target_platform = "whatsapp"
            if len(parts) == 2:
                platform_map = {
                    "whatsapp": "whatsapp",
                    "wa": "whatsapp",
                    "telegram": "telegram",
                    "tg": "telegram",
                }
                target_platform = platform_map.get(parts[1].lower())
                if target_platform is None:
                    await async_twilio.send_message(
                        body="Invalid platform. Use: whatsapp (wa) or telegram (tg).",
                        from_=to_number,
                        to=from_number,
                    )
                    return {"status": "success"}

            result = await set_external_conversation(
                current_user.id,
                int(clean_id),
                target_platform,
                "whatsapp",
            )
            await async_twilio.send_message(
                body=result["message"],
                from_=to_number,
                to=from_number,
            )
            return {"status": "success"}

        if message_lower in ["text_mode", "text mode", "!text"]:
            async with get_db_connection() as conn:
                confirmation_message = await change_response_mode(current_user.id, "text", conn=conn)
            await async_twilio.send_message(
                body=confirmation_message,
                from_=to_number,
                to=from_number,
            )
            return {"status": "success", "message": confirmation_message}

        if message_lower in ["voice_mode", "voice mode", "!voice"]:
            async with get_db_connection() as conn:
                confirmation_message = await change_response_mode(current_user.id, "voice", conn=conn)
            await async_twilio.send_message(
                body=confirmation_message,
                from_=to_number,
                to=from_number,
            )
            return {"status": "success", "message": confirmation_message}

        if message_lower == "!prompt list":
            async with get_db_connection(readonly=True) as conn:
                cursor = await conn.cursor()
                ud_cursor = await conn.execute(
                    "SELECT all_prompts_access, public_prompts_access, category_access FROM USER_DETAILS WHERE user_id = ?",
                    (current_user.id,),
                )
                ud_row = await ud_cursor.fetchone()
                prompts_list = await get_user_accessible_prompts(
                    current_user,
                    cursor,
                    all_prompts_access=ud_row[0] if ud_row else False,
                    public_prompts_access=ud_row[1] if ud_row else False,
                    category_access=ud_row[2] if ud_row else None,
                )

            if not prompts_list:
                await async_twilio.send_message(body="No prompts available.", from_=to_number, to=from_number)
                return {"status": "success"}

            prompt_lines = [f"*{p['id']}* - {p['name']}" for p in prompts_list[:20]]
            msg = "*Available prompts:*\n\n" + "\n".join(prompt_lines)
            if len(prompts_list) > 20:
                msg += f"\n\n_...and {len(prompts_list) - 20} more_"
            msg += "\n\nUse *!prompt <id or name>* to switch."

            await async_twilio.send_message(body=msg, from_=to_number, to=from_number)
            return {"status": "success"}

        if message_lower == "!new":
            await create_new_platform_conversation(current_user.id, "whatsapp", current_user)
            await async_twilio.send_message(
                body="New conversation started. Previous conversation saved and accessible from the web.",
                from_=to_number,
                to=from_number,
            )
            return {"status": "success"}

        whatsapp_data, created_binding = await ensure_platform_conversation(
            current_user.id,
            "whatsapp",
            current_user,
        )
        if created_binding and is_first_whatsapp:
            try:
                async with get_db_connection(readonly=True) as config_conn:
                    config_cursor = await config_conn.execute(
                        "SELECT value FROM SYSTEM_CONFIG WHERE key = 'whatsapp_welcome_message'"
                    )
                    row = await config_cursor.fetchone()
                welcome_template = row[0] if row else None
                if not welcome_template:
                    welcome_template = os.getenv("WHATSAPP_WELCOME_MESSAGE", "")
                if welcome_template:
                    welcome_msg = welcome_template.replace("{username}", current_user.username)
                    await async_twilio.send_message(
                        body=welcome_msg,
                        from_=to_number,
                        to=from_number,
                    )
            except Exception as welcome_err:
                logger.error(f"Failed to send WhatsApp welcome message: {welcome_err}")

        conversation_id = whatsapp_data["conversation_id"]
        answer_mode = whatsapp_data.get("answer", "text")

        logger.debug(f"WhatsApp response mode: {answer_mode}")
        logger.debug("WhatsApp message body received")

        async with get_db_connection(readonly=True) as conn:
            cursor = await conn.cursor()
            await cursor.execute(
                "SELECT locked FROM CONVERSATIONS WHERE id = ? AND user_id = ?",
                (conversation_id, current_user.id),
            )
            lock_row = await cursor.fetchone()
            if not lock_row:
                await async_twilio.send_message(
                    body="Conversation not found. Send !new to start a fresh one.",
                    from_=to_number,
                    to=from_number,
                )
                return {"status": "success", "message": "Conversation not found"}
            if lock_row[0]:
                async with get_db_connection() as log_conn:
                    await log_conn.execute(
                        "INSERT INTO WHATSAPP_LOG (user_id, phone_number, direction, message_type, response_mode) VALUES (?, ?, 'in', 'text', ?)",
                        (current_user.id, from_number, answer_mode),
                    )
                    await log_conn.commit()
                logger.info(
                    f"WhatsApp message blocked: conversation {conversation_id} locked for user {current_user.id}"
                )
                await async_twilio.send_message(
                    body="This conversation is locked. Send !new to start a new one.",
                    from_=to_number,
                    to=from_number,
                )
                return {"status": "success", "message": "Conversation locked"}

        async with get_db_connection(readonly=True) as conn:
            cursor = await conn.cursor()
            await cursor.execute(
                "SELECT external_platforms FROM USER_DETAILS WHERE user_id = ?",
                (current_user.id,),
            )
            row = await cursor.fetchone()
            if row and row[0]:
                fresh_platforms = orjson.loads(row[0])
                fresh_conv_id = (fresh_platforms.get("whatsapp", {}) or {}).get("conversation_id")
                if fresh_conv_id and fresh_conv_id != conversation_id:
                    conversation_id = fresh_conv_id
                    await cursor.execute(
                        "SELECT locked FROM CONVERSATIONS WHERE id = ? AND user_id = ?",
                        (conversation_id, current_user.id),
                    )
                    lock_row = await cursor.fetchone()
                    if not lock_row:
                        await async_twilio.send_message(
                            body="Conversation not found. Send !new to start a fresh one.",
                            from_=to_number,
                            to=from_number,
                        )
                        return {"status": "success", "message": "Conversation not found"}
                    if lock_row[0]:
                        await async_twilio.send_message(
                            body="This conversation is locked. Send !new to start a new one.",
                            from_=to_number,
                            to=from_number,
                        )
                        return {"status": "success", "message": "Conversation locked"}

        if message_lower.startswith("!prompt ") and message_lower != "!prompt list":
            prompt_query = message_body[8:].strip()

            async with get_db_connection() as conn:
                cursor = await conn.cursor()
                target_prompt = None
                if prompt_query.isdigit():
                    p_cursor = await conn.execute(
                        "SELECT id, name FROM PROMPTS WHERE id = ?",
                        (int(prompt_query),),
                    )
                    target_prompt = await p_cursor.fetchone()

                if not target_prompt:
                    p_cursor = await conn.execute(
                        "SELECT id, name FROM PROMPTS WHERE LOWER(name) = LOWER(?)",
                        (prompt_query,),
                    )
                    target_prompt = await p_cursor.fetchone()

                if not target_prompt:
                    p_cursor = await conn.execute(
                        "SELECT id, name FROM PROMPTS WHERE LOWER(name) LIKE LOWER(?)",
                        (f"%{prompt_query}%",),
                    )
                    target_prompt = await p_cursor.fetchone()

                if not target_prompt:
                    await async_twilio.send_message(
                        body=f"Prompt not found: '{prompt_query}'. Use *!prompt list* to see available prompts.",
                        from_=to_number,
                        to=from_number,
                    )
                    return {"status": "success"}

                if not await can_user_access_prompt(current_user, target_prompt[0], cursor):
                    await async_twilio.send_message(
                        body="You don't have access to this prompt.",
                        from_=to_number,
                        to=from_number,
                    )
                    return {"status": "success"}

                update_cursor = await cursor.execute(
                    """
                    UPDATE CONVERSATIONS
                    SET role_id = ?
                    WHERE id = ? AND user_id = ? AND COALESCE(locked, 0) = 0
                    """,
                    (target_prompt[0], conversation_id, current_user.id),
                )
                if update_cursor.rowcount == 0:
                    await conn.rollback()
                    await async_twilio.send_message(
                        body="This conversation is locked. Send !new to start a new one.",
                        from_=to_number,
                        to=from_number,
                    )
                    return {"status": "success"}

                await conn.commit()

                await async_twilio.send_message(
                    body=f"Switched to prompt: *{target_prompt[1]}*",
                    from_=to_number,
                    to=from_number,
                )
                return {"status": "success"}

        transcribed_text = ""
        file_dict = None
        files_list = []  # Support for multiple files

        if media_items:
            for media_item in media_items:
                m_url = media_item["url"]
                m_type = media_item["type"]

                if "audio" in m_type:
                    try:
                        transcribed_text = await transcribe_external_request(request=request, audio=None, user_id=current_user.id, media_url=m_url)
                    except Exception as e:
                        logger.error(f"Error transcribing audio: {e}")
                        await async_twilio.send_message(
                            body="Sorry, there was a problem processing the audio. Please try sending your message as text.",
                            from_=to_number,
                            to=from_number
                        )
                        return {"status": "error", "message": "Error transcribing audio"}
                elif "image" in m_type:
                    try:
                        async with aiohttp.ClientSession() as session:
                            async with session.get(m_url) as resp:
                                if resp.status == 200:
                                    img_data = await resp.read()
                                    files_list.append({
                                        'data': img_data,
                                        'content_type': m_type,
                                        'filename': f"image_{len(files_list)}.jpg"
                                    })
                    except Exception as e:
                        logger.error(f"Error downloading image: {e}")
                elif "pdf" in m_type or "document" in m_type:
                    await async_twilio.send_message(
                        body="Sorry, document attachments are not supported yet. Please send text or images.",
                        from_=to_number,
                        to=from_number
                    )
                    return {"status": "success", "message": "Unsupported media type"}
                else:
                    logger.warning(f"Unsupported WhatsApp media type: {m_type}")
                    await async_twilio.send_message(
                        body=f"Sorry, this media type ({m_type}) is not supported. Please send text, images, or audio.",
                        from_=to_number,
                        to=from_number
                    )
                    return {"status": "success", "message": "Unsupported media type"}

            file_dict = files_list[0] if files_list else None

        user_message = transcribed_text if transcribed_text else message_body
        if not user_message and not file_dict:
            return {"status": "success", "message": "Empty message ignored"}

        # Log incoming WhatsApp message
        msg_type = "audio" if transcribed_text else ("image" if file_dict else "text")
        async with get_db_connection() as log_conn:
            await log_conn.execute(
                "INSERT INTO WHATSAPP_LOG (user_id, phone_number, direction, message_type, response_mode) VALUES (?, ?, 'in', ?, ?)",
                (current_user.id, from_number, msg_type, answer_mode)
            )
            await log_conn.commit()

        def parse_structured_whatsapp_content(payload):
            if isinstance(payload, dict):
                return payload if isinstance(payload.get("type"), str) else None

            if isinstance(payload, list) and payload:
                if all(isinstance(item, dict) and isinstance(item.get("type"), str) for item in payload):
                    return payload

            return None

        async def send_whatsapp_text_message(text: str):
            if not text or not text.strip():
                return

            if answer_mode == "voice":
                logger.debug("WhatsApp voice response mode")
                logger.debug(f"WhatsApp conversation id: {conversation_id}")
                audio_path, error = await handle_tts_request(None, {"text": text, "author": "bot", "conversationId": conversation_id}, current_user, is_whatsapp=True, tts_context="external")

                if error:
                    error_message = "Sorry, there was a problem generating the voice message. I will send you the message as text."
                    await async_twilio.send_message(
                        body=f"{error_message}\n\n{text}",
                        from_=to_number,
                        to=from_number
                    )
                    logger.error(f"Error generating voice message: {error}")
                    return

                token = await get_or_generate_img_token(current_user)
                relative_path = audio_path[len(str(cache_directory)):]
                media_url = f"{request.url.scheme}://{request.url.hostname}/get-audio{relative_path}?token={token}"
                media_url = media_url.replace('\\', '/')
                logger.debug("Generated TTS media URL for WhatsApp")
                await async_twilio.send_message(
                    media_url=[media_url],
                    from_=to_number,
                    to=from_number
                )
                return

            logger.debug("WhatsApp text response mode")
            await async_twilio.send_message(
                body=text,
                from_=to_number,
                to=from_number
            )

        async def send_whatsapp_text(text: str, *, chunk_long_text: bool = False):
            if not text or not text.strip():
                return

            if chunk_long_text and len(text) >= 900:
                text_chunks = await insert_tts_break(text, min_length=700, max_length=900, look_ahead=100)
                for text_chunk in text_chunks:
                    await send_whatsapp_text_message(text_chunk)
                return

            await send_whatsapp_text_message(text)

        async def send_whatsapp_block(block) -> bool:
            if not isinstance(block, dict):
                return False

            block_type = block.get('type')
            if block_type == 'text':
                await send_whatsapp_text(block.get('text', ''), chunk_long_text=True)
                return True

            if block_type == 'image_url':
                logger.debug("Processing image URL for WhatsApp delivery")
                image_data = block.get('image_url', {})
                image_url = image_data.get('url')
                alt_text = image_data.get('alt', 'Image')
                if not image_url:
                    return False
                logger.debug("Sending image via Twilio")
                await async_twilio.send_message(
                    body=f"Image: {alt_text}",
                    media_url=[image_url],
                    from_=to_number,
                    to=from_number
                )
                return True

            if block_type == 'audio_url':
                logger.debug("Processing audio URL for WhatsApp delivery")
                audio_data = block.get('audio_url', {})
                audio_url = audio_data.get('url')
                if not audio_url:
                    return False
                logger.debug("Sending audio via Twilio")
                await async_twilio.send_message(
                    media_url=[audio_url],
                    from_=to_number,
                    to=from_number
                )
                return True

            return False

        async def send_chunks(chunks):
            for chunk in chunks:
                json_content = parse_structured_whatsapp_content(chunk)
                if isinstance(json_content, list):
                    handled_any = False
                    for block in json_content:
                        block_handled = await send_whatsapp_block(block)
                        handled_any = handled_any or block_handled
                    if handled_any:
                        continue
                elif json_content and await send_whatsapp_block(json_content):
                    continue

                await send_whatsapp_text(chunk if isinstance(chunk, str) else orjson.dumps(chunk).decode())

        # Prepare files list with all downloaded images
        files = files_list if files_list else None

        # --- GranSabio check: if enabled, process in background ---
        async with get_db_connection(readonly=True) as conn_gs:
            gs_row = await conn_gs.execute(
                "SELECT COALESCE(ep.gransabio_enabled, 0) FROM CONVERSATIONS c "
                "LEFT JOIN USER_DETAILS ud ON ud.user_id = c.user_id "
                "LEFT JOIN PROMPTS ep ON ep.id = COALESCE(c.role_id, ud.current_prompt_id) "
                "WHERE c.id = ?", (conversation_id,)
            )
            gs_result = await gs_row.fetchone()
        is_gransabio = bool((gs_result or [0])[0])

        if is_gransabio:
            # Reject file attachments for GranSabio (text only)
            if files_list:
                await async_twilio.send_message(
                    body="File attachments are not supported with GranSabio mode. Please send text only.",
                    from_=to_number, to=from_number,
                )
                return JSONResponse(content={"status": "success"})

            from gransabio_config import GRANSABIO_USE_DRAMATIQ, get_gransabio_config
            from gransabio_service import (
                merge_gransabio_config, estimate_pipeline_timeout,
                load_prompt_gransabio_config, process_gransabio_external,
            )

            admin_config = await get_gransabio_config()
            if admin_config.get("gransabio_enabled") != "true":
                await async_twilio.send_message(
                    body="GranSabio is currently disabled.",
                    from_=to_number, to=from_number,
                )
                return JSONResponse(content={"status": "success"})

            prompt_config = await load_prompt_gransabio_config(conversation_id)
            merged_config = merge_gransabio_config(prompt_config, admin_config)
            estimated_timeout = estimate_pipeline_timeout(merged_config)

            platform_context = {
                "from_number": from_number,
                "to_number": to_number,
                "answer_mode": answer_mode,
                "conversation_id": conversation_id,
            }

            if GRANSABIO_USE_DRAMATIQ:
                from tasks import gransabio_external_task
                gransabio_external_task.send_with_options(
                    args=(
                        conversation_id,
                        orjson.dumps(user_message).decode(),
                        "whatsapp",
                        orjson.dumps(platform_context).decode(),
                        estimated_timeout,
                    ),
                    time_limit=28_800_000,
                )
            else:
                import asyncio
                asyncio.create_task(
                    process_gransabio_external(
                        conversation_id=conversation_id,
                        user_id=current_user.id,
                        user_message=user_message,
                        platform="whatsapp",
                        platform_context=platform_context,
                        estimated_timeout=estimated_timeout,
                    )
                )

            return JSONResponse(content={"status": "success"})

        # --- Normal (non-GranSabio) path continues below ---
        # Use process_save_message directly to avoid Form() object issues
        response = await process_save_message(
            request=request,
            conversation_id=conversation_id,
            current_user=current_user,
            text_plain=user_message,
            files=files,
            full_response=False,
            is_whatsapp=True,
            thinking_budget_tokens=None  # Explicitly set to None
        )

        if isinstance(response, StreamingResponse):
            accumulated_text = ""
            last_full_content = ""

            async def flush_accumulated_text():
                nonlocal accumulated_text
                if not accumulated_text.strip():
                    accumulated_text = ""
                    return

                if len(accumulated_text) >= 900:
                    chunks = await insert_tts_break(accumulated_text, min_length=700, max_length=900, look_ahead=100)
                    await send_chunks(chunks)
                else:
                    await send_chunks([accumulated_text])
                accumulated_text = ""

            async for chunk in response.body_iterator:
                chunk_str = chunk.decode('utf-8') if isinstance(chunk, bytes) else chunk

                for line in chunk_str.split('\n'):
                    if line[:5] == 'data:':
                        try:
                            data = orjson.loads(line[5:].strip())
                            content = data.get('content', '')

                            if isinstance(content, (list, dict)):
                                await flush_accumulated_text()
                                await send_chunks([content])
                            else:
                                accumulated_text += content

                                if len(accumulated_text) >= 1400:
                                    chunks = await insert_tts_break(accumulated_text, min_length=900, max_length=1200, look_ahead=100)
                                    await send_chunks(chunks[:-1])
                                    accumulated_text = chunks[-1]

                            if content:
                                last_full_content = accumulated_text
                        except orjson.JSONDecodeError:
                            logger.error(f"Error decoding JSON: {line}")

            await flush_accumulated_text()

        else:
            # Handle non-streaming responses (rate limit, insufficient balance, etc.)
            status_code = response.status_code if hasattr(response, 'status_code') else 500
            error_messages = {
                429: "You've sent too many messages. Please wait a moment.",
                402: "Insufficient balance. Please top up your account.",
                403: "This conversation is not available.",
            }
            user_msg = error_messages.get(status_code, "Sorry, your message could not be processed. Please try again.")
            await async_twilio.send_message(
                body=user_msg,
                from_=to_number,
                to=from_number
            )

        # Log outgoing WhatsApp response
        async with get_db_connection() as log_conn:
            await log_conn.execute(
                "INSERT INTO WHATSAPP_LOG (user_id, phone_number, direction, message_type, response_mode) VALUES (?, ?, 'out', 'text', ?)",
                (current_user.id, from_number, answer_mode)
            )
            await log_conn.commit()

    except Exception as e:
        logger.error(f"!!! Error in whatsapp_webhook: {e}")
        try:
            error_message = "Sorry, an error occurred while processing your message. Please try again later."
            await async_twilio.send_message(
                body=error_message,
                from_=to_number,
                to=from_number
            )
        except Exception as send_err:
            logger.error(f"Failed to send WhatsApp error message: {send_err}")
        return JSONResponse(content={"status": "error"}, status_code=200)

    return {"status": "success"}
