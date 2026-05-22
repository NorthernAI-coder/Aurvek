"""Marketplace geo-blocking admin routes."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from auth import get_current_user, unauthenticated_response
from cloudflare_geo import (
    CloudflareGeoClient,
    geo_sync_engine,
    get_all_geo_data,
    validate_continent_codes,
    validate_country_codes,
)
from common import get_template_context, templates
from database import get_db_connection
from log_config import logger
from marketplace.config import marketplace_public_landings_enabled, require_public_landings_enabled
from models import User


router = APIRouter()


# =============================================================================
# Admin Geo-Blocking Configuration
# =============================================================================

@router.get("/admin/geo", response_class=HTMLResponse)
async def admin_geo_page(request: Request, current_user: User = Depends(get_current_user)):
    """Admin page for geo-blocking configuration via Cloudflare WAF."""
    if current_user is None:
        return RedirectResponse(url="/login", status_code=303)

    if not await current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")

    context = await get_template_context(request, current_user)
    # Transform geo data into continent-grouped format for the admin UI
    raw = get_all_geo_data()
    grouped = {}
    for cont_code, cont_name in raw.get("continents", {}).items():
        countries = [c for c in raw.get("countries", []) if c.get("continent") == cont_code]
        grouped[cont_name] = {"code": cont_code, "countries": countries}
    context["geo_data"] = grouped
    return templates.TemplateResponse("admin_geo.html", context)


@router.get("/api/admin/geo/status")
async def get_geo_status(request: Request, current_user: User = Depends(get_current_user)):
    """Get Cloudflare geo-blocking status and current configuration."""
    if current_user is None:
        return unauthenticated_response()

    if not await current_user.is_admin:
        return JSONResponse(status_code=403, content={"success": False, "message": "Admin access required"})

    try:
        client = CloudflareGeoClient()

        # Build status sub-object matching admin UI expectations
        plan_rules_max = {"free": 5, "pro": 20, "business": 100, "enterprise": 1000}
        status_obj = {
            "connected": client.is_configured(),
            "plan": "--",
            "rules_used": 0,
            "rules_max": 5,
            "transforms_enabled": False,
        }

        if client.is_configured():
            try:
                zone_info = await client.get_zone_info()
                plan = zone_info.get("plan", {})
                plan_id = plan.get("legacy_id", plan.get("name", "unknown"))
                status_obj["plan"] = plan_id
                status_obj["rules_max"] = plan_rules_max.get(plan_id, 5)

                try:
                    ruleset = await client.get_ruleset()
                    rules = ruleset.get("rules", [])
                    status_obj["rules_used"] = len(rules)
                except Exception:
                    pass

                try:
                    status_obj["transforms_enabled"] = await client.check_managed_transforms()
                except Exception:
                    pass

            except Exception as e:
                status_obj["connection_error"] = str(e)

        # Load current global config from DB and parse into UI-friendly format
        async with get_db_connection(readonly=True) as conn:
            raw_config = {}
            async with conn.execute(
                "SELECT key, value FROM SYSTEM_CONFIG WHERE key LIKE 'geo_%'"
            ) as cursor:
                async for row in cursor:
                    raw_config[row[0]] = row[1] if row[1] else ""

            config_obj = {
                "geo_enabled": raw_config.get("geo_enabled") == "1",
                "mode": raw_config.get("geo_global_mode", "deny"),
                "countries": json.loads(raw_config.get("geo_global_blocked_countries", "[]")),
                "continents": json.loads(raw_config.get("geo_global_blocked_continents", "[]")),
                "response_html": raw_config.get("geo_global_response_html", ""),
            }

            landing_policies = []
            if marketplace_public_landings_enabled():
                # Get landing summary: prompts with geo_policy set
                async with conn.execute("""
                    SELECT p.id, p.name, p.public_id, p.geo_policy,
                           pcd.custom_domain
                    FROM PROMPTS p
                    LEFT JOIN PROMPT_CUSTOM_DOMAINS pcd ON pcd.prompt_id = p.id AND pcd.is_active = 1
                    WHERE p.geo_policy IS NOT NULL
                    ORDER BY p.name
                """) as cursor:
                    async for row in cursor:
                        policy = None
                        try:
                            policy = json.loads(row[3]) if row[3] else None
                        except (json.JSONDecodeError, TypeError):
                            pass
                        if policy:
                            landing_policies.append({
                                "id": row[0],
                                "name": row[1],
                                "public_id": row[2],
                                "mode": policy.get("mode", "deny"),
                                "countries": policy.get("countries", []),
                                "enabled": policy.get("enabled", False),
                                "updated_at": policy.get("updated_at", ""),
                                "custom_domain": row[4],
                            })

        return JSONResponse(content={
            "success": True,
            "status": status_obj,
            "config": config_obj,
            "landing_policies": landing_policies,
        })

    except Exception as e:
        logger.error(f"Error getting geo status: {e}")
        return JSONResponse(status_code=500, content={"success": False, "message": str(e)})


@router.put("/api/admin/geo/global")
async def update_geo_global(request: Request, current_user: User = Depends(get_current_user)):
    """Save global geo-blocking configuration and sync to Cloudflare."""
    if current_user is None:
        return unauthenticated_response()

    if not await current_user.is_admin:
        return JSONResponse(status_code=403, content={"success": False, "message": "Admin access required"})

    try:
        data = await request.json()

        # Validate (JS sends geo_enabled, accept both keys)
        enabled = bool(data.get("geo_enabled", data.get("enabled", False)))
        mode = data.get("mode", "deny")
        if mode not in ("deny", "allow"):
            return JSONResponse(status_code=400, content={"success": False, "message": "Invalid mode"})

        countries = validate_country_codes(data.get("countries", []))
        continents = validate_continent_codes(data.get("continents", []))
        response_html = str(data.get("response_html", ""))
        if len(response_html.encode("utf-8")) > 10240:
            return JSONResponse(status_code=400, content={"success": False, "message": "Custom block page HTML exceeds 10 KB limit"})

        # Save to SYSTEM_CONFIG
        async with get_db_connection() as conn:
            updates = {
                "geo_enabled": "1" if enabled else "0",
                "geo_global_mode": mode,
                "geo_global_blocked_countries": json.dumps(countries),
                "geo_global_blocked_continents": json.dumps(continents),
                "geo_global_response_html": response_html,
            }
            for key, value in updates.items():
                await conn.execute(
                    "INSERT OR REPLACE INTO SYSTEM_CONFIG (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
                    (key, value)
                )
            await conn.commit()

        # Sync to Cloudflare
        sync_result = await geo_sync_engine.sync_all()

        return JSONResponse(content={
            "success": True,
            "message": "Global geo-blocking configuration saved and synced",
            "sync": sync_result
        })

    except Exception as e:
        logger.error(f"Error updating global geo config: {e}")
        return JSONResponse(status_code=500, content={"success": False, "message": str(e)})


@router.post("/api/admin/geo/sync")
async def force_geo_sync(request: Request, current_user: User = Depends(get_current_user)):
    """Force re-sync all geo-blocking rules to Cloudflare."""
    if current_user is None:
        return unauthenticated_response()

    if not await current_user.is_admin:
        return JSONResponse(status_code=403, content={"success": False, "message": "Admin access required"})

    try:
        sync_result = await geo_sync_engine.sync_all()
        return JSONResponse(content={"success": True, "message": "Geo rules synced", "sync": sync_result})
    except Exception as e:
        logger.error(f"Error syncing geo rules: {e}")
        return JSONResponse(status_code=500, content={"success": False, "message": str(e)})


@router.post("/api/admin/geo/enable-transforms")
async def enable_geo_transforms(request: Request, current_user: User = Depends(get_current_user)):
    """Enable Cloudflare visitor location managed headers."""
    if current_user is None:
        return unauthenticated_response()

    if not await current_user.is_admin:
        return JSONResponse(status_code=403, content={"success": False, "message": "Admin access required"})

    try:
        client = CloudflareGeoClient()
        if not client.is_configured():
            return JSONResponse(status_code=400, content={"success": False, "message": "Cloudflare not configured"})

        result = await client.enable_managed_transforms()
        return JSONResponse(content={"success": True, "message": "Managed transforms enabled", "result": result})
    except Exception as e:
        logger.error(f"Error enabling transforms: {e}")
        return JSONResponse(status_code=500, content={"success": False, "message": str(e)})


@router.delete("/api/admin/geo/rules")
async def remove_geo_rules(request: Request, current_user: User = Depends(get_current_user)):
    """Remove all aurvek geo-blocking rules from Cloudflare."""
    if current_user is None:
        return unauthenticated_response()

    if not await current_user.is_admin:
        return JSONResponse(status_code=403, content={"success": False, "message": "Admin access required"})

    try:
        result = await geo_sync_engine.remove_all_rules()
        return JSONResponse(content={"success": True, "message": "All geo rules removed", "result": result})
    except Exception as e:
        logger.error(f"Error removing geo rules: {e}")
        return JSONResponse(status_code=500, content={"success": False, "message": str(e)})


@router.delete("/api/admin/geo/landing/{public_id}")
async def delete_landing_geo_policy(public_id: str, current_user: User = Depends(get_current_user)):
    """Remove geo-blocking policy from a specific landing page."""
    require_public_landings_enabled()

    if current_user is None:
        return unauthenticated_response()

    if not await current_user.is_admin:
        return JSONResponse(status_code=403, content={"success": False, "message": "Admin access required"})

    try:
        async with get_db_connection() as conn:
            async with conn.execute(
                "SELECT id FROM PROMPTS WHERE public_id = ?", (public_id,)
            ) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return JSONResponse(status_code=404, content={"success": False, "message": "Landing not found"})

            await conn.execute(
                "UPDATE PROMPTS SET geo_policy = NULL WHERE public_id = ?",
                (public_id,)
            )
            await conn.commit()

        # Re-sync to CF
        async with get_db_connection(readonly=True) as conn:
            async with conn.execute(
                "SELECT value FROM SYSTEM_CONFIG WHERE key = 'geo_enabled'"
            ) as cursor:
                row = await cursor.fetchone()
                geo_enabled = row[0] == "1" if row else False

        if geo_enabled:
            await geo_sync_engine.sync_all()

        return JSONResponse(content={"success": True, "message": "Landing geo policy removed"})
    except Exception as e:
        logger.error(f"Error deleting landing geo policy for {public_id}: {e}")
        return JSONResponse(status_code=500, content={"success": False, "message": str(e)})


# =============================================================================
# Admin Pricing Configuration
# =============================================================================
