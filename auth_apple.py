"""Disabled-by-default Sign in with Apple skeleton.

This module intentionally does not authenticate users yet. It exposes status and
placeholder endpoints so native iOS work can integrate against stable URLs while
the Apple Developer identifiers and server-side token verification are prepared.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse


router = APIRouter()

APPLE_SIGN_IN_ENV_VARS = (
    "APPLE_TEAM_ID",
    "APPLE_CLIENT_ID",
    "APPLE_KEY_ID",
)


def _env_enabled(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on", "enabled"}


def get_apple_sign_in_status() -> dict[str, Any]:
    missing = [name for name in APPLE_SIGN_IN_ENV_VARS if not os.getenv(name, "").strip()]
    has_private_key = bool(
        os.getenv("APPLE_PRIVATE_KEY", "").strip()
        or os.getenv("APPLE_PRIVATE_KEY_PATH", "").strip()
    )
    if not has_private_key:
        missing.append("APPLE_PRIVATE_KEY or APPLE_PRIVATE_KEY_PATH")

    configured = not missing
    enabled = _env_enabled("APPLE_SIGN_IN_ENABLED") and configured
    return {
        "enabled": enabled,
        "configured": configured,
        "missing_env": missing,
        "native_callback_url": "/api/auth/apple/native-callback",
        "web_start_url": "/auth/apple",
        "web_callback_url": "/auth/apple/callback",
        "implementation_status": "disabled" if not enabled else "pending_token_verification",
    }


def _unavailable_response(status_code: int = 503) -> JSONResponse:
    status = get_apple_sign_in_status()
    return JSONResponse(
        {
            "error": "apple_sign_in_unavailable",
            "message": "Sign in with Apple is not enabled on this server yet.",
            "apple_sign_in": status,
        },
        status_code=status_code,
    )


@router.get("/api/auth/apple/status")
async def apple_sign_in_status():
    return JSONResponse({"apple_sign_in": get_apple_sign_in_status()})


@router.post("/api/auth/apple/native-callback")
async def apple_native_callback(request: Request):
    status = get_apple_sign_in_status()
    if not status["enabled"]:
        return _unavailable_response()

    return JSONResponse(
        {
            "error": "apple_sign_in_not_implemented",
            "message": "Apple identity token verification is not implemented yet.",
            "apple_sign_in": status,
        },
        status_code=501,
    )


@router.get("/auth/apple")
async def apple_web_start():
    return _unavailable_response()


@router.get("/auth/apple/callback")
async def apple_web_callback():
    return _unavailable_response()
