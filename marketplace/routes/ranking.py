"""Marketplace ranking admin routes."""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from auth import get_current_user, unauthenticated_response
from common import get_template_context, templates
from database import get_db_connection
from log_config import logger
from models import User
from ranking import (
    get_ranking_config,
    invalidate_ranking_config_cache,
    recalculate_ranking_scores,
)


router = APIRouter()


@router.get("/admin/ranking", response_class=HTMLResponse)
async def admin_ranking_page(request: Request, current_user: User = Depends(get_current_user)):
    """Admin page for configuring ranking weights and mode."""
    if current_user is None:
        return RedirectResponse(url="/login", status_code=303)
    if not await current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")

    ranking_config = await get_ranking_config()
    context = await get_template_context(request, current_user)
    context["ranking_config"] = ranking_config
    return templates.TemplateResponse("admin_ranking.html", context)


@router.get("/api/admin/ranking-config")
async def api_get_ranking_config(request: Request, current_user: User = Depends(get_current_user)):
    """Get current ranking configuration."""
    if current_user is None:
        return unauthenticated_response()
    if not await current_user.is_admin:
        return JSONResponse(status_code=403, content={"success": False, "message": "Admin access required"})

    config = await get_ranking_config()
    return JSONResponse(content={"success": True, "config": config})


@router.put("/api/admin/ranking-config")
async def api_update_ranking_config(request: Request, current_user: User = Depends(get_current_user)):
    """Update ranking configuration."""
    if current_user is None:
        return unauthenticated_response()
    if not await current_user.is_admin:
        return JSONResponse(status_code=403, content={"success": False, "message": "Admin access required"})

    try:
        data = await request.json()

        async with get_db_connection() as conn:
            cursor = await conn.cursor()

            if "mode" in data:
                mode = data["mode"]
                if mode not in ("piggyback", "scheduled"):
                    return JSONResponse(status_code=400, content={"success": False, "message": "Invalid mode"})
                await cursor.execute(
                    "UPDATE SYSTEM_CONFIG SET value = ?, updated_at = CURRENT_TIMESTAMP WHERE key = 'ranking_mode'",
                    (mode,),
                )

            if "interval_hours" in data:
                interval = int(data["interval_hours"])
                if interval < 1 or interval > 168:
                    return JSONResponse(status_code=400, content={"success": False, "message": "Interval must be 1-168 hours"})
                await cursor.execute(
                    "UPDATE SYSTEM_CONFIG SET value = ?, updated_at = CURRENT_TIMESTAMP WHERE key = 'ranking_interval_hours'",
                    (str(interval),),
                )

            if "weights" in data:
                weights = data["weights"]
                for key, value in weights.items():
                    numeric_value = float(value)
                    if numeric_value < 0 or numeric_value > 1000:
                        return JSONResponse(status_code=400, content={"success": False, "message": f"Invalid weight for {key}"})
                await cursor.execute(
                    "UPDATE SYSTEM_CONFIG SET value = ?, updated_at = CURRENT_TIMESTAMP WHERE key = 'ranking_weights'",
                    (json.dumps(weights),),
                )

            await conn.commit()

        invalidate_ranking_config_cache()
        return JSONResponse(content={"success": True, "message": "Ranking configuration updated"})
    except Exception as exc:
        logger.error("Error updating ranking config: %s", exc)
        return JSONResponse(status_code=500, content={"success": False, "message": str(exc)})


@router.post("/api/admin/ranking-recalculate")
async def api_ranking_recalculate(request: Request, current_user: User = Depends(get_current_user)):
    """Trigger manual ranking recalculation."""
    if current_user is None:
        return unauthenticated_response()
    if not await current_user.is_admin:
        return JSONResponse(status_code=403, content={"success": False, "message": "Admin access required"})

    asyncio.create_task(recalculate_ranking_scores())
    return JSONResponse(content={"success": True, "message": "Recalculation started"})
