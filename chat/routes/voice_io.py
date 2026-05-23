import asyncio
import hashlib
import io
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
import httpx
import jwt
from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect, Form
from fastapi.responses import FileResponse, JSONResponse, Response
from pydub import AudioSegment

from auth import (
    get_current_user,
    get_current_user_from_websocket,
    get_user_by_username,
    unauthenticated_response,
)
from captcha_service import get_captcha_config
from clients import deepgram, stt_engine, stt_fallback_enabled
from common import (
    Cost,
    GOOGLE_CLIENT_ID,
    READONLY_MODE,
    SECRET_KEY,
    cache_directory,
    decode_jwt_cached,
    deduct_balance,
    has_sufficient_balance,
    record_daily_usage,
    templates,
    validate_path_within_directory,
)
from database import DB_MAX_RETRIES, DB_RETRY_DELAY_BASE, get_db_connection, is_lock_error
from log_config import logger
from models import ConnectionManager, User
from rediscfg import redis_client
from tasks import generate_mp3_task, generate_pdf_task
from tools.tts import (
    get_file_path,
    get_voice_code_from_conversation,
    handle_tts_request,
    process_text_for_tts,
)
from tools.tts_load_balancer import get_elevenlabs_key

router = APIRouter()
manager = ConnectionManager()
DEFAULT_STT_LANGUAGE = "es"


async def require_conversation_access(conversation_id: int, current_user: User) -> int:
    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.execute(
            "SELECT user_id FROM CONVERSATIONS WHERE id = ?",
            (conversation_id,),
        )
        row = await cursor.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Conversation not found")

    owner_id = int(row["user_id"])
    if owner_id != int(current_user.id) and not await current_user.is_admin:
        raise HTTPException(status_code=403, detail="Access denied")
    return owner_id


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        current_user = await get_current_user_from_websocket(websocket)
        if current_user is None:
            await websocket.close(code=4401, reason="Session expired")
            return

        if READONLY_MODE:
            await websocket.close(code=1013, reason="Read-only mode active")
            return

        while True:
            import orjson

            message = await websocket.receive_text()
            data = orjson.loads(message)
            action = data.get("action")

            if action == "start_tts":
                if manager.active_connections[websocket]["task"]:
                    manager.active_connections[websocket]["task"].cancel()
                task = asyncio.create_task(
                    handle_tts_request(websocket, data, current_user, tts_context="webchat")
                )
                manager.active_connections[websocket]["task"] = task

            elif action == "start_tts_ws":
                if manager.active_connections[websocket]["task"]:
                    manager.active_connections[websocket]["task"].cancel()
                task = asyncio.create_task(
                    handle_tts_request(
                        websocket,
                        data,
                        current_user,
                        ws_mode=True,
                        tts_context="webchat",
                    )
                )
                manager.active_connections[websocket]["task"] = task

            elif action == "stop":
                if manager.active_connections[websocket]["task"]:
                    manager.active_connections[websocket]["task"].cancel()
                await manager.send_json(websocket, {"action": "stopped"})

    except WebSocketDisconnect:
        manager.disconnect(websocket)


@router.post("/api/get-tts-audio")
async def get_tts_audio_endpoint(request: Request, current_user: User = Depends(get_current_user)):
    data = await request.json()
    text = data.get("text")
    conversation_id = data.get("conversationId")
    author = data.get("author", "bot")

    if conversation_id is None:
        return JSONResponse(status_code=400, content={"error": "conversationId not provided"})

    try:
        if author == "user":
            voice_id = current_user.voice_code if current_user.voice_code else "nMPrFLO7QElx9wTR0JGo"
        elif author == "bot":
            voice_id = await get_voice_code_from_conversation(conversation_id, current_user)
        else:
            voice_id = "nMPrFLO7QElx9wTR0JGo"

        text_processed = process_text_for_tts(text)
        hash_input = f"{text_processed}_{voice_id}"
        hash_digest = hashlib.sha256(hash_input.encode("utf-8")).hexdigest()
        _, full_path_opus = get_file_path(hash_digest)

        if os.path.exists(full_path_opus):
            return FileResponse(full_path_opus, media_type="audio/ogg")
        return Response(status_code=204)
    except ValueError as exc:
        logger.warning("TTS audio lookup failed: %s", exc)
        return Response(status_code=204)
    except Exception as exc:
        logger.error("Error in get_tts_audio_endpoint: %s", exc)
        return JSONResponse(status_code=500, content={"error": "Internal server error"})


