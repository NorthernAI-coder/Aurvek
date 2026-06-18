# Shared imports for mechanically extracted AI runtime modules.
import asyncio
import copy
import aiohttp
import orjson
import aiosqlite
import anthropic
import jwt
from jwt import PyJWTError as JWTError
from google import genai as google_genai
from google.genai import types as genai_types
from openai import OpenAI
from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass
import io
import zlib
import base64
from PIL import Image as PilImage
import re
import os
import logging
import hashlib
import time
from typing import Any, List, Optional
import traceback
import sqlite3
import uuid
import requests
import urllib.parse
import contextvars
from contextlib import asynccontextmanager, suppress
from pathlib import Path

# Import own modules
from log_config import logger
from database import get_db_connection, DB_MAX_RETRIES, DB_RETRY_DELAY_BASE, is_lock_error
from auth import get_user_by_id
from rediscfg import check_rate_limit
from common import (
    custom_unescape,
    estimate_message_tokens,
    text_file_block_to_text,
    Cost,
    generate_user_hash,
    has_sufficient_balance,
    get_balance,
    deduct_balance,
    SECRET_KEY,
    ALGORITHM,
    MAX_TOKENS,

    MAX_MESSAGE_SIZE,
    CLOUDFLARE_FOR_IMAGES,
    CLOUDFLARE_BASE_URL,
    generate_signed_url_cloudflare,
    MEDIA_TOKEN_EXPIRE_HOURS,
    openai_key,
    xai_key,
    claude_key,
    gemini_key,
    openrouter_key,
    minimax_key,
    moonshot_key,
    elevenlabs_key,
    tts_engine,
    decode_jwt_cached,
    verify_token_expiration,
    consume_token,
    extract_post_watchdog_config,
    extract_pre_watchdog_config,
    get_llm_info,
    get_llm_token_costs,
    decrypt_api_key,
    get_user_api_key_mode,
    resolve_api_key_for_provider,
    users_directory,
    MAX_IMAGE_PIXELS,
    MAX_RAW_UPLOAD_SIZE_MB,
    MAX_API_IMAGE_SIZE_MB,
    MAX_CHAT_IMAGE_DIMENSION,
    MAX_PDF_SIZE_MB,
    MAX_PDF_PAGES,
    MAX_PDFS_PER_MESSAGE,
    MAX_TEXT_FILE_SIZE_MB,
    MAX_TEXT_FILES_PER_MESSAGE,
    OPENROUTER_MODEL_MAP,
)
from models import User
from save_images import save_image_locally, generate_img_token, resize_image, get_or_generate_img_token
from save_pdfs import validate_pdf, extract_pdf_text_local
from save_pdfs import extract_pdf_page_range
from integrations.conversations import change_response_mode, is_whatsapp_conversation
from tasks import generate_pdf_task, generate_mp3_task
from chat.services.attachment_uploads import (
    load_pending_attachment_files,
    parse_attachment_refs_value,
)
from chat.services.file_inputs import (
    convert_to_jpeg_b64,
    decode_text_file,
    is_text_file,
    validate_and_compress_image,
)
from chat.services.locks import conversation_write_lock
from chat.services.message_requests import validate_message_request
from chat.services.stop_signals import stop_signals
from chat.services.warmup import (
    WarmupCacheKey,
    get_or_prepare as warmup_get_or_prepare,
    get_snapshot as get_warmup_snapshot,
    get_ttl_seconds as get_warmup_ttl_seconds,
    mark_consumed as mark_warmup_consumed,
    mark_error as mark_warmup_error,
    mark_skipped as mark_warmup_skipped,
    normalize_model_ids as normalize_warmup_model_ids,
)
from atagia_bridge import get_atagia_bridge
from chat.services.privacy import ensure_conversation_privacy_schema
from file_storage import (
    attachment_record_to_block,
    create_pending_image_attachment,
    create_pending_pdf_attachment,
    create_pending_text_attachment,
    discard_pending_attachments,
    discard_pending_attachments_for_user,
    finalize_message_attachments,
    image_block_to_provider_block,
    read_attachment_bytes,
    read_pending_attachment_bytes,
)
from wellbeing_service import get_active_pause, record_chat_turn

# API client configuration
openai = OpenAI(api_key=openai_key)
anthropic.api_key = claude_key

# aiohttp logging for HTTP calls
aiohttp_logger = logging.getLogger('aiohttp')
aiohttp_logger.setLevel(logging.DEBUG if os.getenv("APP_DEBUG", "false").lower() == "true" else logging.WARNING)
