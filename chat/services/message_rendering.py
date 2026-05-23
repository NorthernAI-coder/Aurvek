import re
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urljoin

import orjson
from fastapi import Depends, Request
from fastapi.exceptions import HTTPException as FastAPIHTTPException

from auth import get_current_user, get_user_by_username
from common import (
    CLOUDFLARE_BASE_URL,
    CLOUDFLARE_FOR_IMAGES,
    generate_signed_url_cloudflare,
    generate_user_hash,
    validate_path_within_directory,
)
from database import get_db_connection
from file_storage import (
    THUMB_VARIANT,
    attachment_content_url,
    attachment_download_url,
    resolve_attachment_for_user,
)
from models import User
from prompts import get_user_directory
from save_images import get_or_generate_img_token


_LOCAL_MARKDOWN_IMAGE_PATTERN = re.compile(r"!\[(?P<alt>[^\]]*)\]\((?P<url>[^)]+)\)")


def normalize_local_markdown_image_url(url: str, media_owner_username: str) -> Optional[str]:
    """Return a base media URL for legacy local markdown images, or None."""
    candidate = (url or "").strip()
    if not candidate:
        return None

    hash_prefix1, hash_prefix2, user_hash = generate_user_hash(media_owner_username)
    allowed_prefix = f"users/{hash_prefix1}/{hash_prefix2}/{user_hash}/"

    if CLOUDFLARE_BASE_URL and candidate.startswith(CLOUDFLARE_BASE_URL):
        relative_path = candidate[len(CLOUDFLARE_BASE_URL):]
    elif candidate.startswith("/users/"):
        relative_path = candidate.lstrip("/")
    elif candidate.startswith("users/"):
        relative_path = candidate
    else:
        return None

    relative_path = relative_path.split("?", 1)[0]
    if not relative_path:
        return None
    if not relative_path.startswith(allowed_prefix):
        return None

    relative_to_user = relative_path[len(allowed_prefix):]
    if not relative_to_user:
        return None

    try:
        validated_path = validate_path_within_directory(
            relative_to_user,
            Path(get_user_directory(media_owner_username)),
        )
    except FastAPIHTTPException:
        return None

    normalized_relative = validated_path.relative_to(Path("data").resolve()).as_posix()
    return f"{CLOUDFLARE_BASE_URL}{normalized_relative}"


def convert_legacy_markdown_images_to_blocks(
    message: str, media_owner_username: str
) -> Optional[List[Dict]]:
    """Convert legacy local markdown images into structured blocks for rehydration."""
    matches = list(_LOCAL_MARKDOWN_IMAGE_PATTERN.finditer(message or ""))
    if not matches:
        return None

    blocks = []
    cursor = 0
    converted = False

    for match in matches:
        prefix = message[cursor:match.start()]
        if prefix:
            blocks.append({"type": "text", "text": prefix})

        base_url = normalize_local_markdown_image_url(
            match.group("url"), media_owner_username
        )
        if base_url:
            converted = True
            blocks.append({
                "type": "image_url",
                "image_url": {
                    "url": base_url,
                    "alt": match.group("alt"),
                },
            })
        else:
            blocks.append({"type": "text", "text": match.group(0)})

        cursor = match.end()

    suffix = message[cursor:]
    if suffix:
        blocks.append({"type": "text", "text": suffix})

    if not converted:
        return None

    merged_blocks = []
    for block in blocks:
        if block.get("type") == "text":
            text = block.get("text", "")
            if not text:
                continue
            if merged_blocks and merged_blocks[-1].get("type") == "text":
                merged_blocks[-1]["text"] += text
            else:
                merged_blocks.append({"type": "text", "text": text})
        else:
            merged_blocks.append(block)

    return merged_blocks


