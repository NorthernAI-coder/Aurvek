import orjson

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from auth import get_current_user
from models import User
from integrations.devices.service import (
    DeviceValidationError,
    DeviceRuntimeError,
    MAX_DEVICE_MESSAGE_BODY_BYTES,
    authenticate_device_token,
    device_me_payload,
    extract_bearer_token,
    get_conversation_external_bindings,
    handle_device_text_message,
    update_conversation_external_bindings,
)


router = APIRouter()


def _runtime_error_response(exc: DeviceRuntimeError) -> JSONResponse:
    return JSONResponse(
        content={
            "success": False,
            "error": exc.code,
            "message": exc.message,
        },
        status_code=exc.status_code,
    )


def _validation_status(exc: DeviceValidationError) -> int:
    return getattr(exc, "status_code", 400)


def _body_too_large_error() -> DeviceRuntimeError:
    return DeviceRuntimeError(
        "payload_too_large",
        "Request body exceeds the external device message size limit",
        413,
    )


async def _read_device_json_payload(request: Request) -> dict:
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            declared_size = int(content_length)
        except (TypeError, ValueError) as exc:
            raise DeviceRuntimeError("invalid_request", "Invalid Content-Length header", 400) from exc
        if declared_size < 0:
            raise DeviceRuntimeError("invalid_request", "Invalid Content-Length header", 400)
        if declared_size > MAX_DEVICE_MESSAGE_BODY_BYTES:
            raise _body_too_large_error()

    body = bytearray()
    async for chunk in request.stream():
        if not chunk:
            continue
        body.extend(chunk)
        if len(body) > MAX_DEVICE_MESSAGE_BODY_BYTES:
            raise _body_too_large_error()

    try:
        payload = orjson.loads(bytes(body))
    except Exception as exc:
        raise DeviceRuntimeError("invalid_request", "Request body must be JSON", 400) from exc
    if not isinstance(payload, dict):
        raise DeviceRuntimeError("invalid_request", "Request body must be a JSON object", 400)
    return payload


@router.get("/api/devices/me")
async def get_current_device(request: Request):
    try:
        token = extract_bearer_token(request.headers.get("Authorization"))
        device = await authenticate_device_token(token)
        return JSONResponse(
            content={
                "success": True,
                "device": await device_me_payload(device),
            }
        )
    except DeviceRuntimeError as exc:
        return _runtime_error_response(exc)


@router.get("/api/conversations/{conversation_id}/external-bindings")
async def get_external_bindings_for_conversation(
    conversation_id: int,
    current_user: User = Depends(get_current_user),
):
    if current_user is None:
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        data = await get_conversation_external_bindings(
            user_id=current_user.id,
            conversation_id=conversation_id,
        )
        return JSONResponse(content={"success": True, **data})
    except DeviceValidationError as exc:
        return JSONResponse(
            status_code=_validation_status(exc),
            content={
                "success": False,
                "error": "external_bindings_error",
                "message": str(exc),
            },
        )


@router.post("/api/conversations/{conversation_id}/external-bindings")
async def update_external_bindings_for_conversation(
    conversation_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    if current_user is None:
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(
            status_code=400,
            content={
                "success": False,
                "error": "invalid_request",
                "message": "Request body must be valid JSON.",
            },
        )
    if not isinstance(payload, dict):
        return JSONResponse(
            status_code=400,
            content={
                "success": False,
                "error": "invalid_request",
                "message": "Request body must be a JSON object.",
            },
        )

    try:
        data = await update_conversation_external_bindings(
            user_id=current_user.id,
            conversation_id=conversation_id,
            device_ids=payload.get("device_ids", []),
            group_ids=payload.get("group_ids", []),
        )
        return JSONResponse(content={"success": True, **data})
    except DeviceValidationError as exc:
        return JSONResponse(
            status_code=_validation_status(exc),
            content={
                "success": False,
                "error": "external_bindings_error",
                "message": str(exc),
            },
        )


@router.post("/api/devices/messages")
async def post_device_message(request: Request):
    try:
        token = extract_bearer_token(request.headers.get("Authorization"))
        device = await authenticate_device_token(token)
        payload = await _read_device_json_payload(request)

        result = await handle_device_text_message(
            request=request,
            device=device,
            message_id=payload.get("message_id"),
            text=payload.get("text"),
            metadata=payload.get("metadata", {}),
            snapshot=payload.get("snapshot"),
            snapshots=payload.get("snapshots"),
        )
        return JSONResponse(content=result)
    except DeviceRuntimeError as exc:
        return _runtime_error_response(exc)
