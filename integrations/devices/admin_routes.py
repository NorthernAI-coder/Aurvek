from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from auth import get_current_user
from captcha_service import get_captcha_config
from common import GOOGLE_CLIENT_ID, PRIMARY_APP_DOMAIN, get_template_context, templates
from integrations.devices.service import (
    DeviceValidationError,
    add_device_to_group,
    clear_binding,
    create_device,
    create_group,
    get_admin_page_data,
    remove_device_from_group,
    rotate_device_token,
    set_binding,
    set_device_enabled,
    soft_delete_device,
    soft_delete_group,
    update_device,
    update_group,
)
from models import User


router = APIRouter()


def _login_response(request: Request):
    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "captcha": get_captcha_config(),
            "google_oauth_available": bool(GOOGLE_CLIENT_ID),
        },
    )


def _redirect(message: str | None = None, error: str | None = None) -> RedirectResponse:
    params = {}
    if message:
        params["message"] = message
    if error:
        params["error"] = error
    query = f"?{urlencode(params)}" if params else ""
    return RedirectResponse(url=f"/admin/devices{query}", status_code=303)


def _form_int(value, field: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise DeviceValidationError(f"{field} must be a number")
    if parsed <= 0:
        raise DeviceValidationError(f"{field} must be positive")
    return parsed


def _optional_form_int(value, field: str) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    return _form_int(value, field)


def _form_int_list(values, field: str) -> list[int]:
    parsed = []
    for value in values:
        if value is None or str(value).strip() == "":
            continue
        parsed.append(_form_int(value, field))
    return parsed


async def _render_admin_devices(
    request: Request,
    current_user: User,
    *,
    message: str | None = None,
    error: str | None = None,
    token_value: str | None = None,
    token_slug: str | None = None,
) -> HTMLResponse:
    page_data = await get_admin_page_data()
    context = await get_template_context(request, current_user)
    token_block = None
    if token_value and token_slug:
        token_block = "\n".join(
            [
                f"AURVEK_BASE_URL=https://{PRIMARY_APP_DOMAIN}",
                f"AURVEK_DEVICE_SLUG={token_slug}",
                f"AURVEK_DEVICE_TOKEN={token_value}",
            ]
        )
    context.update(
        {
            **page_data,
            "current_user_id": current_user.id,
            "message": message or request.query_params.get("message"),
            "error": error or request.query_params.get("error"),
            "token_block": token_block,
        }
    )
    return templates.TemplateResponse("admin_devices.html", context)


@router.get("/admin/devices", response_class=HTMLResponse)
async def admin_devices(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    if current_user is None:
        return _login_response(request)
    if not await current_user.is_admin:
        raise HTTPException(status_code=403, detail="Access denied")

    return await _render_admin_devices(request, current_user)


@router.post("/admin/devices", response_class=HTMLResponse)
async def admin_devices_action(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    if current_user is None:
        return _login_response(request)
    if not await current_user.is_admin:
        raise HTTPException(status_code=403, detail="Access denied")

    form = await request.form()
    action = (form.get("action") or "").strip()

    try:
        if action == "create_device":
            owner_user_id = _optional_form_int(form.get("owner_user_id"), "Owner")
            if owner_user_id is None:
                owner_user_id = current_user.id
            slug = (form.get("slug") or "").strip()
            result = await create_device(
                owner_user_id=owner_user_id,
                display_name=form.get("display_name"),
                slug=slug,
                device_type=form.get("device_type") or "custom",
                notes=form.get("notes") or "",
                capability_names=form.getlist("capabilities"),
                group_ids=_form_int_list(form.getlist("group_ids"), "Group"),
                conversation_id=_optional_form_int(form.get("conversation_id"), "Conversation"),
            )
            return await _render_admin_devices(
                request,
                current_user,
                message="Device created. Copy the token now; it will not be shown again.",
                token_value=result.token,
                token_slug=slug,
            )

        if action == "update_device":
            await update_device(
                device_id=_form_int(form.get("device_id"), "Device"),
                display_name=form.get("display_name"),
                slug=form.get("slug"),
                device_type=form.get("device_type") or "custom",
                notes=form.get("notes") or "",
                capability_names=form.getlist("capabilities"),
                icon_class=form.get("icon_class"),
            )
            return _redirect(message="Device updated")

        if action == "set_device_enabled":
            await set_device_enabled(
                _form_int(form.get("device_id"), "Device"),
                (form.get("enabled") or "0") == "1",
            )
            return _redirect(message="Device status updated")

        if action == "rotate_device_token":
            device_id = _form_int(form.get("device_id"), "Device")
            result = await rotate_device_token(device_id)
            token_slug = (form.get("device_slug") or f"device-{device_id}").strip()
            return await _render_admin_devices(
                request,
                current_user,
                message="Device token rotated. Copy the new token now.",
                token_value=result.token,
                token_slug=token_slug,
            )

        if action == "soft_delete_device":
            await soft_delete_device(_form_int(form.get("device_id"), "Device"))
            return _redirect(message="Device removed")

        if action == "create_group":
            await create_group(
                owner_user_id=_form_int(form.get("owner_user_id"), "Owner"),
                name=form.get("name"),
                slug=form.get("slug"),
                notes=form.get("notes") or "",
                icon_class=form.get("icon_class"),
            )
            return _redirect(message="Group created")

        if action == "update_group":
            await update_group(
                group_id=_form_int(form.get("group_id"), "Group"),
                name=form.get("name"),
                slug=form.get("slug"),
                notes=form.get("notes") or "",
                icon_class=form.get("icon_class"),
            )
            return _redirect(message="Group updated")

        if action == "soft_delete_group":
            await soft_delete_group(_form_int(form.get("group_id"), "Group"))
            return _redirect(message="Group removed")

        if action == "add_membership":
            await add_device_to_group(
                device_id=_form_int(form.get("device_id"), "Device"),
                group_id=_form_int(form.get("group_id"), "Group"),
                is_primary_route_group=bool(form.get("is_primary_route_group")),
                routing_priority=_form_int(form.get("routing_priority") or 100, "Priority"),
            )
            return _redirect(message="Device added to group")

        if action == "remove_membership":
            await remove_device_from_group(
                device_id=_form_int(form.get("device_id"), "Device"),
                group_id=_form_int(form.get("group_id"), "Group"),
            )
            return _redirect(message="Device removed from group")

        if action == "set_binding":
            await set_binding(
                target_type=(form.get("target_type") or "").strip(),
                target_id=_form_int(form.get("target_id"), "Target"),
                conversation_id=_form_int(form.get("conversation_id"), "Conversation"),
                response_mode=form.get("response_mode") or "text",
            )
            return _redirect(message="Binding updated")

        if action == "clear_binding":
            await clear_binding(
                target_type=(form.get("target_type") or "").strip(),
                target_id=_form_int(form.get("target_id"), "Target"),
            )
            return _redirect(message="Binding cleared")

        return _redirect(error="Unknown device action")
    except DeviceValidationError as exc:
        return await _render_admin_devices(request, current_user, error=str(exc))
