import asyncio

import orjson
from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import JSONResponse

from auth import get_current_user
from database import get_db_connection
from file_storage import (
    delete_attachment_and_rewrite_message,
    get_attachment_path_for_user,
    prune_unreferenced_blobs,
)
from log_config import logger
from models import User

from chat.services.attachment_uploads import (
    ATTACHMENT_UPLOAD_CHUNK_ROOT,
    ATTACHMENT_UPLOAD_CHUNK_SIZE_BYTES,
    ATTACHMENT_UPLOAD_TTL_SECONDS,
    attachment_upload_dir,
    create_pending_attachment_from_upload,
    delete_attachment_upload_dir,
    discard_pending_attachments_for_user,
    ensure_attachment_upload_allowed,
    json_error,
    parse_attachment_refs_value,
    pending_attachment_upload_payload,
    prune_stale_attachment_upload_dirs,
    validate_chunk_upload_metadata,
)

router = APIRouter()


@router.post("/api/conversations/{conversation_id}/attachments/chunk")
async def upload_attachment_chunk(
    conversation_id: int,
    current_user: User = Depends(get_current_user),
    upload_id: str = Form(...),
    chunk_index: int = Form(...),
    total_chunks: int = Form(...),
    filename: str = Form(...),
    content_type: str = Form(""),
    total_size: int = Form(...),
    chunk: UploadFile = File(...),
):
    guard_response = await ensure_attachment_upload_allowed(conversation_id, current_user)
    if guard_response is not None:
        return guard_response

    try:
        normalized_type, _max_bytes = validate_chunk_upload_metadata(
            upload_id=upload_id,
            chunk_index=chunk_index,
            total_chunks=total_chunks,
            filename=filename,
            content_type=content_type,
            total_size=total_size,
        )
        upload_dir = attachment_upload_dir(current_user.id, conversation_id, upload_id)
    except ValueError as exc:
        return json_error(str(exc), status_code=400)

    start = chunk_index * ATTACHMENT_UPLOAD_CHUNK_SIZE_BYTES
    expected_size = min(ATTACHMENT_UPLOAD_CHUNK_SIZE_BYTES, max(0, total_size - start))
    data = await chunk.read(ATTACHMENT_UPLOAD_CHUNK_SIZE_BYTES + 1)
    if len(data) > ATTACHMENT_UPLOAD_CHUNK_SIZE_BYTES:
        return json_error("Chunk exceeds upload size limit", status_code=400)
    if len(data) != expected_size:
        return json_error("Chunk size does not match metadata", status_code=400)

    try:
        if chunk_index == 0:
            await asyncio.to_thread(
                prune_stale_attachment_upload_dirs,
                ATTACHMENT_UPLOAD_CHUNK_ROOT,
                ATTACHMENT_UPLOAD_TTL_SECONDS,
            )
        await asyncio.to_thread(upload_dir.mkdir, parents=True, exist_ok=True)
        meta_path = upload_dir / "meta.json"
        metadata = {
            "upload_id": upload_id,
            "filename": filename,
            "content_type": normalized_type,
            "total_size": int(total_size),
            "total_chunks": int(total_chunks),
            "user_id": int(current_user.id),
            "conversation_id": int(conversation_id),
        }
        if meta_path.exists():
            existing = orjson.loads(await asyncio.to_thread(meta_path.read_bytes))
            comparable_keys = ("filename", "content_type", "total_size", "total_chunks", "user_id", "conversation_id")
            if any(existing.get(key) != metadata.get(key) for key in comparable_keys):
                await delete_attachment_upload_dir(upload_dir)
                return json_error("Upload metadata changed during transfer", status_code=400)
        else:
            await asyncio.to_thread(meta_path.write_bytes, orjson.dumps(metadata))
        part_path = upload_dir / f"{chunk_index:06d}.part"
        await asyncio.to_thread(part_path.write_bytes, data)
    except Exception as exc:
        logger.error("[upload_attachment_chunk] Could not persist chunk: %s", exc)
        return json_error("Failed to store upload chunk", status_code=500)

    return JSONResponse(
        content={
            "success": True,
            "upload_id": upload_id,
            "chunk_index": chunk_index,
            "total_chunks": total_chunks,
        }
    )