async def process_message(
    message: str,
    request: Request,
    current_user: User = Depends(get_current_user),
    media_owner_username: Optional[str] = None,
    conversation_id: Optional[int] = None,
    message_id: Optional[int] = None,
):
    valid_extensions = {"png", "jpg", "jpeg", "gif", "webp"}
    start = f"{CLOUDFLARE_BASE_URL}sk" if CLOUDFLARE_BASE_URL else ""
    start_len = len(start)
    media_owner_username = media_owner_username or current_user.username
    media_token_user = current_user

    if not CLOUDFLARE_FOR_IMAGES and media_owner_username != current_user.username:
        owner_user = await get_user_by_username(media_owner_username)
        if owner_user:
            media_token_user = owner_user

    try:
        message_json = orjson.loads(message)
    except orjson.JSONDecodeError:
        message_json = convert_legacy_markdown_images_to_blocks(
            message, media_owner_username
        )
        if message_json is None:
            return message

    if isinstance(message_json, list):
        can_admin_view = await current_user.is_admin
        for entry in message_json:
            if entry.get("type") == "image_url":
                image_info = entry.get("image_url", {})
                attachment_ref = image_info.get("attachment_ref")
                if attachment_ref:
                    async with get_db_connection(readonly=True) as conn:
                        attachment = await resolve_attachment_for_user(
                            conn,
                            public_id=attachment_ref,
                            user_id=current_user.id,
                            conversation_id=conversation_id,
                            message_id=message_id,
                            require_kind="image",
                            allow_admin=can_admin_view,
                        )
                    if attachment:
                        image_info["url"] = attachment_content_url(attachment_ref, variant=THUMB_VARIANT)
                        image_info["fullsize_url"] = attachment_content_url(attachment_ref)
                        image_info["filename"] = image_info.get("filename") or attachment.get("original_filename")
                    continue

                url = image_info.get("url", "")

                extension = url.rsplit(".", 1)[-1].lower()
                if extension not in valid_extensions:
                    continue

                if url.startswith(start):
                    hash_prefix1, hash_prefix2, user_hash = generate_user_hash(
                        media_owner_username
                    )
                    image_path = f"users/{hash_prefix1}/{hash_prefix2}/{user_hash}/{url[start_len:]}"

                    if CLOUDFLARE_FOR_IMAGES:
                        signed_url = generate_signed_url_cloudflare(image_path, expiration_seconds=3600)
                        entry["image_url"]["url"] = signed_url
                    else:
                        token = await get_or_generate_img_token(media_token_user)
                        full_url = urljoin(CLOUDFLARE_BASE_URL, f"{image_path}?token={token}")
                        entry["image_url"]["url"] = full_url

                elif url.startswith(CLOUDFLARE_BASE_URL):
                    image_path = url[len(CLOUDFLARE_BASE_URL):]

                    if CLOUDFLARE_FOR_IMAGES:
                        signed_url = generate_signed_url_cloudflare(image_path, expiration_seconds=3600)
                        entry["image_url"]["url"] = signed_url
                    else:
                        token = await get_or_generate_img_token(media_token_user)
                        full_url = urljoin(CLOUDFLARE_BASE_URL, f"{image_path}?token={token}")
                        entry["image_url"]["url"] = full_url

            elif entry.get("type") == "video_url":
                url = entry.get("video_url", {}).get("url", "")
                if url.startswith(CLOUDFLARE_BASE_URL):
                    video_path = url[len(CLOUDFLARE_BASE_URL):]

                    if CLOUDFLARE_FOR_IMAGES:
                        signed_url = generate_signed_url_cloudflare(video_path, expiration_seconds=3600)
                        entry["video_url"]["url"] = signed_url
                    else:
                        token = await get_or_generate_img_token(media_token_user)
                        full_url = urljoin(CLOUDFLARE_BASE_URL, f"{video_path}?token={token}")
                        entry["video_url"]["url"] = full_url

            elif entry.get("type") == "document_url":
                doc_info = entry.get("document_url", {})
                attachment_ref = doc_info.get("attachment_ref")
                if attachment_ref:
                    async with get_db_connection(readonly=True) as conn:
                        attachment = await resolve_attachment_for_user(
                            conn,
                            public_id=attachment_ref,
                            user_id=current_user.id,
                            conversation_id=conversation_id,
                            message_id=message_id,
                            require_kind="pdf",
                            allow_admin=can_admin_view,
                        )
                    if attachment:
                        doc_info["url"] = attachment_download_url(attachment_ref)
                        doc_info["filename"] = doc_info.get("filename") or attachment.get("original_filename")
                        doc_info["pages"] = doc_info.get("pages") or attachment.get("page_count") or 0

            elif entry.get("type") == "text_file":
                text_info = entry.get("text_file", {})
                attachment_ref = text_info.get("attachment_ref")
                if attachment_ref:
                    async with get_db_connection(readonly=True) as conn:
                        attachment = await resolve_attachment_for_user(
                            conn,
                            public_id=attachment_ref,
                            user_id=current_user.id,
                            conversation_id=conversation_id,
                            message_id=message_id,
                            require_kind="text",
                            allow_admin=can_admin_view,
                        )
                    if attachment:
                        text_info["url"] = attachment_download_url(attachment_ref)
                        text_info["filename"] = text_info.get("filename") or attachment.get("original_filename")
                        text_info["lines"] = text_info.get("lines") or attachment.get("text_line_count") or 0

        return orjson.dumps(message_json).decode("utf-8")

    return message
