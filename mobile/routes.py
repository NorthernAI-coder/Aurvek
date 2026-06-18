"""Mobile bootstrap/config API routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from auth import get_current_user
from mobile.client import mobile_config_payload
from models import User


router = APIRouter()


def _user_payload(user: User) -> dict[str, Any]:
    return {
        "id": user.id,
        "username": user.username,
        "role_id": user.role_id,
        "is_enabled": bool(getattr(user, "is_enabled", True)),
        "can_send_files": bool(getattr(user, "can_send_files", False)),
        "can_generate_images": bool(getattr(user, "can_generate_images", False)),
        "all_prompts_access": bool(getattr(user, "all_prompts_access", False)),
        "public_prompts_access": bool(getattr(user, "public_prompts_access", False)),
        "current_prompt_id": getattr(user, "current_prompt_id", None),
        "authentication_mode": getattr(user, "authentication_mode", None),
        "can_change_password": bool(getattr(user, "can_change_password", False)),
    }


@router.get("/api/mobile/v1/config")
async def mobile_config(request: Request):
    return JSONResponse(mobile_config_payload(request))


@router.get("/api/mobile/v1/bootstrap")
async def mobile_bootstrap(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    authenticated = current_user is not None
    return JSONResponse(
        {
            "config": mobile_config_payload(request),
            "session": {
                "authenticated": authenticated,
                "expired": not authenticated,
                "reason": None if authenticated else "unauthenticated",
            },
            "user": _user_payload(current_user) if authenticated else None,
        }
    )