@router.get("/download-pdf/{conversation_id}")
async def initiate_download_pdf(
    conversation_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    if current_user is None:
        logger.warning("User not authenticated attempted to access /download-pdf")
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "captcha": get_captcha_config(),
                "google_oauth_available": bool(GOOGLE_CLIENT_ID),
            },
        )

    try:
        is_user_admin = await current_user.is_admin
    except Exception as exc:
        logger.error("Error verifying if user is admin: %s", exc)
        raise HTTPException(status_code=500, detail="Error verifying permissions.")

    await require_conversation_access(conversation_id, current_user)

    lock_key = f"pdf_lock:{conversation_id}"
    try:
        lock_acquired = await redis_client.set(lock_key, "locked", nx=True, ex=300)
        if not lock_acquired:
            return JSONResponse(content={"message": "PDF generation is already in progress or you recently generated one. Please try again in a few minutes."})

        generate_pdf_task.send(conversation_id=conversation_id, user_id=current_user.id, is_admin=is_user_admin)
        return JSONResponse(content={"message": "PDF generation has started. Please check the media gallery later to download the PDF."})
    except Exception as exc:
        logger.error("Error trying to generate PDF for conversation_id %s: %s", conversation_id, exc)
        raise HTTPException(status_code=500, detail="Internal server error.")


