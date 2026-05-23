import asyncio
import io
import sqlite3

import aiohttp
import httpx
import requests
from fastapi import HTTPException
from pydub import AudioSegment

from clients import deepgram, stt_engine, stt_fallback_enabled
from common import Cost, deduct_balance, has_sufficient_balance, record_daily_usage
from database import DB_MAX_RETRIES, DB_RETRY_DELAY_BASE, get_db_connection, is_lock_error
from log_config import logger
from tools.tts_load_balancer import get_elevenlabs_key


DEFAULT_STT_LANGUAGE = "es"


async def transcribe_with_elevenlabs(audio_content: bytes = None, media_url: str = None):
    try:
        eleven_key = get_elevenlabs_key()
        if not eleven_key:
            raise Exception("No ElevenLabs API key available")

        url = "https://api.elevenlabs.io/v1/speech-to-text"
        headers = {"xi-api-key": eleven_key}

        async with aiohttp.ClientSession() as session:
            if media_url:
                async with session.get(media_url) as response:
                    if response.status != 200:
                        raise Exception(f"Error downloading audio from URL: {response.status}")
                    audio_content = await response.read()

            if not audio_content:
                raise Exception("No audio content available")

            form_data = aiohttp.FormData()
            form_data.add_field("model_id", "scribe_v2")
            form_data.add_field("file", audio_content, filename="audio.webm", content_type="audio/webm")

            async with session.post(url, headers=headers, data=form_data) as response:
                if response.status != 200:
                    error_text = await response.text()
                    raise Exception(f"ElevenLabs API error: {response.status} - {error_text}")
                result = await response.json()
                return result.get("text", "")
    except Exception as e:
        logger.error(f"Error transcribing with ElevenLabs: {str(e)}")
        raise


async def transcribe_with_deepgram(
    audio_content: bytes = None,
    media_url: str = None,
    user_agent: str = None,
):
    try:
        options = {
            "model": "nova-2",
            "smart_format": True,
            "punctuate": True,
            "language": DEFAULT_STT_LANGUAGE,
        }
        if media_url:
            response = await deepgram.listen.asyncprerecorded.v("1").transcribe_url(
                {"url": media_url},
                options,
            )
        elif audio_content:
            response = await deepgram.listen.asyncprerecorded.v("1").transcribe_file(
                {"buffer": audio_content},
                options,
                timeout=httpx.Timeout(300.0, connect=10.0),
            )
        else:
            raise Exception("No audio content or media URL provided")

        result = response.to_dict()
        if not result:
            raise Exception("No response from Deepgram")
        return result["results"]["channels"][0]["alternatives"][0]["transcript"]
    except Exception as e:
        logger.error(f"Error transcribing with Deepgram: {str(e)}")
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
                    logger.error(f"Error executing STT cost query: {exc}")
                    return
            except Exception as e:
                if transaction_started:
                    try:
                        await conn.rollback()
                    except Exception:
                        pass
                logger.error(f"Error executing STT cost query: {e}")
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


async def transcribe_external_audio(
    *,
    user_id: int,
    media_url: str = None,
    audio_content: bytes = None,
    user_agent: str = None,
):
    if media_url:
        response = await asyncio.to_thread(requests.get, media_url, timeout=(5, 30))
        if response.status_code != 200:
            raise HTTPException(status_code=400, detail="Error downloading audio file")
        audio_content = response.content

    if not audio_content:
        raise HTTPException(status_code=400, detail="No audio")

    audio_segment = AudioSegment.from_file(io.BytesIO(audio_content))
    audio_duration = audio_segment.duration_seconds
    if audio_duration <= 0:
        raise HTTPException(status_code=400, detail="No audio")

    duration_min = audio_duration / 60
    total_stt_cost = Cost.STT_COST_PER_MINUTE * duration_min
    if not await has_sufficient_balance(user_id, total_stt_cost):
        raise HTTPException(status_code=402, detail="Insufficient balance")

    prompt = None
    try:
        if stt_engine == "elevenlabs":
            prompt = await transcribe_with_elevenlabs(audio_content=audio_content)
        else:
            prompt = await transcribe_with_deepgram(
                audio_content=audio_content,
                user_agent=user_agent,
            )
    except Exception as primary_error:
        if not stt_fallback_enabled:
            raise primary_error

        fallback_engine = "deepgram" if stt_engine == "elevenlabs" else "elevenlabs"
        try:
            if fallback_engine == "elevenlabs":
                prompt = await transcribe_with_elevenlabs(audio_content=audio_content)
            else:
                prompt = await transcribe_with_deepgram(
                    audio_content=audio_content,
                    user_agent=user_agent,
                )
        except Exception:
            raise primary_error

    await cost_stt(user_id, duration_min)
    return prompt


async def transcribe_external_request(request, audio=None, user_id: int = None, media_url: str = None):
    user_agent = request.headers.get("user-agent") if request else None
    audio_content = await audio.read() if audio else None
    return await transcribe_external_audio(
        user_id=user_id,
        media_url=media_url,
        audio_content=audio_content,
        user_agent=user_agent,
    )