@router.post("/api/conversations/{conversation_id}/attachments/complete")
async def complete_attachment_upload(
    conversation_id: int,
    current_user: User = Depends(get_current_user),
    upload_id: str = Form(...),
    total_chunks: int = Form(...),
    filename: str = Form(...),
    content_type: str = Form(""),
    total_size: int = Form(...),
):
    guard_response = await ensure_attachment_upload_allowed(conversation_id, current_user)
    if guard_response is not None:
        return guard_response

    try:
        normalized_type, _max_bytes = validate_chunk_upload_metadata(
            upload_id=upload_id,
            chunk_index=0,
            total_chunks=total_chunks,
            filename=filename,
            content_type=content_type,
            total_size=total_size,
        )
        upload_dir = attachment_upload_dir(current_user.id, conversation_id, upload_id)
    except ValueError as exc:
        return json_error(str(exc), status_code=400)

    meta_path = upload_dir / "meta.json"
    if not meta_path.exists():
        return json_error("Upload chunks were not found. Please attach the file again.", status_code=400)

    try:
        metadata = orjson.loads(await asyncio.to_thread(meta_path.read_bytes))
    except Exception:
        await delete_attachment_upload_dir(upload_dir)
        return json_error("Upload metadata is corrupted. Please attach the file again.", status_code=400)

    expected_metadata = {
        "filename": filename,
        "content_type": normalized_type,
        "total_size": int(total_size),
        "total_chunks": int(total_chunks),
        "user_id": int(current_user.id),
        "conversation_id": int(conversation_id),
    }
    if any(metadata.get(key) != value for key, value in expected_metadata.items()):
        await delete_attachment_upload_dir(upload_dir)
        return json_error("Upload metadata changed during transfer", status_code=400)

    parts: list[bytes] = []
    for index in range(total_chunks):
        part_path = upload_dir / f"{index:06d}.part"
        if not part_path.exists():
            return json_error("Upload is incomplete. Please retry the file upload.", status_code=400)
        part_data = await asyncio.to_thread(part_path.read_bytes)
        start = index * ATTACHMENT_UPLOAD_CHUNK_SIZE_BYTES
        expected_size = min(ATTACHMENT_UPLOAD_CHUNK_SIZE_BYTES, max(0, total_size - start))
        if len(part_data) != expected_size:
            await delete_attachment_upload_dir(upload_dir)
            return json_error("Upload chunk size mismatch. Please attach the file again.", status_code=400)
        parts.append(part_data)

    data = b"".join(parts)
    if len(data) != total_size:
        await delete_attachment_upload_dir(upload_dir)
        return json_error("Upload size mismatch. Please attach the file again.", status_code=400)

    try:
        pending = await create_pending_attachment_from_upload(
            user_id=current_user.id,
            conversation_id=conversation_id,
            data=data,
            filename=filename,
            content_type=normalized_type,
        )
    except ValueError as exc:
        await delete_attachment_upload_dir(upload_dir)
        return json_error(str(exc), status_code=400)
    except Exception as exc:
        await delete_attachment_upload_dir(upload_dir)
        logger.error("[complete_attachment_upload] Could not create pending attachment: %s", exc)
        return json_error("Failed to finalize uploaded attachment", status_code=500)

    await delete_attachment_upload_dir(upload_dir)
    return JSONResponse(content=pending_attachment_upload_payload(pending))


@router.post("/api/conversations/{conversation_id}/attachments/discard")
async def discard_uploaded_attachments(
    conversation_id: int,
    current_user: User = Depends(get_current_user),
    attachment_refs: str = Form("[]"),
):
    if current_user is None:
        return json_error("Not authenticated", status_code=401, redirect="/login")
    try:
        refs = parse_attachment_refs_value(attachment_refs)
    except ValueError as exc:
        return json_error(str(exc), status_code=400)
    if not refs:
        return JSONResponse(content={"success": True, "discarded": 0})

    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.execute(
            "SELECT user_id FROM CONVERSATIONS WHERE id = ?",
            (conversation_id,),
        )
        row = await cursor.fetchone()
    if not row or int(row["user_id"]) != int(current_user.id):
        return json_error("Conversation not found.", status_code=404)

    discarded = await discard_pending_attachments_for_user(
        refs,
        user_id=current_user.id,
        conversation_id=conversation_id,
        reason="client_discard",
    )
    return JSONResponse(content={"success": True, "discarded": discarded})


async def serve_attachment_file(
    public_id: str,
    current_user: User,
    *,
    variant=None,
    download: bool = False,
):
    from fastapi import HTTPException
    from fastapi.responses import FileResponse
    from file_storage import THUMB_VARIANT

    if current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")

    is_admin_user = await current_user.is_admin
    try:
        async with get_db_connection(readonly=True) as conn:
            resolved = await get_attachment_path_for_user(
                conn,
                public_id=public_id,
                user_id=current_user.id,
                variant=variant,
                allow_admin=is_admin_user,
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if not resolved:
        raise HTTPException(status_code=404, detail="Attachment not found")

    path, attachment = resolved
    if variant == THUMB_VARIANT and attachment.get("attachment_type") != "image":
        raise HTTPException(status_code=404, detail="Attachment variant not found")
    media_type = "image/webp" if variant == THUMB_VARIANT else attachment.get("mime_detected")
    filename = attachment.get("original_filename") or path.name
    if download:
        return FileResponse(path, media_type=media_type, filename=filename)
    return FileResponse(path, media_type=media_type)


@router.get("/api/attachments/{public_id}/content")
async def attachment_content(
    public_id: str,
    variant: str | None = None,
    current_user: User = Depends(get_current_user),
):
    return await serve_attachment_file(
        public_id,
        current_user,
        variant=variant,
        download=False,
    )


@router.get("/api/attachments/{public_id}/download")
async def attachment_download(
    public_id: str,
    current_user: User = Depends(get_current_user),
):
    return await serve_attachment_file(
        public_id,
        current_user,
        download=True,
    )


@router.delete("/api/attachments/{public_id}")
async def delete_attachment(public_id: str, current_user: User = Depends(get_current_user)):
    from fastapi import HTTPException

    if current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")

    async with get_db_connection() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        try:
            deleted = await delete_attachment_and_rewrite_message(
                conn,
                public_id=public_id,
                user_id=current_user.id,
                allow_admin=await current_user.is_admin,
            )
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise

    if not deleted:
        raise HTTPException(status_code=404, detail="Attachment not found")
    await prune_unreferenced_blobs()
    return JSONResponse(content={"success": True, "message": "Attachment deleted"})