@router.get("/download-mp3/{conversation_id}")
async def initiate_download_mp3(
    conversation_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    if current_user is None:
        logger.warning("User not authenticated attempted to access /download-mp3")
        return unauthenticated_response()

    try:
        is_user_admin = await current_user.is_admin
    except Exception as exc:
        logger.error("Error verifying if user is admin: %s", exc)
        raise HTTPException(status_code=500, detail="Error verifying permissions.")

    await require_conversation_access(conversation_id, current_user)

    lock_key = f"mp3_lock:{conversation_id}:{current_user.id}"
    try:
        lock_acquired = await redis_client.set(lock_key, "locked", nx=True, ex=300)
        if not lock_acquired:
            return JSONResponse(content={"message": "MP3 generation is already in progress or you recently generated one. Please try again in a few minutes."})

        generate_mp3_task.send(conversation_id=conversation_id, user_id=current_user.id, is_admin=is_user_admin)
        logger.info("MP3 generation task queued for conversation_id: %s", conversation_id)
        return JSONResponse(content={"message": "MP3 generation has started. Please check the media gallery later to download the MP3."})
    except Exception as exc:
        logger.error("Error trying to generate MP3 for conversation_id %s: %s", conversation_id, exc)
        raise HTTPException(status_code=500, detail="Internal server error.")


@router.get("/serve-mp3/{conversation_id}")
async def serve_mp3(conversation_id: int, current_user: User = Depends(get_current_user)):
    if current_user is None:
        return unauthenticated_response()

    try:
        from common import generate_user_hash, users_directory

        hash_prefix1, hash_prefix2, user_hash = generate_user_hash(current_user.username)
        conv_str = f"{conversation_id:07d}"
        mp3_dir = Path(users_directory) / hash_prefix1 / hash_prefix2 / user_hash / "files" / conv_str[:3] / conv_str[3:] / "mp3"
        if not mp3_dir.exists():
            return JSONResponse(content={"error": "MP3 not found"}, status_code=404)
        mp3_files = sorted(mp3_dir.glob("*.mp3"), key=lambda path: path.stat().st_mtime, reverse=True)
        if not mp3_files:
            return JSONResponse(content={"error": "MP3 not found"}, status_code=404)
        return FileResponse(mp3_files[0], media_type="audio/mpeg", filename=f"conversation_{conversation_id}.mp3")
    except Exception as exc:
        logger.error("Error serving MP3: %s", exc)
        return JSONResponse(content={"error": "An error occurred while serving the MP3"}, status_code=500)


def get_browser(user_agent: str):
    logger.debug("User_agent: %s", user_agent)
    if "Firefox" in user_agent:
        return "firefox"
    if "Safari" in user_agent and "Chrome" not in user_agent:
        return "safari"
    if "Edg" in user_agent:
        return "edge"
    if "Chrome" in user_agent:
        return "chrome"
    return "other"


async def transcribe_with_elevenlabs(audio_content: bytes = None):
    try:
        eleven_key = get_elevenlabs_key()
        if not eleven_key:
            raise Exception("No ElevenLabs API key available")

        url = "https://api.elevenlabs.io/v1/speech-to-text"
        headers = {"xi-api-key": eleven_key}

        async with aiohttp.ClientSession() as session:
            if audio_content:
                form_data = aiohttp.FormData()
                form_data.add_field("model_id", "scribe_v2")
                form_data.add_field("file", audio_content, filename="audio.webm", content_type="audio/webm")
                async with session.post(url, headers=headers, data=form_data) as response:
                    if response.status != 200:
                        error_text = await response.text()
                        raise Exception(f"ElevenLabs API error: {response.status} - {error_text}")
                    result = await response.json()
                    return result.get("text", "")
            raise Exception("No audio content available")
    except Exception as exc:
        logger.error("Error transcribing with ElevenLabs: %s", str(exc))
        raise


async def transcribe_with_deepgram(audio_content: bytes = None, user_agent: str = None):
    try:
        options = {
            "model": "nova-2",
            "smart_format": True,
            "punctuate": True,
            "language": DEFAULT_STT_LANGUAGE,
        }
        if audio_content:
            result = await deepgram.listen.asyncprerecorded.v("1").transcribe_file(
                {"buffer": audio_content},
                options,
                timeout=httpx.Timeout(300.0, connect=10.0),
            )
        else:
            raise Exception("No audio content or media URL provided")

        data = result.to_dict()
        if not data:
            raise Exception("No response from Deepgram")
        return data["results"]["channels"][0]["alternatives"][0]["transcript"]
    except Exception as exc:
        logger.error("Error transcribing with Deepgram: %s", str(exc))
        raise


async def cost_stt(user_id: int, duration_in_minutes: float):
    total_stt_cost = Cost.STT_COST_PER_MINUTE * duration_in_minutes
    if not await deduct_balance(user_id, total_stt_cost):
        return

    last_lock_error = None
    for attempt in range(DB_MAX_RETRIES):
        retry_needed = False
        wait_time = 0.0
        async with get_db_connection() as conn:
            transaction_started = False
            try:
                await conn.execute("BEGIN IMMEDIATE")
                transaction_started = True
                await conn.execute(
                    """
                    INSERT INTO SERVICE_USAGE (user_id, service_id, usage_quantity, cost)
                    VALUES (?, ?, ?, ?)
                    """,
                    (user_id, Cost.STT_SERVICE_ID, duration_in_minutes, total_stt_cost),
                )
                await conn.execute(
                    """
                    UPDATE USER_DETAILS
                    SET total_cost = total_cost + ?, total_stt_cost = total_stt_cost + ?
                    WHERE user_id = ?
                    """,
                    (total_stt_cost, total_stt_cost, user_id),
                )
                await record_daily_usage(
                    user_id=user_id,
                    usage_type="stt",
                    cost=total_stt_cost,
                    units=duration_in_minutes,
                    conn=conn,
                )
                await conn.commit()
                return
            except sqlite3.OperationalError as exc:
                if transaction_started:
                    try:
                        await conn.rollback()
                    except Exception:
                        pass
                if is_lock_error(exc) and attempt < DB_MAX_RETRIES - 1:
                    wait_time = DB_RETRY_DELAY_BASE * (attempt + 1)
                    last_lock_error = exc
                    retry_needed = True
                else:
                    logger.error("Error executing STT cost query: %s", exc)
                    return
            except Exception as exc:
                if transaction_started:
                    try:
                        await conn.rollback()
                    except Exception:
                        pass
                logger.error("Error executing STT cost query: %s", exc)
                return

        if retry_needed:
            await asyncio.sleep(wait_time)

    if last_lock_error:
        logger.error(
            "Could not register STT cost for user_id=%s after %s retries: %s",
            user_id,
            DB_MAX_RETRIES,
            last_lock_error,
        )


async def transcribe(request: Request, audio: UploadFile = File(None), user_id: int = None):
    try:
        audio_duration = 0
        content = None

        if audio:
            content = await audio.read()
            audio_file = io.BytesIO(content)
            user_agent = request.headers.get("user-agent")
            browser = get_browser(user_agent)

            if browser == "firefox":
                logger.info("Using OggOpus for Firefox")
                ogg_audio = AudioSegment.from_file(audio_file, format="ogg", codec="opus")
                audio_duration = ogg_audio.duration_seconds
            elif browser == "chrome" or browser == "edge":
                logger.info("Using WebMOpus for Chrome and Edge")
                webm_audio = AudioSegment.from_file(audio_file, format="webm", codec="opus")
                audio_duration = webm_audio.duration_seconds
            elif browser == "safari":
                logger.info("Using MP4 for Safari")
                mp4_audio = AudioSegment.from_file(audio_file, format="mp4")
                audio_duration = mp4_audio.duration_seconds
            else:
                raise HTTPException(status_code=400, detail="Unsupported browser (for now)")
        else:
            raise HTTPException(status_code=400, detail="No audio or media URL provided")

        if audio_duration <= 0:
            raise HTTPException(status_code=400, detail="No audio")

        duration_min = audio_duration / 60
        total_stt_cost = Cost.STT_COST_PER_MINUTE * duration_min
        if not await has_sufficient_balance(user_id, total_stt_cost):
            raise HTTPException(status_code=402, detail="Insufficient balance")

        user_agent = request.headers.get("user-agent")
        try:
            if stt_engine == "elevenlabs":
                prompt = await transcribe_with_elevenlabs(audio_content=content)
            else:
                prompt = await transcribe_with_deepgram(audio_content=content, user_agent=user_agent)
        except Exception as primary_error:
            if stt_fallback_enabled:
                logger.warning("Primary STT engine (%s) failed: %s", stt_engine, str(primary_error))
                fallback_engine = "deepgram" if stt_engine == "elevenlabs" else "elevenlabs"
                try:
                    if fallback_engine == "elevenlabs":
                        prompt = await transcribe_with_elevenlabs(audio_content=content)
                    else:
                        prompt = await transcribe_with_deepgram(audio_content=content, user_agent=user_agent)
                    logger.info("Fallback to %s successful", fallback_engine)
                except Exception as fallback_error:
                    logger.error("Both STT engines failed. Primary: %s, Fallback: %s", str(primary_error), str(fallback_error))
                    raise primary_error
            else:
                raise primary_error

        await cost_stt(user_id, audio_duration / 60)
        return prompt
    except HTTPException as exc:
        if exc.detail == "User ID could not be determined":
            raise
        raise HTTPException(status_code=500, detail=str(exc))
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=exc.response.status_code, detail=f"HTTP error: {exc}")
    except Exception as exc:
        logger.error("Error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/api/transcribe-web")
async def transcribe_web(
    request: Request,
    audio: UploadFile = File(None),
    conversation_id: str = Form(...),
    current_user: User = Depends(get_current_user),
):
    if current_user is None:
        return unauthenticated_response()

    try:
        await require_conversation_access(int(conversation_id), current_user)

        prompt = await transcribe(request, audio, current_user.id)
        return JSONResponse(content={"prompt": prompt}, status_code=200)
    except HTTPException as exc:
        if exc.detail == "User ID could not be determined":
            logger.error("transcribe web: Could not determine user_id")
        raise
    except Exception as exc:
        return JSONResponse(content={"error": str(exc)}, status_code=500)


@router.get("/get-audio/{path:path}")
async def get_audio(path: str, token: str):
    try:
        payload = decode_jwt_cached(token, SECRET_KEY)
        username = payload.get("username")
        if not username:
            raise HTTPException(status_code=401, detail="Invalid token")

        current_user = await get_user_by_username(username)
        if not current_user:
            raise HTTPException(status_code=401, detail="Invalid token: user not found")

        exp = payload.get("exp")
        if not exp:
            raise HTTPException(status_code=401, detail="Token does not have expiration time")

        cache_base = Path(cache_directory)
        validated_path = validate_path_within_directory(path, cache_base)
    except jwt.ExpiredSignatureError:
        response = JSONResponse(status_code=401, content={"detail": "Token expired"})
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid token")

    if validated_path.exists():
        current_time = datetime.now(timezone.utc)
        expiration_time = datetime.fromtimestamp(exp, timezone.utc)
        time_until_expiration = expiration_time - current_time

        if time_until_expiration.total_seconds() <= 0:
            response = JSONResponse(status_code=401, content={"detail": "Token expired"})
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
            return response

        audio_path_str = str(validated_path)
        if audio_path_str.endswith(".ogg") or audio_path_str.endswith(".opus"):
            media_type = "audio/ogg"
        elif audio_path_str.endswith(".mp3"):
            media_type = "audio/mpeg"
        else:
            raise HTTPException(status_code=415, detail="Unsupported media type")

        response = FileResponse(str(validated_path), media_type=media_type)
        response.headers["Cache-Control"] = f"public, max-age={int(time_until_expiration.total_seconds())}"
        response.headers["Expires"] = expiration_time.strftime("%a, %d %b %Y %H:%M:%S GMT")
        return response

    raise HTTPException(status_code=404, detail="File not found")
